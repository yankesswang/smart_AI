"""站點註冊表與執行期管理（Fleet 層）。

正式平台與單機 Demo 的第二個關鍵差別：同時管理多個站點。

每個註冊站點對應一個 ``SiteRuntime``：一個 adapter、一組 Agent、
一條背景時間軸。站點各自獨立推進與處理事件，一個站點出錯不會拖垮其他站點。

執行緒模型：
- 每個站點一條背景執行緒，以 ``poll_interval_s`` 為週期推進並偵測。
- 站點內部用 ``RLock`` 保護狀態；跨站點沒有共用可變狀態。
- 閉環（handle_event）在同一條站點執行緒內同步執行，執行期間該站點
  不再推進時間 —— 與真實系統「處理中的事件不會被新事件插隊」一致。
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..agents.base import AgentContext
from ..audit import AuditLog
from ..config import Settings, get_settings
from ..domain import ApprovalDecision, RecoveryPlan, Severity
from ..orchestrator import LoopResult, Orchestrator
from ..policy.engine import PolicyDecision, PolicyEngine
from ..prediction import ForecastService
from ..twin.scenarios import get_scenario
from .adapters import DataSourceAdapter, SimulatedAdapter, build_adapter
from .store import Store, dump_json, load_json, utcnow

MAX_EVENTS = 2000
MAX_HISTORY = 720
APPROVAL_TIMEOUT_S = 600.0


@dataclass
class PendingApproval:
    """等待人工裁決的方案。"""

    site_id: str
    plan: RecoveryPlan
    decision: PolicyDecision
    requested_at: str = field(default_factory=utcnow)
    event: threading.Event = field(default_factory=threading.Event)
    result: ApprovalDecision | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "plan": self.plan.to_dict(),
            "policy": self.decision.to_dict(),
            "requested_at": self.requested_at,
        }


class SiteRuntime:
    """單一站點的執行期狀態。"""

    def __init__(
        self,
        record: dict[str, Any],
        store: Store,
        settings: Settings,
        prediction: ForecastService,
        on_event: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.site_id = record["site_id"]
        self.name = record["name"]
        self.region = record.get("region", "")
        self.record = record
        self.store = store
        self.settings = settings
        self.prediction = prediction
        self._on_event = on_event

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.autopilot = bool(record.get("autopilot", True))
        self.poll_interval_s = float(record.get("poll_interval_s", 2.0))

        self.adapter: DataSourceAdapter = build_adapter(
            record.get("adapter_kind", "simulated"),
            self.site_id,
            load_json(record.get("adapter_config"), {}),
            settings=settings,
        )

        self.audit = AuditLog(settings=settings)
        self.ctx = AgentContext(
            settings=settings,
            audit=self.audit,
            policy=PolicyEngine(require_approval=settings.require_approval),
        )
        self.orch = self._build_orchestrator()

        self.events: list[dict[str, Any]] = []
        self.seq = 0
        self.history: list[dict[str, Any]] = []
        self.pending: PendingApproval | None = None
        self.last_loop: LoopResult | None = None
        self.live: dict[str, Any] = {}
        self.busy = False
        self.status = "stopped"
        self.last_error: str | None = None
        self.started_at = datetime.now(timezone.utc)
        self.last_poll_at: str | None = None

    # ------------------------------------------------------------------ 建構
    def _build_orchestrator(self) -> Orchestrator:
        if not isinstance(self.adapter, SimulatedAdapter):
            # Agent 閉環目前綁在 twin 上；真實 adapter 要接閉環時，
            # 需要提供等價的 twin 介面（狀態讀取 + 動作套用 + 投影）。
            raise NotImplementedError(
                f"站點 {self.site_id} 使用 {self.adapter.kind} adapter，"
                "Agent 閉環尚未支援非模擬資料源。"
            )
        return Orchestrator(
            twin=self.adapter.twin,
            ctx=self.ctx,
            approval=self._request_approval,
            on_stage=self._on_stage,
            forecaster=self.prediction,
        )

    @property
    def twin(self):
        return self.adapter.twin  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ 生命週期
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.adapter.start()
            self._stop.clear()
            self.status = "running"
            self.last_error = None
            self._thread = threading.Thread(
                target=self._run, name=f"site-{self.site_id}", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self.adapter.stop()
            self.status = "stopped"
            self._thread = None

    # ------------------------------------------------------------------ 背景迴圈
    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self._cycle()
                except Exception as exc:  # 單一站點的錯誤不能終止整個平台
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.status = "error"
                    self._push("error", {"message": self.last_error,
                                         "trace": traceback.format_exc(limit=3)})
                    # 出錯後放慢節奏，避免錯誤訊息洗版
                    self._stop.wait(5.0)
                else:
                    self._stop.wait(self.poll_interval_s)
        finally:
            # thread-local 連線只有本執行緒關得掉；站點停止／重啟多次之後
            # 這些連線會累積成 FD 洩漏。
            self.store.close()

    def _cycle(self) -> None:
        """推進一格時間，偵測異常；autopilot 開啟時自動處理。"""
        if isinstance(self.adapter, SimulatedAdapter):
            snapshot = self.adapter.step()
        else:
            snapshot = self.adapter.poll()

        self.last_poll_at = utcnow()
        detected = self.orch.monitoring.detect(snapshot, self.twin.topo)
        self.orch.safety.perceive(snapshot)
        self._record_history(snapshot)
        self._persist_kpi(snapshot)

        for event in detected:
            self._sync_alarm(event)
        # 一次只處理最嚴重的那一個；其餘留在告警清單裡等下一輪。
        actionable = [e for e in detected if e.severity.rank >= Severity.WARNING.rank]
        if actionable and self.autopilot:
            self._handle(max(actionable, key=lambda e: e.severity.rank))

    def _claim(self) -> bool:
        """搶下「這個站點正在處理事件」的旗標。

        check-and-set 必須在同一把鎖裡完成：背景迴圈與 trigger_loop
        會從不同執行緒同時嘗試啟動閉環，兩邊都跑起來的話會共用同一個
        Orchestrator 與 twin，而且 self.pending 只有一格，後者會覆蓋前者，
        讓先進來的那個閉環一路等到 600 秒逾時才被自動否決。
        """
        with self._lock:
            if self.busy:
                return False
            self.busy = True
            return True

    def _release(self) -> None:
        with self._lock:
            self.busy = False
            self.pending = None

    def _handle(self, event) -> None:
        if not self._claim():
            return
        self.status = "handling"
        try:
            self.last_loop = self.orch.handle_event(event)
            self._push("loop_done", {"result": self.last_loop.to_dict()})
            self._on_loop_complete(self.last_loop)
        finally:
            self._release()
            self.status = "running"

    # ------------------------------------------------------------------ 手動控制
    def trigger_loop(self, max_ticks: int = 40) -> bool:
        """手動要求跑一次閉環（autopilot 關閉時使用）。"""
        if not self._claim():
            return False
        thread = threading.Thread(target=self._trigger_worker, args=(max_ticks,), daemon=True)
        thread.start()
        return True

    def _trigger_worker(self, max_ticks: int) -> None:
        try:
            event = self.orch.run_until_event(max_ticks, min_severity=Severity.WARNING)
            if event is None:
                self._push("idle", {"message": "觀察視窗內未偵測到需要處理的異常。"})
                return
            self.last_loop = self.orch.handle_event(event)
            self._push("loop_done", {"result": self.last_loop.to_dict()})
            self._on_loop_complete(self.last_loop)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._push("error", {"message": self.last_error})
        finally:
            # 工作執行緒結束前釋放 sqlite 連線，否則每次手動觸發都漏一個 FD。
            self._release()
            self.store.close()

    def inject_scenario(self, scenario_id: str, actor: str) -> None:
        """注入情境。正式平台只在模擬站點開放，且會留下稽核紀錄。"""
        if not isinstance(self.adapter, SimulatedAdapter):
            raise ValueError("只有模擬站點可以注入情境。")
        scenario = get_scenario(scenario_id)
        with self._lock:
            for injection in scenario.injections:
                shifted = type(injection)(
                    fault_id=injection.fault_id,
                    machine_id=injection.machine_id,
                    start_tick=self.twin.tick + max(0, injection.start_tick - 2),
                    ramp_ticks=injection.ramp_ticks,
                    max_progress=injection.max_progress,
                )
                self.twin.schedule([shifted])
        self.audit.log("inject", actor, scenario=scenario_id, site_id=self.site_id)
        self._push("inject", {"scenario_id": scenario_id, "title": scenario.title, "actor": actor})

    def set_autopilot(self, enabled: bool) -> None:
        with self._lock:
            self.autopilot = enabled
        self._push("autopilot", {"enabled": enabled})

    # ------------------------------------------------------------------ 核准
    def _request_approval(self, plan: RecoveryPlan, decision: PolicyDecision) -> ApprovalDecision:
        pending = PendingApproval(site_id=self.site_id, plan=plan, decision=decision)
        self.pending = pending
        self._push("approval_required", pending.to_dict())
        if not pending.event.wait(timeout=APPROVAL_TIMEOUT_S):
            self.pending = None
            return ApprovalDecision(plan.plan_id, False, "timeout", "等待人工核准逾時，依安全原則不執行。")
        self.pending = None
        return pending.result or ApprovalDecision(plan.plan_id, False, "unknown", "未取得有效核准。")

    def submit_approval(self, plan_id: str, approved: bool, approver: str, reason: str = "") -> bool:
        pending = self.pending
        if pending is None or pending.plan.plan_id != plan_id:
            return False
        pending.result = ApprovalDecision(plan_id, approved, approver, reason, auto=False)
        pending.event.set()
        self._push("approval_submitted", {"plan_id": plan_id, "approved": approved, "approver": approver})
        return True

    # ------------------------------------------------------------------ 事件
    _LIVE_STAGES = ("detect", "diagnose", "impact", "plan", "safety", "rank",
                    "work_order", "execute", "verify")

    def _on_stage(self, stage: str, payload: dict[str, Any]) -> None:
        if stage in self._LIVE_STAGES:
            self.live[stage] = payload
        self._push(stage, payload)

    def _push(self, stage: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.seq += 1
            record = {
                "seq": self.seq,
                "site_id": self.site_id,
                "stage": stage,
                "ts": utcnow(),
                "payload": payload,
            }
            self.events.append(record)
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]
        if self._on_event is not None:
            try:
                self._on_event(self.site_id, stage, payload)
            except Exception:
                pass  # 訂閱者的錯誤不該影響站點運行

    def events_since(self, since: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self.events if e["seq"] > since]

    # ------------------------------------------------------------------ 歷史與 KPI
    def _record_history(self, snapshot) -> None:
        point: dict[str, Any] = {
            "tick": snapshot.tick,
            "ts": utcnow(),
            "production_pct": round(snapshot.production_pct, 1),
            "factory_health": round(snapshot.factory_health, 1),
        }
        for mid, machine in snapshot.machines.items():
            point[f"{mid}.health"] = round(machine.health, 1)
            for name, reading in machine.readings.items():
                point[f"{mid}.{name}"] = round(reading.value, 2)
        with self._lock:
            self.history.append(point)
            if len(self.history) > MAX_HISTORY:
                del self.history[: len(self.history) - MAX_HISTORY]

    def prediction_history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [p.copy() for p in self.history]

    def _persist_kpi(self, snapshot) -> None:
        """每 10 個 tick 落一次 KPI，讓歷史查詢不依賴記憶體。"""
        if snapshot.tick % 10 != 0:
            return
        kpi = self.twin.kpi()
        open_alarms = self.store.scalar(
            "SELECT COUNT(*) FROM alarms WHERE site_id = ? AND state IN ('open','acknowledged')",
            (self.site_id,),
            default=0,
        )
        with self.store.write() as conn:
            conn.execute(
                """
                INSERT INTO kpi_samples (site_id, ts, tick, factory_health, production_pct,
                                         max_delay_min, hazard_exposure_min, open_alarms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.site_id, utcnow(), snapshot.tick,
                    round(snapshot.factory_health, 2), round(snapshot.production_pct, 2),
                    round(float(kpi.get("max_delay_min", 0.0)), 2),
                    round(float(self.twin.hazard_exposure_min), 2),
                    int(open_alarms),
                ),
            )

    # ------------------------------------------------------------------ 告警同步
    def _sync_alarm(self, event) -> None:
        """把 Monitoring Agent 的異常事件同步成平台告警。

        用 (machine_id, 觸發條件) 當 dedup key：同一個持續中的異常
        只會有一筆未關閉告警，重複偵測累加 occurrence_count。
        """
        from .services import AlarmService  # 延後匯入，避免模組循環

        AlarmService(self.store).raise_from_event(self.site_id, event)

    def _on_loop_complete(self, result: LoopResult) -> None:
        """閉環完成後，把工單與告警狀態寫回資料庫。"""
        from .services import AlarmService, WorkOrderService

        try:
            if result.work_order:
                WorkOrderService(self.store).create_from_loop(self.site_id, result)
            if result.verified:
                AlarmService(self.store).auto_resolve(self.site_id, result)
        except Exception as exc:
            self._push("error", {"message": f"閉環結果落庫失敗：{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------------ 狀態輸出
    def summary(self) -> dict[str, Any]:
        """Fleet 總覽用的精簡狀態。"""
        snapshot = self.adapter.poll()
        kpi = self.twin.kpi()
        open_alarms = self.store.query(
            """
            SELECT severity, COUNT(*) AS n FROM alarms
            WHERE site_id = ? AND state IN ('open','acknowledged')
            GROUP BY severity
            """,
            (self.site_id,),
        )
        counts = {row["severity"]: row["n"] for row in open_alarms}
        return {
            "site_id": self.site_id,
            "name": self.name,
            "region": self.region,
            "status": self.status,
            "autopilot": self.autopilot,
            "busy": self.busy,
            "simulated": self.adapter.simulated,
            "adapter_kind": self.adapter.kind,
            "connected": self.adapter.connected,
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "tick": snapshot.tick,
            "factory_health": round(snapshot.factory_health, 1),
            "production_pct": round(snapshot.production_pct, 1),
            "max_delay_min": round(float(kpi.get("max_delay_min", 0.0)), 1),
            "hazard_exposure_min": round(float(self.twin.hazard_exposure_min), 1),
            "machines_down": sum(1 for m in snapshot.machines.values() if not m.online),
            "machine_count": len(snapshot.machines),
            "alarms": {
                "critical": counts.get("critical", 0),
                "warning": counts.get("warning", 0),
                "info": counts.get("info", 0),
                "total": sum(counts.values()),
            },
            "pending_approval": self.pending is not None,
        }

    def detail(self) -> dict[str, Any]:
        """站點主控台用的完整狀態。"""
        snapshot = self.adapter.poll()
        return {
            **self.summary(),
            "run_id": self.audit.run_id,
            "snapshot": snapshot.to_dict(),
            "monitoring": self.orch.monitoring.snapshot_summary(snapshot, self.twin.topo),
            "kpi": {k: round(v, 2) for k, v in self.twin.kpi().items()},
            "live": self.live,
            "history": self.history[-180:],
            "pending_approval_detail": self.pending.to_dict() if self.pending else None,
            "last_loop": self.last_loop.to_dict() if self.last_loop else None,
            "adapter": self.adapter.descriptor(),
            "poll_interval_s": self.poll_interval_s,
        }


# --------------------------------------------------------------------------------------
# FleetManager
# --------------------------------------------------------------------------------------
class FleetManager:
    """所有站點的註冊表與生命週期控制點。"""

    def __init__(self, store: Store, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self._lock = threading.RLock()
        self._sites: dict[str, SiteRuntime] = {}
        # 全 fleet 共用一個 ForecastService：TabFM 權重載入約 8 秒，
        # 每個站點各載一份既慢又佔記憶體。
        self.prediction = ForecastService()
        self._subscribers: list[Callable[[str, str, dict[str, Any]], None]] = []

    # ------------------------------------------------------------------ 註冊
    def register_site(
        self,
        site_id: str,
        name: str,
        *,
        region: str = "",
        adapter_kind: str = "simulated",
        adapter_config: dict[str, Any] | None = None,
        autopilot: bool = True,
        poll_interval_s: float = 2.0,
        enabled: bool = True,
    ) -> dict[str, Any]:
        now = utcnow()
        config = dict(adapter_config or {})
        config.setdefault("autopilot", autopilot)
        config.setdefault("poll_interval_s", poll_interval_s)
        with self.store.write() as conn:
            conn.execute(
                """
                INSERT INTO sites (site_id, name, region, adapter_kind, adapter_config,
                                   enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(site_id) DO UPDATE SET
                    name = excluded.name, region = excluded.region,
                    adapter_kind = excluded.adapter_kind,
                    adapter_config = excluded.adapter_config,
                    enabled = excluded.enabled, updated_at = excluded.updated_at
                """,
                (site_id, name, region, adapter_kind, dump_json(config), int(enabled), now, now),
            )
        return self.site_record(site_id) or {}

    def site_record(self, site_id: str) -> dict[str, Any] | None:
        return self.store.query_one("SELECT * FROM sites WHERE site_id = ?", (site_id,))

    def site_records(self) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM sites ORDER BY region, site_id")

    def remove_site(self, site_id: str) -> None:
        runtime = self._sites.pop(site_id, None)
        if runtime:
            runtime.stop()
        with self.store.write() as conn:
            conn.execute("DELETE FROM sites WHERE site_id = ?", (site_id,))

    # ------------------------------------------------------------------ 執行期
    def start_all(self) -> None:
        for record in self.site_records():
            if not record["enabled"]:
                continue
            try:
                self.runtime(record["site_id"]).start()
            except NotImplementedError:
                # 尚未實作的 adapter（OPC-UA / MQTT）：站點保留在清單上但不啟動。
                continue

    def stop_all(self) -> None:
        with self._lock:
            runtimes = list(self._sites.values())
        for runtime in runtimes:
            runtime.stop()

    def runtime(self, site_id: str) -> SiteRuntime:
        with self._lock:
            runtime = self._sites.get(site_id)
            if runtime is not None:
                return runtime
            record = self.site_record(site_id)
            if record is None:
                raise KeyError(site_id)
            config = load_json(record.get("adapter_config"), {})
            record = {
                **record,
                "autopilot": config.get("autopilot", True),
                "poll_interval_s": config.get("poll_interval_s", 2.0),
            }
            runtime = SiteRuntime(
                record, self.store, self.settings, self.prediction, on_event=self._fanout
            )
            self._sites[site_id] = runtime
            return runtime

    def active_runtimes(self) -> list[SiteRuntime]:
        with self._lock:
            return list(self._sites.values())

    def has_runtime(self, site_id: str) -> bool:
        with self._lock:
            return site_id in self._sites

    # ------------------------------------------------------------------ 事件訂閱
    def subscribe(self, callback: Callable[[str, str, dict[str, Any]], None]) -> None:
        self._subscribers.append(callback)

    def _fanout(self, site_id: str, stage: str, payload: dict[str, Any]) -> None:
        for callback in list(self._subscribers):
            try:
                callback(site_id, stage, payload)
            except Exception:
                pass

    # ------------------------------------------------------------------ 總覽
    def overview(self) -> dict[str, Any]:
        """Fleet 首頁：所有站點的彙總。"""
        sites: list[dict[str, Any]] = []
        for record in self.site_records():
            site_id = record["site_id"]
            if self.has_runtime(site_id):
                sites.append(self.runtime(site_id).summary())
                continue
            # 尚未啟動的站點也要出現在清單上，狀態標為 stopped。
            config = load_json(record.get("adapter_config"), {})
            counts = {
                row["severity"]: row["n"]
                for row in self.store.query(
                    """
                    SELECT severity, COUNT(*) AS n FROM alarms
                    WHERE site_id = ? AND state IN ('open','acknowledged')
                    GROUP BY severity
                    """,
                    (site_id,),
                )
            }
            sites.append({
                "site_id": site_id,
                "name": record["name"],
                "region": record.get("region", ""),
                "status": "stopped" if record["enabled"] else "disabled",
                "autopilot": config.get("autopilot", True),
                "busy": False,
                "simulated": record["adapter_kind"] == "simulated",
                "adapter_kind": record["adapter_kind"],
                "connected": False,
                "last_poll_at": None,
                "last_error": None,
                "tick": 0,
                "factory_health": 0.0,
                "production_pct": 0.0,
                "max_delay_min": 0.0,
                "hazard_exposure_min": 0.0,
                "machines_down": 0,
                "machine_count": 0,
                "alarms": {
                    "critical": counts.get("critical", 0),
                    "warning": counts.get("warning", 0),
                    "info": counts.get("info", 0),
                    "total": sum(counts.values()),
                },
                "pending_approval": False,
            })

        running = [s for s in sites if s["status"] in ("running", "handling")]
        total_alarms = sum(s["alarms"]["total"] for s in sites)
        critical = sum(s["alarms"]["critical"] for s in sites)
        health_values = [s["factory_health"] for s in running] or [0.0]
        production_values = [s["production_pct"] for s in running] or [0.0]
        open_wo = self.store.scalar(
            "SELECT COUNT(*) FROM work_orders WHERE state IN ('open','in_progress')", default=0
        )
        return {
            "sites": sites,
            "totals": {
                "site_count": len(sites),
                "sites_running": len(running),
                "sites_with_alarms": sum(1 for s in sites if s["alarms"]["total"] > 0),
                "alarms_total": total_alarms,
                "alarms_critical": critical,
                "pending_approvals": sum(1 for s in sites if s["pending_approval"]),
                "open_work_orders": int(open_wo),
                "avg_factory_health": round(sum(health_values) / len(health_values), 1),
                "avg_production_pct": round(sum(production_values) / len(production_values), 1),
                "machines_down": sum(s["machines_down"] for s in sites),
                "simulated_sites": sum(1 for s in sites if s["simulated"]),
            },
            "generated_at": utcnow(),
        }


__all__ = ["FleetManager", "PendingApproval", "SiteRuntime"]
