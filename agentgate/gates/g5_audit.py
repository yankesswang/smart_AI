"""G5 執行與封存(Audit)— 不可竄改的稽核鏈(規格 §4.6)。

每筆紀錄含前一筆的雜湊,事後竄改可被偵測。單機即可實作,不需區塊鏈——
區塊鏈的成本與延遲對此場景不成比例;雜湊鏈已足以提供竄改偵測,
不可否認性由「核准者身分綁定 + 完整證據封存」提供。

三個承諾:
1. 雜湊鏈:竄改任何一筆,之後所有雜湊都對不上。
2. 完整性:每個被執行的動作都必須能回溯到裁決(與核准,若需要)。
3. 否決與降級也留痕:被擋下的動作和被執行的動作留下同等完整的紀錄。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ChainRecord:
    seq: int
    ts: str
    trace_id: str
    action_id: str
    stage: str        # g0_trust / g1_resolution / g2_adjudication / g3_projection /
                      # g4_pending / g4_approved / g4_rejected / g5_executed / g5_blocked
    actor: str
    detail: dict[str, Any]
    prev_hash: str
    case_id: str = ""   # 這道動作出自哪一件工單(可為空:Demo 導播等系統事件)
    hash: str = ""

    def payload(self) -> dict[str, Any]:
        """參與雜湊的欄位(不含 hash 本身)。"""
        return {
            "seq": self.seq,
            "ts": self.ts,
            "trace_id": self.trace_id,
            "action_id": self.action_id,
            "case_id": self.case_id,
            "stage": self.stage,
            "actor": self.actor,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
        }

    def compute_hash(self) -> str:
        canonical = json.dumps(self.payload(), ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        data = self.payload()
        data["hash"] = self.hash
        return data


class AuditChain:
    """執行緒安全的雜湊鏈稽核紀錄器,可選擇落地 JSONL。"""

    def __init__(self, run_id: str | None = None, persist_dir: Path | None = None) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.run_id = run_id or f"ag-{stamp}-{uuid.uuid4().hex[:6]}"
        self.records: list[ChainRecord] = []
        self._lock = threading.Lock()
        self._path: Path | None = None
        if persist_dir is not None:
            persist_dir.mkdir(parents=True, exist_ok=True)
            self._path = persist_dir / f"gate-audit-{self.run_id}.jsonl"

    @property
    def path(self) -> Path | None:
        return self._path

    def append(self, *, trace_id: str, action_id: str, stage: str, actor: str,
               case_id: str = "", ts: str | None = None, **detail: Any) -> ChainRecord:
        """寫入一筆稽核紀錄。

        ``ts`` 只在回填當班歷史時傳入(見 ``console.OpsSimulator``):真實系統的
        稽核鏈時間戳是事件發生的時間,不是寫入的時間。線上路徑一律留空。
        """
        with self._lock:
            prev_hash = self.records[-1].hash if self.records else GENESIS_HASH
            record = ChainRecord(
                seq=len(self.records), ts=ts or _now(), trace_id=trace_id,
                action_id=action_id, stage=stage, actor=actor,
                detail=detail, prev_hash=prev_hash, case_id=case_id,
            )
            record.hash = record.compute_hash()
            self.records.append(record)
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record.to_dict(), ensure_ascii=False, default=str) + "\n")
            return record

    # -- 完整性驗證 -------------------------------------------------------------------
    def verify(self) -> dict[str, Any]:
        """驗證雜湊鏈:重算每筆雜湊並檢查鏈接。回傳第一個斷點(若有)。"""
        with self._lock:
            prev = GENESIS_HASH
            for record in self.records:
                if record.prev_hash != prev:
                    return {"ok": False, "checked": len(self.records),
                            "broken_at_seq": record.seq,
                            "reason": f"第 {record.seq} 筆的 prev_hash 與前一筆不符(鏈接斷裂)"}
                if record.compute_hash() != record.hash:
                    return {"ok": False, "checked": len(self.records),
                            "broken_at_seq": record.seq,
                            "reason": f"第 {record.seq} 筆內容與其雜湊不符(內容遭竄改)"}
                prev = record.hash
            return {"ok": True, "checked": len(self.records), "broken_at_seq": None,
                    "reason": "雜湊鏈完整,未偵測到竄改"}

    def completeness(self) -> dict[str, Any]:
        """稽核完整率 AC:每個被執行的動作都必須能回溯到裁決(與核准,若需要)。"""
        with self._lock:
            by_action: dict[str, set[str]] = {}
            approval_required: set[str] = set()
            for record in self.records:
                by_action.setdefault(record.action_id, set()).add(record.stage)
                if record.stage == "g2_adjudication" and record.detail.get("requires_approval"):
                    approval_required.add(record.action_id)
            executed = [a for a, stages in by_action.items() if "g5_executed" in stages]
            traceable = []
            for action_id in executed:
                stages = by_action[action_id]
                if "g2_adjudication" not in stages:
                    continue
                if action_id in approval_required and "g4_approved" not in stages:
                    continue
                traceable.append(action_id)
            total = len(executed)
            return {
                "executed": total,
                "traceable": len(traceable),
                "audit_completeness": (len(traceable) / total) if total else 1.0,
                "gaps": sorted(set(executed) - set(traceable)),
            }

    # -- 查詢 -------------------------------------------------------------------------
    def to_list(self, limit: int | None = None, trace_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = self.records
            if trace_id:
                records = [r for r in records if r.trace_id == trace_id]
            data = [r.to_dict() for r in records]
        return data[-limit:] if limit else data

    def tamper_for_demo(self, seq: int, new_detail: dict[str, Any]) -> bool:
        """Demo 專用:直接竄改一筆紀錄的內容,讓 verify() 當場抓出來。

        這個方法存在的唯一目的,是在簡報現場證明雜湊鏈驗證真的有效。
        """
        with self._lock:
            if not (0 <= seq < len(self.records)):
                return False
            self.records[seq].detail = new_detail
            return True

    def reset(self) -> None:
        with self._lock:
            self.records = []


__all__ = ["AuditChain", "ChainRecord", "GENESIS_HASH"]
