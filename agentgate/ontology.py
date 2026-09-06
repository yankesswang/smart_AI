"""動作本體論(規格 §4.1)與來源信任分級(§4.2)的核心型別。

治理的前提是動作必須是結構化的一等公民,不能是自由文字。
這個模組刻意不依賴 factory_guardian.domain —— AgentGate 是獨立的治理核心。
"""

from __future__ import annotations

import hashlib
import json
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


# 可被「確認提升」的不可信通道(G0-R2)。memory 不在其中:
# 記憶是 Agent 自己寫的,讓它被確認等於讓 Agent 自己給自己背書。
LIFTABLE_CHANNELS: set[Channel] = {Channel.TOOL_OUTPUT, Channel.USER_UNVERIFIED}

# 有資格擔任「確認者」的通道。必須本身就是 verified 等級 ——
# 一份文件不能確認另一份文件。
CONFIRMER_CHANNELS: set[Channel] = {Channel.USER_VERIFIED, Channel.SYSTEM}


def action_fingerprint(kind: "ActionKind | str", params: dict[str, Any]) -> str:
    """動作指紋:動作種類 + 全部參數的確定性雜湊。

    為什麼要有這個東西:確認提升的前提是「用戶確認的是**這一個**動作與**這一組**參數」。
    只比對「用戶有沒有說好」是不夠的 —— 攻擊者可以讓用戶對「退費 880 元到本人帳戶」
    說好,Agent 卻送出「退費 880 元到 ACC-9999」。指紋涵蓋全部參數,
    任何一個欄位被換掉,指紋就對不上,提升就不成立。

    指紋由 runtime 計算(harness 在向用戶展示動作的當下算一次、送進閘門時再算一次),
    **不由 LLM 自報** —— 見 ``gates/g1_resolution.attach_runtime_provenance``。
    """
    kind_value = kind.value if isinstance(kind, ActionKind) else str(kind)
    canonical = json.dumps(
        {"kind": kind_value, "params": params},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Provenance:
    """一個指令來源節點。攻擊者可以改寫措辭,但改不掉指令從哪個通道進來的事實。

    ``confirms`` / ``confirmed_action_hash`` 是 G0-R2「確認提升」用的兩個欄位:
    一個已驗證(或系統)節點可以宣告「我確認了 <confirms> 這個來源提出的、指紋為
    <confirmed_action_hash> 的那個動作」。兩個欄位都由 runtime 寫入,
    LLM 自報的一律在 G1 邊界被清掉。
    """

    channel: Channel
    source_ref: str          # 例如 "upload:invoice_20260817.pdf#p3"
    trust: str = ""          # verified / derived / untrusted(留空則由通道推導)
    confirms: str = ""       # 本節點確認了哪一個來源(該來源的 source_ref)
    confirmed_action_hash: str = ""   # 被確認的動作指紋(runtime 計算,見 action_fingerprint)

    def __post_init__(self) -> None:
        if not self.trust:
            object.__setattr__(self, "trust", CHANNEL_TRUST[self.channel])

    @property
    def is_confirmation(self) -> bool:
        """是不是一個「有效形式」的確認節點(內容是否對得上另算)。"""
        return bool(self.confirmed_action_hash) and self.channel in CONFIRMER_CHANNELS

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel.value,
            "source_ref": self.source_ref,
            "trust": self.trust,
            "max_risk": CHANNEL_MAX_RISK[self.channel],
            "confirms": self.confirms,
            "confirmed_action_hash": self.confirmed_action_hash,
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
    # 觸發後把動作風險抬升到這個等級(空字串 = 不抬升)。
    # G2 的規則在 PolicyEngine 內部就處理掉了;這個欄位讓 **G3 的規則**
    # 也能抬升風險而不必在 pipeline 裡寫死規則 ID。
    escalate_to: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "severity": self.severity.value,
            "message": self.message,
            "statute": self.statute,
            "escalate_to": self.escalate_to,
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
    # 結構化的「用戶已知悉」事實,由 runtime 記錄(例如:已於通話中告知違約金並取得同意)。
    # 刻意**不**參與 G2/G3 的確定性裁決 —— 那兩層只看動作、參數與帳戶狀態。
    # 它只出現在 G4 的證據包裡供人(或模擬核准者)判斷,理由見實作說明「知悉是欄位不是文字」。
    acknowledgements: dict[str, Any] = field(default_factory=dict)
    # G1 在清洗 LLM 自報 provenance 時產生的 finding(嚴重度 info),
    # 由 pipeline 併入裁決紀錄 —— 「以 runtime 為準」這件事本身要留痕。
    runtime_findings: list[Finding] = field(default_factory=list)

    def fingerprint(self) -> str:
        """本次動作的指紋(runtime 計算)。確認提升與客戶授權判定都以它為準。"""
        return action_fingerprint(self.kind, self.params)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "fingerprint": self.fingerprint(),
            "acknowledgements": self.acknowledgements,
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
    risk_cap: str             # 鏈可授權的最高風險等級(已計入確認提升)
    weakest_link: Provenance | None
    chain: list[Provenance]
    # 與 chain 等長:每個節點「經確認提升後」實際可授權的風險上限。
    effective_caps: list[str] = field(default_factory=list)
    # 實際發生的提升紀錄(G0-R2)。空 list = 這次裁決沒有任何節點被提升。
    lifts: list[dict[str, Any]] = field(default_factory=list)
    # 形式上是確認節點、但指紋對不上這次動作的節點(攻擊訊號:確認了 A 卻拿去做 B)。
    stale_confirmations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        caps = self.effective_caps or [CHANNEL_MAX_RISK[p.channel] for p in self.chain]
        return {
            "min_trust": self.min_trust,
            "risk_cap": self.risk_cap,
            "weakest_link": self.weakest_link.to_dict() if self.weakest_link else None,
            "chain": [
                {**p.to_dict(), "effective_max_risk": cap}
                for p, cap in zip(self.chain, caps)
            ],
            "lifts": self.lifts,
            "stale_confirmations": self.stale_confirmations,
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
    # 連鎖後果:這個動作執行後會被**其他流程**接手做的事。
    # 每一項 {"process", "label", "service_interruption"};
    # 只要有一項 service_interruption 為真,G3 的 AG-32 就會把它升成需核准。
    cascade: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "affected_subjects": self.affected_subjects[:20],
            "affected_count": self.affected_count,
            "financial_delta": self.financial_delta,
            "reversible": self.reversible,
            "reversal_window_hours": self.reversal_window_hours,
            "pii_fields_exposed": self.pii_fields_exposed,
            "cascade": self.cascade,
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
    "CONFIRMER_CHANNELS",
    "LIFTABLE_CHANNELS",
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
    "action_fingerprint",
    "risk_exceeds",
    "risk_max",
]
