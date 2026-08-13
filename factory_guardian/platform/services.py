"""營運服務層：告警生命週期、工單、班別交接、稽核與分析。

這一層是「正式維運平台」與「Demo」最實質的差異。Demo 只需要把
Agent 的推理過程演出來；正式平台要回答的是現場真正會問的問題：

- 現在有哪些未處理的告警？誰確認了？多久沒人動？
- 這張工單派給誰？做完了沒？花了多久？
- 上一班留下什麼未結案的事？
- 這個月的 MTTA / MTTR 是多少？告警最多的是哪台機器？

所有狀態變更都會寫進 ``audit_events``，帶上真實操作者身分。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..orchestrator import LoopResult
from .store import Store, dump_json, load_json, utcnow

# --------------------------------------------------------------------------------------
# 常數
# --------------------------------------------------------------------------------------
ALARM_STATES = ("open", "acknowledged", "resolved", "closed")
ALARM_ACTIVE_STATES = ("open", "acknowledged")
WORK_ORDER_STATES = ("open", "in_progress", "blocked", "completed", "cancelled")
WORK_ORDER_PRIORITIES = ("low", "normal", "high", "urgent")
WORK_ORDER_KINDS = ("corrective", "preventive", "inspection", "safety")

_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _minutes_between(start: str | None, end: str | None) -> float | None:
    a, b = _parse_ts(start), _parse_ts(end)
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() / 60.0, 2)


# Monitoring Agent 的觸發字串是機器可讀格式（threshold:vibration=WARNING）。
# 告警清單是給值班人員掃視的，這裡翻成中文再存。
_SIGNAL_LABELS = {
    "vibration": "振動",
    "temperature": "溫度",
    "spindle_load": "主軸負載",
    "current": "電流",
    "pressure": "壓力",
    "coolant_flow": "冷卻流量",
    "coolant_temp": "冷卻液溫度",
    "noise": "噪音",
    "rpm": "轉速",
    "rpm_pct": "轉速比",
}


def describe_trigger(trigger: str) -> str:
    """把單一觸發條件翻成人看得懂的短句。無法辨識時原樣回傳。"""
    if trigger.startswith("threshold:"):
        body = trigger.split(":", 1)[1]
        signal, _, band = body.partition("=")
        label = _SIGNAL_LABELS.get(signal, signal)
        level = {"CRITICAL": "超出危險門檻", "WARNING": "超出警告門檻"}.get(band, band)
        return f"{label}{level}"
    if trigger.startswith("trend:"):
        return f"持續惡化趨勢（{trigger.split(':', 1)[1]}）"
    if trigger.startswith("health:"):
        return f"健康度低於門檻（{trigger.split(':', 1)[1]}）"
    return trigger


# --------------------------------------------------------------------------------------
# 稽核
# --------------------------------------------------------------------------------------
class AuditService:
    """平台操作稽核：誰、在哪個站點、對什麼、做了什麼。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    def record(
        self,
        actor: str,
        action: str,
        *,
        actor_role: str = "",
        site_id: str = "",
        target: str = "",
        outcome: str = "ok",
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self.store.write() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (ts, actor, actor_role, action, site_id, target, outcome, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (utcnow(), actor, actor_role, action, site_id, target, outcome, dump_json(detail or {})),
            )

    def list(
        self,
        *,
        site_id: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        since: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        if site_id:
            where.append("site_id = ?")
            params.append(site_id)
        if actor:
            where.append("actor = ?")
            params.append(actor)
        if action:
            where.append("action = ?")
            params.append(action)
        if since:
            where.append("ts >= ?")
            params.append(since)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = self.store.scalar(f"SELECT COUNT(*) FROM audit_events {clause}", params, default=0)
        rows = self.store.query(
            f"SELECT * FROM audit_events {clause} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        for row in rows:
            row["detail"] = load_json(row.get("detail"), {})
        return {"total": int(total), "records": rows, "limit": limit, "offset": offset}


# --------------------------------------------------------------------------------------
# 告警
# --------------------------------------------------------------------------------------
class AlarmService:
    """告警的完整生命週期。

    狀態機：
        open ──ack──▶ acknowledged ──resolve──▶ resolved ──close──▶ closed
          └────────────resolve───────────────────┘

    dedup_key 保證「同一個持續中的異常」只有一筆未關閉告警。
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self.audit = AuditService(store)

    # ------------------------------------------------------------------ 產生
    def raise_alarm(
        self,
        site_id: str,
        *,
        title: str,
        severity: str,
        machine_id: str = "",
        detail: str = "",
        dedup_key: str | None = None,
        source: str = "monitoring",
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = dedup_key or f"{machine_id}:{title}"
        now = utcnow()
        with self.store.write() as conn:
            existing = conn.execute(
                """
                SELECT alarm_id, severity, occurrence_count FROM alarms
                WHERE site_id = ? AND dedup_key = ? AND state IN ('open','acknowledged')
                """,
                (site_id, key),
            ).fetchone()
            if existing:
                # 已有未關閉告警：累加次數，並在嚴重度升高時升級。
                new_severity = existing["severity"]
                if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(existing["severity"], 0):
                    new_severity = severity
                conn.execute(
                    """
                    UPDATE alarms SET occurrence_count = occurrence_count + 1,
                        updated_at = ?, severity = ?, detail = ?, evidence = ?
                    WHERE alarm_id = ?
                    """,
                    (now, new_severity, detail, dump_json(evidence or {}), existing["alarm_id"]),
                )
                alarm_id = existing["alarm_id"]
            else:
                alarm_id = _new_id("ALM")
                conn.execute(
                    """
                    INSERT INTO alarms (alarm_id, site_id, machine_id, dedup_key, severity, state,
                                        title, detail, source, evidence, raised_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
                    """,
                    (alarm_id, site_id, machine_id, key, severity, title, detail,
                     source, dump_json(evidence or {}), now, now),
                )
        return self.get(alarm_id) or {}

    def raise_from_event(self, site_id: str, event) -> dict[str, Any]:
        """把 Monitoring Agent 的 AnomalyEvent 轉成平台告警。"""
        triggers = list(event.triggers)
        # 用觸發訊號組 dedup key：同一台機同一組訊號異常視為同一件事。
        key = f"{event.machine_id}:{'|'.join(sorted(triggers)) or event.kind}"
        readable = "、".join(describe_trigger(t) for t in triggers) if triggers else event.kind
        title = f"{event.machine_id} {readable}"
        detail = (
            f"健康度 {event.health:.1f}；偵測於 tick {event.tick}"
            f"（模擬時間 {event.sim_minutes:.0f} 分）。"
            f"原始觸發條件：{'、'.join(triggers) if triggers else event.kind}。"
        )
        return self.raise_alarm(
            site_id,
            title=title,
            severity=event.severity.value,
            machine_id=event.machine_id,
            detail=detail,
            dedup_key=key,
            source=event.detector,
            evidence={
                "event_id": event.event_id,
                "kind": event.kind,
                "triggers": triggers,
                "health": round(event.health, 1),
                "tick": event.tick,
                "readings": {k: v.to_dict() for k, v in event.readings.items()},
            },
        )

    # ------------------------------------------------------------------ 狀態轉移
    def acknowledge(self, alarm_id: str, actor: str, actor_role: str = "", note: str = "") -> dict[str, Any]:
        now = utcnow()
        with self.store.write() as conn:
            row = conn.execute("SELECT state FROM alarms WHERE alarm_id = ?", (alarm_id,)).fetchone()
            if row is None:
                raise KeyError(alarm_id)
            if row["state"] != "open":
                raise ValueError(f"只有 open 狀態的告警可以確認，目前為 {row['state']}。")
            conn.execute(
                """
                UPDATE alarms SET state = 'acknowledged', acked_at = ?, acked_by = ?, updated_at = ?
                WHERE alarm_id = ?
                """,
                (now, actor, now, alarm_id),
            )
        self.audit.record(actor, "alarm.acknowledge", actor_role=actor_role,
                          target=alarm_id, detail={"note": note})
        return self.get(alarm_id) or {}

    def resolve(
        self, alarm_id: str, actor: str, resolution: str, actor_role: str = ""
    ) -> dict[str, Any]:
        now = utcnow()
        with self.store.write() as conn:
            row = conn.execute("SELECT state FROM alarms WHERE alarm_id = ?", (alarm_id,)).fetchone()
            if row is None:
                raise KeyError(alarm_id)
            if row["state"] not in ALARM_ACTIVE_STATES:
                raise ValueError(f"告警已是 {row['state']} 狀態，無法再結案。")
            conn.execute(
                """
                UPDATE alarms SET state = 'resolved', resolved_at = ?, resolved_by = ?,
                    resolution = ?, updated_at = ?
                WHERE alarm_id = ?
                """,
                (now, actor, resolution, now, alarm_id),
            )
        self.audit.record(actor, "alarm.resolve", actor_role=actor_role,
                          target=alarm_id, detail={"resolution": resolution})
        return self.get(alarm_id) or {}

    def close(self, alarm_id: str, actor: str, actor_role: str = "") -> dict[str, Any]:
        now = utcnow()
        with self.store.write() as conn:
            conn.execute(
                "UPDATE alarms SET state = 'closed', updated_at = ? WHERE alarm_id = ?",
                (now, alarm_id),
            )
        self.audit.record(actor, "alarm.close", actor_role=actor_role, target=alarm_id)
        return self.get(alarm_id) or {}

    def auto_resolve(self, site_id: str, result: LoopResult) -> int:
        """閉環驗證通過後，自動結案該機台的未關閉告警。"""
        machine_id = result.event.machine_id if result.event else ""
        if not machine_id:
            return 0
        plan_id = result.executed_plan.plan_id if result.executed_plan else "-"
        now = utcnow()
        with self.store.write() as conn:
            cursor = conn.execute(
                """
                UPDATE alarms SET state = 'resolved', resolved_at = ?, resolved_by = 'agent-loop',
                    resolution = ?, updated_at = ?
                WHERE site_id = ? AND machine_id = ? AND state IN ('open','acknowledged')
                """,
                (now, f"Agent 閉環執行 {plan_id} 後驗證通過，KPI 恢復。", now, site_id, machine_id),
            )
            count = cursor.rowcount
        if count:
            self.audit.record(
                "agent-loop", "alarm.auto_resolve", actor_role="system", site_id=site_id,
                target=machine_id, detail={"resolved": count, "plan_id": plan_id},
            )
        return count

    # ------------------------------------------------------------------ 查詢
    def get(self, alarm_id: str) -> dict[str, Any] | None:
        row = self.store.query_one("SELECT * FROM alarms WHERE alarm_id = ?", (alarm_id,))
        return self._decorate(row) if row else None

    def list(
        self,
        *,
        site_id: str | None = None,
        state: str | None = None,
        severity: str | None = None,
        machine_id: str | None = None,
        active_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        if site_id:
            where.append("site_id = ?")
            params.append(site_id)
        if state:
            where.append("state = ?")
            params.append(state)
        elif active_only:
            where.append("state IN ('open','acknowledged')")
        if severity:
            where.append("severity = ?")
            params.append(severity)
        if machine_id:
            where.append("machine_id = ?")
            params.append(machine_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = self.store.scalar(f"SELECT COUNT(*) FROM alarms {clause}", params, default=0)
        rows = self.store.query(
            f"""
            SELECT * FROM alarms {clause}
            ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                     raised_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )
        return {
            "total": int(total),
            "alarms": [self._decorate(r) for r in rows],
            "limit": limit,
            "offset": offset,
        }

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        row = dict(row)
        row["evidence"] = load_json(row.get("evidence"), {})
        now = utcnow()
        row["age_min"] = _minutes_between(row.get("raised_at"), now)
        row["ack_latency_min"] = _minutes_between(row.get("raised_at"), row.get("acked_at"))
        row["resolve_latency_min"] = _minutes_between(row.get("raised_at"), row.get("resolved_at"))
        # 未確認且已超過 15 分鐘 → 現場清單要把它推到最上面。
        row["stale"] = bool(
            row["state"] == "open" and (row["age_min"] or 0) > 15
        )
        return row


# --------------------------------------------------------------------------------------
# 工單
# --------------------------------------------------------------------------------------
class WorkOrderService:
    """維修工單：建立、指派、進行、完成。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.audit = AuditService(store)

    def create(
        self,
        site_id: str,
        *,
        title: str,
        machine_id: str = "",
        detail: str = "",
        kind: str = "corrective",
        priority: str = "normal",
        assignee: str = "",
        estimated_min: float = 0.0,
        alarm_id: str | None = None,
        created_by: str = "system",
        actor_role: str = "",
        source_key: str = "",
    ) -> dict[str, Any]:
        if priority not in WORK_ORDER_PRIORITIES:
            raise ValueError(f"未知優先度 {priority}")
        if kind not in WORK_ORDER_KINDS:
            raise ValueError(f"未知工單類型 {kind}")
        work_order_id = _new_id("WO")
        now = utcnow()
        with self.store.write() as conn:
            # source_key 有唯一索引：兩條執行緒同時為同一個閉環建工單時，
            # 後到的那個會撞索引，這裡把它接住並回傳既有工單。
            if source_key:
                dup = conn.execute(
                    "SELECT work_order_id FROM work_orders WHERE source_key = ?", (source_key,)
                ).fetchone()
                if dup:
                    work_order_id = dup["work_order_id"]
            conn.execute(
                """
                INSERT OR IGNORE INTO work_orders
                    (work_order_id, site_id, machine_id, alarm_id, kind,
                     priority, state, title, detail, assignee, estimated_min,
                     created_by, created_at, updated_at, source_key)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (work_order_id, site_id, machine_id, alarm_id, kind, priority, title,
                 detail, assignee, estimated_min, created_by, now, now, source_key),
            )
        self.audit.record(created_by, "workorder.create", actor_role=actor_role, site_id=site_id,
                          target=work_order_id, detail={"title": title, "priority": priority})
        return self.get(work_order_id) or {}

    def create_from_loop(self, site_id: str, result: LoopResult) -> dict[str, Any] | None:
        """把 Agent 閉環產生的工單落進資料庫。"""
        wo = result.work_order
        if wo is None:
            return None
        # 同一個閉環工單只建一次（重試會重複呼叫）。
        #
        # Agent 的 work_order_id 是 "WO-{tick}-{seq}"，站點重建時 tick 與 seq 都會歸零，
        # 所以它在同一個站點內會重複。用「站點 + Agent 工單號 + 機台 + 問題」當來源鍵，
        # 並存進獨立欄位精確比對——用 detail LIKE 會讓 WO-9-1 誤中 WO-9-10。
        source_key = f"{site_id}|{wo.work_order_id}|{wo.machine_id}|{wo.problem}"
        existing = self.store.query_one(
            "SELECT work_order_id FROM work_orders WHERE source_key = ?", (source_key,)
        )
        if existing:
            return self.get(existing["work_order_id"])
        # MaintenanceAgent 產出的是 "P1"/"P2"/"P3"，不是數字。
        priority_map = {"P1": "urgent", "P2": "high", "P3": "normal"}
        detail_lines = [
            f"來源：Agent 閉環工單 {wo.work_order_id}",
            f"問題：{wo.problem}",
            f"需要技能：{wo.required_skill}",
        ]
        if wo.suggested_parts:
            detail_lines.append(f"建議零件：{'、'.join(wo.suggested_parts)}")
        if wo.sop_refs:
            detail_lines.append(f"SOP：{'、'.join(wo.sop_refs)}")
        if wo.safety_precautions:
            detail_lines.append(f"安全注意：{'；'.join(wo.safety_precautions)}")
        return self.create(
            site_id,
            title=f"{wo.machine_id} {wo.problem}",
            machine_id=wo.machine_id,
            detail="\n".join(detail_lines),
            kind="corrective",
            priority=priority_map.get(wo.priority, "normal"),
            estimated_min=float(wo.estimated_repair_min),
            created_by="agent-loop",
            actor_role="system",
            source_key=source_key,
        )

    def update(
        self,
        work_order_id: str,
        actor: str,
        *,
        actor_role: str = "",
        state: str | None = None,
        assignee: str | None = None,
        priority: str | None = None,
        completion_note: str | None = None,
    ) -> dict[str, Any]:
        current = self.get(work_order_id)
        if current is None:
            raise KeyError(work_order_id)
        if state is not None and state not in WORK_ORDER_STATES:
            raise ValueError(f"未知工單狀態 {state}")
        if priority is not None and priority not in WORK_ORDER_PRIORITIES:
            raise ValueError(f"未知優先度 {priority}")

        now = utcnow()
        fields: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]
        if state is not None:
            fields.append("state = ?")
            params.append(state)
            # 進行中與完成各自打一次時間戳，MTTR 才算得出來。
            if state == "in_progress" and not current.get("started_at"):
                fields.append("started_at = ?")
                params.append(now)
            if state in ("completed", "cancelled"):
                fields.append("completed_at = ?")
                params.append(now)
        if assignee is not None:
            fields.append("assignee = ?")
            params.append(assignee)
        if priority is not None:
            fields.append("priority = ?")
            params.append(priority)
        if completion_note is not None:
            fields.append("completion_note = ?")
            params.append(completion_note)
        params.append(work_order_id)

        with self.store.write() as conn:
            conn.execute(
                f"UPDATE work_orders SET {', '.join(fields)} WHERE work_order_id = ?", params
            )
        self.audit.record(
            actor, "workorder.update", actor_role=actor_role, site_id=current["site_id"],
            target=work_order_id,
            detail={"state": state, "assignee": assignee, "priority": priority},
        )
        return self.get(work_order_id) or {}

    def get(self, work_order_id: str) -> dict[str, Any] | None:
        row = self.store.query_one(
            "SELECT * FROM work_orders WHERE work_order_id = ?", (work_order_id,)
        )
        return self._decorate(row) if row else None

    def list(
        self,
        *,
        site_id: str | None = None,
        state: str | None = None,
        assignee: str | None = None,
        machine_id: str | None = None,
        open_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        if site_id:
            where.append("site_id = ?")
            params.append(site_id)
        if state:
            where.append("state = ?")
            params.append(state)
        elif open_only:
            where.append("state IN ('open','in_progress','blocked')")
        if assignee:
            where.append("assignee = ?")
            params.append(assignee)
        if machine_id:
            where.append("machine_id = ?")
            params.append(machine_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = self.store.scalar(f"SELECT COUNT(*) FROM work_orders {clause}", params, default=0)
        rows = self.store.query(
            f"""
            SELECT * FROM work_orders {clause}
            ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                   WHEN 'normal' THEN 2 ELSE 3 END,
                     created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )
        return {
            "total": int(total),
            "work_orders": [self._decorate(r) for r in rows],
            "limit": limit,
            "offset": offset,
        }

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        row = dict(row)
        row["age_min"] = _minutes_between(row.get("created_at"), utcnow())
        row["duration_min"] = _minutes_between(row.get("started_at"), row.get("completed_at"))
        return row


# --------------------------------------------------------------------------------------
# 班別交接
# --------------------------------------------------------------------------------------
class HandoverService:
    """班別交接記錄：交班時把未結案事項寫下來給下一班。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.audit = AuditService(store)

    def create(
        self,
        site_id: str,
        *,
        shift: str,
        author: str,
        summary: str,
        actor_role: str = "",
        open_items: list[str] | None = None,
    ) -> dict[str, Any]:
        handover_id = _new_id("HO")
        items = open_items
        if items is None:
            # 沒有手動填寫時，自動帶入目前未結案的告警與工單。
            items = self._auto_open_items(site_id)
        with self.store.write() as conn:
            conn.execute(
                """
                INSERT INTO shift_handovers (handover_id, site_id, shift, author, summary,
                                             open_items, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (handover_id, site_id, shift, author, summary, dump_json(items), utcnow()),
            )
        self.audit.record(author, "handover.create", actor_role=actor_role, site_id=site_id,
                          target=handover_id, detail={"shift": shift})
        return self.get(handover_id) or {}

    def _auto_open_items(self, site_id: str) -> list[str]:
        alarms = self.store.query(
            """
            SELECT alarm_id, title, severity FROM alarms
            WHERE site_id = ? AND state IN ('open','acknowledged')
            ORDER BY raised_at DESC LIMIT 20
            """,
            (site_id,),
        )
        orders = self.store.query(
            """
            SELECT work_order_id, title, state FROM work_orders
            WHERE site_id = ? AND state IN ('open','in_progress','blocked')
            ORDER BY created_at DESC LIMIT 20
            """,
            (site_id,),
        )
        items = [f"[告警/{a['severity']}] {a['alarm_id']} {a['title']}" for a in alarms]
        items += [f"[工單/{o['state']}] {o['work_order_id']} {o['title']}" for o in orders]
        return items

    def get(self, handover_id: str) -> dict[str, Any] | None:
        row = self.store.query_one(
            "SELECT * FROM shift_handovers WHERE handover_id = ?", (handover_id,)
        )
        if row is None:
            return None
        row["open_items"] = load_json(row.get("open_items"), [])
        return row

    def list(self, site_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clause = "WHERE site_id = ?" if site_id else ""
        params = [site_id] if site_id else []
        rows = self.store.query(
            f"SELECT * FROM shift_handovers {clause} ORDER BY created_at DESC LIMIT ?",
            [*params, limit],
        )
        for row in rows:
            row["open_items"] = load_json(row.get("open_items"), [])
        return rows


# --------------------------------------------------------------------------------------
# 分析
# --------------------------------------------------------------------------------------
class AnalyticsService:
    """維運指標：MTTA / MTTR、告警分布、工單負載、KPI 趨勢。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    def summary(self, site_id: str | None = None, days: int = 7) -> dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        site_clause = "AND site_id = ?" if site_id else ""
        site_params: list[Any] = [site_id] if site_id else []

        alarm_rows = self.store.query(
            f"""
            SELECT severity, state, raised_at, acked_at, resolved_at, machine_id
            FROM alarms WHERE raised_at >= ? {site_clause}
            """,
            [since, *site_params],
        )
        ack_latencies = [
            v for v in (_minutes_between(r["raised_at"], r["acked_at"]) for r in alarm_rows)
            if v is not None
        ]
        resolve_latencies = [
            v for v in (_minutes_between(r["raised_at"], r["resolved_at"]) for r in alarm_rows)
            if v is not None
        ]

        wo_rows = self.store.query(
            f"""
            SELECT state, priority, started_at, completed_at, machine_id, assignee
            FROM work_orders WHERE created_at >= ? {site_clause}
            """,
            [since, *site_params],
        )
        wo_durations = [
            v for v in (_minutes_between(r["started_at"], r["completed_at"]) for r in wo_rows)
            if v is not None
        ]

        by_machine: dict[str, int] = {}
        for row in alarm_rows:
            key = row["machine_id"] or "(未指定)"
            by_machine[key] = by_machine.get(key, 0) + 1

        by_severity: dict[str, int] = {}
        for row in alarm_rows:
            by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + 1

        by_wo_state: dict[str, int] = {}
        for row in wo_rows:
            by_wo_state[row["state"]] = by_wo_state.get(row["state"], 0) + 1

        return {
            "window_days": days,
            "since": since,
            "site_id": site_id,
            "alarms": {
                "total": len(alarm_rows),
                "by_severity": by_severity,
                "by_machine": dict(sorted(by_machine.items(), key=lambda kv: -kv[1])[:10]),
                "open_now": sum(1 for r in alarm_rows if r["state"] in ALARM_ACTIVE_STATES),
                # MTTA：從告警產生到有人按下確認的平均分鐘數
                "mtta_min": round(sum(ack_latencies) / len(ack_latencies), 1) if ack_latencies else None,
                # MTTR：從告警產生到結案的平均分鐘數
                "mttr_min": round(sum(resolve_latencies) / len(resolve_latencies), 1)
                if resolve_latencies else None,
                "ack_rate_pct": round(100.0 * len(ack_latencies) / len(alarm_rows), 1)
                if alarm_rows else None,
            },
            "work_orders": {
                "total": len(wo_rows),
                "by_state": by_wo_state,
                "open_now": sum(1 for r in wo_rows if r["state"] in ("open", "in_progress", "blocked")),
                "avg_duration_min": round(sum(wo_durations) / len(wo_durations), 1)
                if wo_durations else None,
            },
        }

    def kpi_trend(self, site_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.store.query(
            """
            SELECT ts, tick, factory_health, production_pct, max_delay_min,
                   hazard_exposure_min, open_alarms
            FROM kpi_samples WHERE site_id = ? ORDER BY ts DESC LIMIT ?
            """,
            (site_id, limit),
        )
        return list(reversed(rows))


__all__ = [
    "ALARM_ACTIVE_STATES",
    "ALARM_STATES",
    "WORK_ORDER_KINDS",
    "WORK_ORDER_PRIORITIES",
    "WORK_ORDER_STATES",
    "AlarmService",
    "AnalyticsService",
    "AuditService",
    "HandoverService",
    "WorkOrderService",
]
