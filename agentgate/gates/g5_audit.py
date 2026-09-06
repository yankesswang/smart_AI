"""G5 執行與封存(Audit)— 不可竄改的稽核鏈(規格 §4.6)。

每筆紀錄含前一筆的雜湊,事後竄改可被偵測。單機即可實作,不需區塊鏈——
區塊鏈的成本與延遲對此場景不成比例;雜湊鏈已足以提供竄改偵測,
不可否認性由「核准者身分綁定 + 完整證據封存」提供。

三個承諾:
1. 雜湊鏈:竄改任何一筆,之後所有雜湊都對不上。
2. 完整性:每個被執行的動作都必須能回溯到裁決(與核准,若需要)。
3. 否決與降級也留痕:被擋下的動作和被執行的動作留下同等完整的紀錄。

**外部錨定(anchor)** 補上雜湊鏈單獨做不到的那一件事。純雜湊鏈只證明
「鏈是自洽的」:一個有寫入權限的管理員可以改掉第 3 筆、再把第 4 筆之後的
雜湊全部重算一遍,鏈就又自洽了 —— ``verify()`` 什麼都抓不到。

``anchor()`` 定期把 (鏈長, head hash, 時間戳) 寫進一份 append-only 的錨點檔,
並以 HMAC-SHA256 簽名。金鑰來自環境變數,真實導入時由 KMS/HSM 保管,
**與資料庫的管理權限分離**。於是整條重寫必然在錨點上露餡:攻擊者算得出新的
鏈雜湊,但簽不出對應的錨點簽章(他沒有金鑰),而舊錨點指向的 head hash
和新鏈對不上。

為什麼這樣就夠、不需要區塊鏈:法遵要的是**可偵測與可舉證**,不是
**技術上不可能竄改**。錨點檔可以另存一份到 SIEM、第三方保管或稽核單位信箱 ——
一旦送出去就不在管理員的控制範圍內。區塊鏈買的是「無需信任任何單一保管方」,
而電信業的稽核本來就有既存的保管方(稽核室、主管機關),
為此付出的成本與延遲不成比例。誠實的邊界:攻擊者若同時握有錨點金鑰
**且**能改掉所有外部副本,這道防線就失效 —— 這是金鑰保管的假設,不是雜湊的假設。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64

# 外部錨定的簽章金鑰。真實導入時由 KMS/HSM 保管,與應用程式資料庫的
# 管理權限分離;Demo 沒設環境變數時用固定金鑰,並在輸出標示 demo_key = True。
ANCHOR_KEY_ENV = "AGENTGATE_ANCHOR_KEY"
DEMO_ANCHOR_KEY = "agentgate-demo-anchor-key-not-for-production"


def _anchor_key() -> tuple[bytes, bool]:
    """回傳 (金鑰, 是否為 demo 金鑰)。"""
    key = os.environ.get(ANCHOR_KEY_ENV)
    if key:
        return key.encode("utf-8"), False
    return DEMO_ANCHOR_KEY.encode("utf-8"), True


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
        self._anchor_path: Path | None = None
        self.anchors: list[dict[str, Any]] = []
        if persist_dir is not None:
            persist_dir.mkdir(parents=True, exist_ok=True)
            self._path = persist_dir / f"gate-audit-{self.run_id}.jsonl"
            self._anchor_path = persist_dir / f"gate-anchors-{self.run_id}.jsonl"

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def anchor_path(self) -> Path | None:
        return self._anchor_path

    # -- 外部錨定 ---------------------------------------------------------------------
    @staticmethod
    def _sign(payload: dict[str, Any]) -> tuple[str, bool]:
        key, is_demo = _anchor_key()
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest(), is_demo

    def head(self) -> str:
        return self.records[-1].hash if self.records else GENESIS_HASH

    def anchor(self, at: str | None = None) -> dict[str, Any]:
        """把當前 head hash、鏈長、時間戳寫進 append-only 錨點檔並簽名。

        錨點只增不改。真實部署會定期(每 N 筆或每 N 分鐘)呼叫它,
        並把錨點檔複製到治理層自己改不到的地方。
        """
        with self._lock:
            payload = {
                "run_id": self.run_id,
                "anchor_seq": len(self.anchors),
                "ts": at or _now(),
                "chain_length": len(self.records),
                "head_hash": self.records[-1].hash if self.records else GENESIS_HASH,
            }
            signature, is_demo = self._sign(payload)
            record = {**payload, "sig": signature, "alg": "HMAC-SHA256",
                      "demo_key": is_demo}
            self.anchors.append(record)
            if self._anchor_path is not None:
                with self._anchor_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            return record

    def _verify_anchors(self) -> dict[str, Any] | None:
        """檢查每個錨點的簽章,以及鏈在該長度時的 head hash 是否吻合。

        回傳 None = 全部通過;否則回傳第一個對不上的錨點的說明。
        """
        for anchor in self.anchors:
            payload = {k: anchor[k] for k in
                       ("run_id", "anchor_seq", "ts", "chain_length", "head_hash")}
            expected_sig, _ = self._sign(payload)
            if not hmac.compare_digest(expected_sig, str(anchor.get("sig", ""))):
                return {
                    "ok": False, "checked": len(self.records), "broken_at_seq": None,
                    "reason": (f"錨點 #{anchor['anchor_seq']} 的簽章驗證失敗 —— "
                               "錨點本身遭偽造或金鑰不符。"),
                    "anchor_seq": anchor["anchor_seq"],
                }
            length = int(anchor["chain_length"])
            if length > len(self.records):
                return {
                    "ok": False, "checked": len(self.records), "broken_at_seq": None,
                    "reason": (f"錨點 #{anchor['anchor_seq']} 記錄鏈長 {length},"
                               f"但現存鏈只有 {len(self.records)} 筆 —— 有紀錄被刪除。"),
                    "anchor_seq": anchor["anchor_seq"],
                }
            actual = self.records[length - 1].hash if length else GENESIS_HASH
            if actual != anchor["head_hash"]:
                return {
                    "ok": False, "checked": len(self.records),
                    "broken_at_seq": length - 1 if length else None,
                    "reason": (f"錨點 #{anchor['anchor_seq']} 記錄第 {length} 筆時的 "
                               f"head hash 為 {anchor['head_hash'][:16]}…,"
                               f"現存鏈為 {actual[:16]}… —— 鏈被整條重寫。"),
                    "anchor_seq": anchor["anchor_seq"],
                }
        return None

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
                            "broken_at_seq": record.seq, "anchors": len(self.anchors),
                            "reason": f"第 {record.seq} 筆的 prev_hash 與前一筆不符(鏈接斷裂)"}
                if record.compute_hash() != record.hash:
                    return {"ok": False, "checked": len(self.records),
                            "broken_at_seq": record.seq, "anchors": len(self.anchors),
                            "reason": f"第 {record.seq} 筆內容與其雜湊不符(內容遭竄改)"}
                prev = record.hash
            anchor_problem = self._verify_anchors()
            if anchor_problem is not None:
                return {**anchor_problem, "anchors": len(self.anchors)}
            return {"ok": True, "checked": len(self.records), "broken_at_seq": None,
                    "anchors": len(self.anchors),
                    "reason": ("雜湊鏈完整,未偵測到竄改"
                               + (f";並與 {len(self.anchors)} 個外部錨點一致"
                                  if self.anchors else "(尚未建立外部錨點)"))}

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

    def rewrite_for_demo(self, seq: int, new_detail: dict[str, Any]) -> bool:
        """Demo 專用:竄改一筆之後**把整條鏈重算一遍**,讓鏈內驗證再度自洽。

        這正是「管理員自己改整條鏈」的攻擊。``verify()`` 的鏈內檢查會通過 ——
        只有外部錨點抓得到,因為攻擊者簽不出新的錨點簽章。
        """
        with self._lock:
            if not (0 <= seq < len(self.records)):
                return False
            self.records[seq].detail = new_detail
            prev = GENESIS_HASH
            for record in self.records:
                record.prev_hash = prev
                record.hash = record.compute_hash()
                prev = record.hash
            return True

    def reset(self) -> None:
        with self._lock:
            self.records = []
            self.anchors = []


__all__ = ["ANCHOR_KEY_ENV", "AuditChain", "ChainRecord", "DEMO_ANCHOR_KEY", "GENESIS_HASH"]
