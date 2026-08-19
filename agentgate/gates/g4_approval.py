"""G4 人工核准(Approval)。

核准介面是產品,不是附屬品:主管必須能在 15 秒內做出有依據的決定(規格 §4.5)。
證據包包含:動作內容、影響範圍、可回復性、觸發規則(ID + 條文原文)、
指令來源鏈、預演結果、以及明確標示為未經驗證的 Agent 推理摘要。

核准者身分必須經過驗證並與動作綁定 —— Demo 以「門號綁定碼」模擬
中華電信門號級身分驗證(規格 §9);真實導入時由 CHT 身分識別服務取代。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..ontology import ActionRequest, Evidence, Finding, Projection, TrustVerdict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


# 核准時限(秒)。風險越高、時限越短 —— 高風險動作卡在佇列本身就是營運損失,
# 所以「多久內必須有人決定」跟「誰能決定」一樣是政策的一部分。
SLA_SECONDS: dict[str, int] = {
    "low": 1_800, "medium": 900, "high": 300, "forbidden": 300, "unresolved": 900,
}

# Demo 核准者名冊:credential 模擬門號級身分綁定碼(SIM 綁定,推播可轉發、SIM 不行)
APPROVERS: dict[str, dict[str, str]] = {
    "MGR-001": {"name": "值班主管 王主任", "credential": "sim-0911-000-111", "role": "duty_manager"},
    "MGR-002": {"name": "帳務主管 李經理", "credential": "sim-0922-000-222", "role": "billing_manager"},
    "MGR-003": {"name": "夜班主管 陳副理", "credential": "sim-0933-000-333", "role": "duty_manager"},
}


@dataclass
class PendingApproval:
    request: ActionRequest
    risk: str
    findings: list[Finding]
    projection: Projection | None
    trust: TrustVerdict | None
    evidence: list[Evidence]
    approval_id: str = field(default_factory=lambda: f"apr-{uuid.uuid4().hex[:10]}")
    created_at: str = field(default_factory=_now)
    status: str = "pending"                 # pending / approved / rejected
    approver_id: str | None = None
    approver_name: str | None = None
    reason: str = ""
    decided_at: str | None = None

    # -- SLA ---------------------------------------------------------------------------
    @property
    def sla_seconds(self) -> int:
        return SLA_SECONDS.get(self.risk, 900)

    def age_seconds(self, now: datetime | None = None) -> float:
        """待決時間;已決行者為送件到決行的耗時。"""
        start = _parse(self.created_at)
        if start is None:
            return 0.0
        end = _parse(self.decided_at) if self.decided_at else None
        if end is None:
            end = now or datetime.now(timezone.utc)
        return max(0.0, (end - start).total_seconds())

    def sla_state(self, now: datetime | None = None) -> dict[str, Any]:
        age = self.age_seconds(now)
        return {
            "sla_seconds": self.sla_seconds,
            "age_seconds": round(age, 1),
            "remaining_seconds": round(self.sla_seconds - age, 1),
            "breached": age > self.sla_seconds,
        }

    def evidence_package(self, now: datetime | None = None) -> dict[str, Any]:
        """15 秒決策所需的完整證據包(規格 §4.5)。"""
        return {
            "approval_id": self.approval_id,
            "created_at": self.created_at,
            "status": self.status,
            "action": self.request.to_dict(),
            "case": self.request.context,
            "risk": self.risk,
            "sla": self.sla_state(now),
            "findings": [f.to_dict() for f in self.findings],
            "projection": self.projection.to_dict() if self.projection else None,
            "provenance": self.trust.to_dict() if self.trust else None,
            "evidence": [e.to_dict() for e in self.evidence],
            "agent_reasoning": {
                "text": self.request.reasoning,
                "warning": "以下為未經驗證的模型輸出,不得作為核准唯一依據。",
            },
            "approver_id": self.approver_id,
            "approver_name": self.approver_name,
            "reason": self.reason,
            "decided_at": self.decided_at,
        }


class ApprovalQueue:
    """待核准佇列。核准/駁回都必須綁定經驗證的核准者身分。"""

    def __init__(self, approvers: dict[str, dict[str, str]] | None = None) -> None:
        self.approvers = approvers if approvers is not None else dict(APPROVERS)
        self._items: dict[str, PendingApproval] = {}

    def enqueue(self, item: PendingApproval) -> PendingApproval:
        self._items[item.approval_id] = item
        return item

    def get(self, approval_id: str) -> PendingApproval | None:
        return self._items.get(approval_id)

    def pending(self) -> list[PendingApproval]:
        return [i for i in self._items.values() if i.status == "pending"]

    def all_items(self) -> list[PendingApproval]:
        return list(self._items.values())

    def verify_approver(self, approver_id: str, credential: str) -> tuple[bool, str]:
        """驗證核准者身分(Demo:門號綁定碼比對;真實導入:CHT 門號級驗證)。"""
        record = self.approvers.get(approver_id)
        if record is None:
            return False, f"未知核准者 {approver_id}"
        if record["credential"] != credential:
            return False, "身分憑證驗證失敗:綁定碼不符"
        return True, record["name"]

    def decide(
        self,
        approval_id: str,
        approved: bool,
        approver_id: str,
        credential: str,
        reason: str,
        at: str | None = None,
    ) -> tuple[PendingApproval | None, str]:
        """簽核。回傳 (紀錄, 錯誤訊息);成功時錯誤訊息為空字串。

        ``at`` 同 ``AuditChain.append``:僅供回填當班歷史使用。
        """
        item = self._items.get(approval_id)
        if item is None:
            return None, f"找不到待核准項目 {approval_id}"
        if item.status != "pending":
            return None, f"項目 {approval_id} 已於 {item.decided_at} 決定為 {item.status}"
        if not approved and not reason.strip():
            return None, "駁回必須填寫理由"
        ok, name_or_error = self.verify_approver(approver_id, credential)
        if not ok:
            return None, name_or_error
        item.status = "approved" if approved else "rejected"
        item.approver_id = approver_id
        item.approver_name = name_or_error
        item.reason = reason
        item.decided_at = at or _now()
        return item, ""

    def clear(self) -> None:
        self._items = {}


__all__ = ["APPROVERS", "SLA_SECONDS", "ApprovalQueue", "PendingApproval"]
