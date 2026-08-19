"""動作本體論(規格 §4.1)與來源信任分級(§4.2)的核心型別。

治理的前提是動作必須是結構化的一等公民,不能是自由文字。
這個模組刻意不依賴 factory_guardian.domain —— AgentGate 是獨立的治理核心。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------------------
# 風險等級
# --------------------------------------------------------------------------------------
RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "forbidden": 3}


def risk_max(*levels: str) -> str:
    """取多個風險等級中最高者。"""
    return max(levels, key=lambda lv: RISK_ORDER[lv])


def risk_exceeds(risk: str, cap: str) -> bool:
    """動作風險是否超過授權上限。"""
    return RISK_ORDER[risk] > RISK_ORDER[cap]


# --------------------------------------------------------------------------------------
# 動作種類(規格 §4.1)
# --------------------------------------------------------------------------------------
class ActionKind(str, Enum):
    # 讀取類
    READ_ACCOUNT = "read_account"        # 查詢本人帳務
    READ_BULK = "read_bulk"              # 批次查詢／匯出
    # 金流類
    ISSUE_REFUND = "issue_refund"        # 退費
    CHANGE_PLAN = "change_plan"          # 變更資費
    # 服務類
    SUSPEND_SERVICE = "suspend_service"  # 停話
    REISSUE_SIM = "reissue_sim"          # 補發 SIM
    # 治理類
    POLICY_OVERRIDE = "policy_override"  # 繞過治理(永久禁止)


# --------------------------------------------------------------------------------------
# 指令來源通道與信任分級(規格 §4.2)
# --------------------------------------------------------------------------------------
class Channel(str, Enum):
    USER_VERIFIED = "user_verified"      # 已完成身分驗證的用戶指令
    USER_UNVERIFIED = "user_unverified"  # 未驗證的用戶指令
    TOOL_OUTPUT = "tool_output"          # 工具回傳內容(文件、網頁、DB)
    MEMORY = "memory"                    # Agent 自身記憶／歷史
    SYSTEM = "system"                    # 系統提示


# 各通道的信任等級與可授權的最高風險等級。
# G0-R1 權限升級阻斷:動作風險不得高於指令來源鏈中「最低信任通道」所能授權的等級。
CHANNEL_TRUST: dict[Channel, str] = {
    Channel.SYSTEM: "verified",
    Channel.USER_VERIFIED: "verified",
    Channel.MEMORY: "derived",
    Channel.USER_UNVERIFIED: "untrusted",
    Channel.TOOL_OUTPUT: "untrusted",
}

CHANNEL_MAX_RISK: dict[Channel, str] = {
    Channel.SYSTEM: "high",
    Channel.USER_VERIFIED: "high",
    Channel.MEMORY: "medium",
    Channel.USER_UNVERIFIED: "low",
    Channel.TOOL_OUTPUT: "low",
}


@dataclass(frozen=True)
class Provenance:
    """一個指令來源節點。攻擊者可以改寫措辭,但改不掉指令從哪個通道進來的事實。"""

    channel: Channel
    source_ref: str          # 例如 "upload:invoice_20260817.pdf#p3"
    trust: str = ""          # verified / derived / untrusted(留空則由通道推導)

    def __post_init__(self) -> None:
        if not self.trust:
            object.__setattr__(self, "trust", CHANNEL_TRUST[self.channel])

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel.value,
            "source_ref": self.source_ref,
            "trust": self.trust,
            "max_risk": CHANNEL_MAX_RISK[self.channel],
        }


# --------------------------------------------------------------------------------------
# 證據與規則觸發紀錄
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Evidence:
    source: str        # rule / provenance / projection / shadow / agent
    reference: str     # 規則 ID、來源鏈 ref、預演欄位
    statement: str     # 人讀得懂的一句話
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "reference": self.reference,
            "statement": self.statement,
            "weight": self.weight,
        }


class Severity(str, Enum):
    BLOCK = "block"                        # 直接攔下
    APPROVAL_REQUIRED = "approval_required"  # 需人工核准
    INFO = "info"                          # 僅記錄


@dataclass(frozen=True)
class Finding:
    """一條規則被觸發的紀錄:規則 ID + 條文原文 + 證據。"""

    rule_id: str
    title: str
    severity: Severity
    message: str
    statute: str = ""                     # 條文原文(核准介面要能引用)
    evidence: tuple[Evidence, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "severity": self.severity.value,
            "message": self.message,
            "statute": self.statute,
            "evidence": [e.to_dict() for e in self.evidence],
        }


# --------------------------------------------------------------------------------------
# 動作請求(規格 §6)
# --------------------------------------------------------------------------------------
class PrincipalRole(str, Enum):
    CUSTOMER = "customer"   # 已驗證的用戶本人
    CSR = "csr"             # 客服人員(可代客操作,但需核准)
    OPS = "ops"             # 營運人員(批次作業)


@dataclass
class ActionRequest:
    kind: ActionKind
    params: dict[str, Any]
    principal: str                          # 代表誰執行(帳號 ID)
    agent_id: str
    provenance_chain: list[Provenance]
    principal_role: PrincipalRole = PrincipalRole.CUSTOMER
    reasoning: str = ""                     # Agent 的推理摘要(未經驗證的模型輸出)
    action_id: str = field(default_factory=lambda: f"act-{uuid.uuid4().hex[:10]}")
    trace_id: str = field(default_factory=lambda: f"trace-{uuid.uuid4().hex[:10]}")
    # 工單脈絡(case_id、渠道、客戶、發生時間)。刻意不參與任何裁決:
    # 治理結果只能由動作本身與來源鏈決定,不能因為「這件客訴看起來很急」而放寬。
    # 它的用途是讓稽核與核准介面能回到指令的出處。
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "params": self.params,
            "principal": self.principal,
            "principal_role": self.principal_role.value,
            "agent_id": self.agent_id,
            "provenance_chain": [p.to_dict() for p in self.provenance_chain],
            "reasoning": self.reasoning,
            "trace_id": self.trace_id,
            "context": self.context,
        }


# --------------------------------------------------------------------------------------
# G0 產出:信任裁決
# --------------------------------------------------------------------------------------
@dataclass
class TrustVerdict:
    """指令來源鏈的信任評估結果。"""

    min_trust: str            # 鏈中最低信任等級
    risk_cap: str             # 鏈可授權的最高風險等級
    weakest_link: Provenance | None
    chain: list[Provenance]

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_trust": self.min_trust,
            "risk_cap": self.risk_cap,
            "weakest_link": self.weakest_link.to_dict() if self.weakest_link else None,
            "chain": [p.to_dict() for p in self.chain],
        }


# --------------------------------------------------------------------------------------
# G3 產出:後果預演(規格 §4.4)
# --------------------------------------------------------------------------------------
@dataclass
class Projection:
    affected_subjects: list[str]        # 受影響的用戶／帳號
    affected_count: int
    financial_delta: float              # 金流影響(負值 = 公司支出)
    reversible: bool                    # 是否可回復
    reversal_window_hours: int | None
    pii_fields_exposed: list[str]       # 觸及的個資欄位

    def to_dict(self) -> dict[str, Any]:
        return {
            "affected_subjects": self.affected_subjects[:20],
            "affected_count": self.affected_count,
            "financial_delta": self.financial_delta,
            "reversible": self.reversible,
            "reversal_window_hours": self.reversal_window_hours,
            "pii_fields_exposed": self.pii_fields_exposed,
        }


# --------------------------------------------------------------------------------------
# 最終裁決(規格 §6)
# --------------------------------------------------------------------------------------
@dataclass
class GateVerdict:
    action_id: str
    allowed: bool
    requires_approval: bool
    risk: str
    status: str                          # executed / pending_approval / blocked / rejected
    gate_blocked_at: str | None          # G0..G5;未被攔則 None
    reasons: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    projection: Projection | None = None
    trust: TrustVerdict | None = None
    evidence: list[Evidence] = field(default_factory=list)
    audit_ref: str = ""
    approval_id: str | None = None
    execution_result: dict[str, Any] | None = None
    decision_latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "risk": self.risk,
            "status": self.status,
            "gate_blocked_at": self.gate_blocked_at,
            "reasons": self.reasons,
            "findings": [f.to_dict() for f in self.findings],
            "projection": self.projection.to_dict() if self.projection else None,
            "trust": self.trust.to_dict() if self.trust else None,
            "evidence": [e.to_dict() for e in self.evidence],
            "audit_ref": self.audit_ref,
            "approval_id": self.approval_id,
            "execution_result": self.execution_result,
            "decision_latency_ms": round(self.decision_latency_ms, 3),
        }


__all__ = [
    "ActionKind",
    "ActionRequest",
    "CHANNEL_MAX_RISK",
    "CHANNEL_TRUST",
    "Channel",
    "Evidence",
    "Finding",
    "GateVerdict",
    "PrincipalRole",
    "Projection",
    "Provenance",
    "RISK_ORDER",
    "Severity",
    "TrustVerdict",
    "risk_exceeds",
    "risk_max",
]
