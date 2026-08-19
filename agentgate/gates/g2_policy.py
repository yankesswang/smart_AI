"""G2 政策裁決(Adjudication)。

這一層是**規則**,不是 LLM(規格 §4.3)。三個理由:
1. 可稽核性:主管機關要的是「依據第 X 條規則」,不是「模型認為風險較高」。
2. 不可繞過:LLM 判斷可被提示詞影響;確定性規則不能。
3. 可重現:同樣輸入必然同樣輸出。

風險分級不是靜態常數,而是動作 × 範圍 × 主體的函式(規格 §4.1)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..ontology import (
    ActionKind,
    ActionRequest,
    Evidence,
    Finding,
    PrincipalRole,
    Severity,
    risk_max,
)
from ..shadow import (
    BULK_FORBIDDEN_COUNT,
    REFUND_MONTHLY_LIMIT,
    REFUND_SINGLE_LIMIT,
    ShadowTelecomEnv,
)


# --------------------------------------------------------------------------------------
# 動作基礎權限表(規格 §4.1)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ActionPolicy:
    kind: ActionKind
    base_risk: str               # low / medium / high / forbidden
    requires_approval: bool
    forbidden: bool = False
    note: str = ""


ACTION_POLICY: dict[ActionKind, ActionPolicy] = {
    ActionKind.READ_ACCOUNT: ActionPolicy(
        ActionKind.READ_ACCOUNT, "low", False, note="查詢本人帳務:可自動"),
    ActionKind.READ_BULK: ActionPolicy(
        ActionKind.READ_BULK, "high", True, note="批次查詢/匯出:含個資,一律需核准"),
    ActionKind.ISSUE_REFUND: ActionPolicy(
        ActionKind.ISSUE_REFUND, "medium", True, note="退費:金流動作,一律需核准"),
    ActionKind.CHANGE_PLAN: ActionPolicy(
        ActionKind.CHANGE_PLAN, "medium", False, note="變更資費:一般情況可自動"),
    ActionKind.SUSPEND_SERVICE: ActionPolicy(
        ActionKind.SUSPEND_SERVICE, "high", True, note="停話:服務中斷,一律需核准"),
    ActionKind.REISSUE_SIM: ActionPolicy(
        ActionKind.REISSUE_SIM, "high", True, note="補發 SIM:帳號接管主要途徑,一律需核准"),
    ActionKind.POLICY_OVERRIDE: ActionPolicy(
        ActionKind.POLICY_OVERRIDE, "forbidden", True, forbidden=True,
        note="繞過治理:任何角色皆不得執行"),
}


# --------------------------------------------------------------------------------------
# 規則協定
# --------------------------------------------------------------------------------------
@dataclass
class RuleContext:
    request: ActionRequest
    shadow: ShadowTelecomEnv

    @property
    def subject(self) -> str:
        """受影響主體(帳戶)。批次動作無單一主體,回傳空字串。"""
        if self.request.kind is ActionKind.READ_BULK:
            return ""
        return str(self.request.params.get("account_id", self.request.principal))


@dataclass
class RuleHit:
    message: str
    severity: Severity
    escalate_to: str | None = None       # 觸發後把風險抬升到這個等級
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(frozen=True)
class GateRule:
    rule_id: str
    title: str
    statute: str                          # 條文原文(核准介面直接引用)
    check: Callable[[RuleContext], RuleHit | None]

    def evaluate(self, ctx: RuleContext) -> tuple[Finding, str | None] | None:
        hit = self.check(ctx)
        if hit is None:
            return None
        finding = Finding(
            rule_id=self.rule_id, title=self.title, severity=hit.severity,
            message=hit.message, statute=self.statute, evidence=tuple(hit.evidence),
        )
        return finding, hit.escalate_to


# --------------------------------------------------------------------------------------
# 規則本體
# --------------------------------------------------------------------------------------
def _rule_policy_override(ctx: RuleContext) -> RuleHit | None:
    if ctx.request.kind is not ActionKind.POLICY_OVERRIDE:
        return None
    return RuleHit(
        message="動作要求繞過治理層,系統禁止。",
        severity=Severity.BLOCK,
        escalate_to="forbidden",
    )


def _rule_cross_subject_customer(ctx: RuleContext) -> RuleHit | None:
    req = ctx.request
    if req.kind is ActionKind.READ_BULK or req.principal_role is not PrincipalRole.CUSTOMER:
        return None
    if ctx.subject == req.principal:
        return None
    return RuleHit(
        message=(
            f"已驗證用戶 {req.principal} 嘗試對他人帳戶 {ctx.subject} 執行 "
            f"{req.kind.value},用戶身分不得操作非本人帳戶。"
        ),
        severity=Severity.BLOCK,
        escalate_to="high",
        evidence=[Evidence("rule", "AG-01",
                           f"受影響主體 {ctx.subject} ≠ 已驗證用戶 {req.principal}")],
    )


def _rule_cross_subject_csr(ctx: RuleContext) -> RuleHit | None:
    req = ctx.request
    if req.kind is ActionKind.READ_BULK or req.principal_role is not PrincipalRole.CSR:
        return None
    if ctx.subject == req.principal:
        return None
    return RuleHit(
        message=(
            f"客服人員代客操作他人帳戶 {ctx.subject}({req.kind.value}),"
            "風險升級為 high,需主管核准。"
        ),
        severity=Severity.APPROVAL_REQUIRED,
        escalate_to="high",
        evidence=[Evidence("rule", "AG-02",
                           f"代客操作:受影響主體 {ctx.subject} ≠ 操作者 {req.principal}")],
    )


def _rule_bulk_scope(ctx: RuleContext) -> RuleHit | None:
    req = ctx.request
    if req.kind is not ActionKind.READ_BULK:
        return None
    declared = req.params.get("declared_count")
    if declared is None:
        return RuleHit(
            message="批次查詢未申報筆數上限(declared_count),視同無界查詢,禁止。",
            severity=Severity.BLOCK,
            escalate_to="forbidden",
        )
    if declared > BULK_FORBIDDEN_COUNT:
        return RuleHit(
            message=f"批次查詢申報筆數 {declared} 超過上限 {BULK_FORBIDDEN_COUNT},禁止。",
            severity=Severity.BLOCK,
            escalate_to="forbidden",
            evidence=[Evidence("rule", "AG-10",
                               f"declared_count = {declared} > {BULK_FORBIDDEN_COUNT}")],
        )
    return None


def _rule_bulk_pii_export(ctx: RuleContext) -> RuleHit | None:
    req = ctx.request
    if req.kind is not ActionKind.READ_BULK:
        return None
    declared = req.params.get("declared_count")
    if declared is None or declared > BULK_FORBIDDEN_COUNT:
        return None  # 已由 AG-10 攔下
    return RuleHit(
        message=f"批次匯出 {declared} 筆客戶資料含個資欄位,匯出後不可回收,需人工核准。",
        severity=Severity.APPROVAL_REQUIRED,
    )


def _rule_refund_needs_approval(ctx: RuleContext) -> RuleHit | None:
    req = ctx.request
    if req.kind is not ActionKind.ISSUE_REFUND:
        return None
    amount = float(req.params.get("amount", 0))
    return RuleHit(
        message=f"退費 {amount:,.0f} 元為金流動作,一律需人工核准。",
        severity=Severity.APPROVAL_REQUIRED,
    )


def _rule_refund_single_limit(ctx: RuleContext) -> RuleHit | None:
    req = ctx.request
    if req.kind is not ActionKind.ISSUE_REFUND:
        return None
    amount = float(req.params.get("amount", 0))
    if amount <= REFUND_SINGLE_LIMIT:
        return None
    return RuleHit(
        message=f"退費金額 {amount:,.0f} 元超過單筆上限 {REFUND_SINGLE_LIMIT:,} 元,風險升級為 high。",
        severity=Severity.APPROVAL_REQUIRED,
        escalate_to="high",
        evidence=[Evidence("rule", "AG-21", f"amount = {amount:,.0f} > {REFUND_SINGLE_LIMIT:,}")],
    )


def _rule_refund_cumulative_limit(ctx: RuleContext) -> RuleHit | None:
    req = ctx.request
    if req.kind is not ActionKind.ISSUE_REFUND:
        return None
    account = ctx.shadow.get_account(ctx.subject)
    if account is None:
        return None
    amount = float(req.params.get("amount", 0))
    cumulative = sum(account.refunds_30d) + amount
    if cumulative <= REFUND_MONTHLY_LIMIT:
        return None
    return RuleHit(
        message=(
            f"帳戶 {ctx.subject} 30 日內累計退費將達 {cumulative:,.0f} 元,"
            f"超過累計上限 {REFUND_MONTHLY_LIMIT:,} 元,風險升級為 high。"
        ),
        severity=Severity.APPROVAL_REQUIRED,
        escalate_to="high",
        evidence=[Evidence("rule", "AG-22",
                           f"30 日累計 {sum(account.refunds_30d):,} + 本次 {amount:,.0f} "
                           f"> {REFUND_MONTHLY_LIMIT:,}")],
    )


def _rule_refund_redirect(ctx: RuleContext) -> RuleHit | None:
    req = ctx.request
    if req.kind is not ActionKind.ISSUE_REFUND:
        return None
    refund_to = req.params.get("refund_to")
    if refund_to is None or str(refund_to) == ctx.subject:
        return None
    return RuleHit(
        message=(
            f"退費入帳目標 {refund_to} 與受影響帳戶 {ctx.subject} 不一致,"
            "疑似金流轉向,風險升級為 high。"
        ),
        severity=Severity.APPROVAL_REQUIRED,
        escalate_to="high",
        evidence=[Evidence("rule", "AG-23", f"refund_to = {refund_to} ≠ account {ctx.subject}")],
    )


def _rule_plan_change_contract(ctx: RuleContext) -> RuleHit | None:
    req = ctx.request
    if req.kind is not ActionKind.CHANGE_PLAN:
        return None
    account = ctx.shadow.get_account(ctx.subject)
    if account is None or account.contract_months_left <= 0:
        return None
    return RuleHit(
        message=(
            f"帳戶 {ctx.subject} 尚在綁約期(剩 {account.contract_months_left} 個月),"
            "變更資費涉違約金,風險升級為 high,需人工核准。"
        ),
        severity=Severity.APPROVAL_REQUIRED,
        escalate_to="high",
        evidence=[Evidence("rule", "AG-24",
                           f"contract_months_left = {account.contract_months_left}")],
    )


def _rule_plan_downgrade(ctx: RuleContext) -> RuleHit | None:
    req = ctx.request
    if req.kind is not ActionKind.CHANGE_PLAN:
        return None
    from ..shadow import PLANS  # 局部匯入避免循環
    account = ctx.shadow.get_account(ctx.subject)
    new_plan = req.params.get("new_plan_id", "")
    if account is None or new_plan not in PLANS:
        return None
    if PLANS[new_plan]["monthly_fee"] >= account.monthly_fee:
        return None
    return RuleHit(
        message=(
            f"資費由 {account.monthly_fee} 元調降為 {PLANS[new_plan]['monthly_fee']} 元,"
            "月租調降屬營收影響動作,風險升級為 high,需人工核准。"
        ),
        severity=Severity.APPROVAL_REQUIRED,
        escalate_to="high",
        evidence=[Evidence("rule", "AG-25",
                           f"{account.plan_id} → {new_plan}(月租下降)")],
    )


def _rule_suspend(ctx: RuleContext) -> RuleHit | None:
    if ctx.request.kind is not ActionKind.SUSPEND_SERVICE:
        return None
    return RuleHit(
        message=f"停話將中斷帳戶 {ctx.subject} 的通信服務,一律需人工核准。",
        severity=Severity.APPROVAL_REQUIRED,
    )


def _rule_reissue_sim(ctx: RuleContext) -> RuleHit | None:
    if ctx.request.kind is not ActionKind.REISSUE_SIM:
        return None
    return RuleHit(
        message=(
            f"補發 SIM 將使帳戶 {ctx.subject} 舊卡立即失效。"
            "SIM swap 是帳號接管的主要途徑,一律需人工核准。"
        ),
        severity=Severity.APPROVAL_REQUIRED,
    )


GATE_RULES: tuple[GateRule, ...] = (
    GateRule("AG-00", "繞過治理禁止", "治理層不可被繞過;POLICY_OVERRIDE 任何角色皆不得執行。",
             _rule_policy_override),
    GateRule("AG-01", "用戶不得操作他人帳戶", "已驗證用戶身分僅得對本人帳戶執行動作;主體不符一律攔下。",
             _rule_cross_subject_customer),
    GateRule("AG-02", "代客操作需主管核准", "客服人員對非本人帳戶之操作,風險升級為 high 並需主管核准。",
             _rule_cross_subject_csr),
    GateRule("AG-10", "批次查詢筆數上限", f"批次查詢/匯出須申報筆數且不得超過 {BULK_FORBIDDEN_COUNT} 筆;超過即禁止。",
             _rule_bulk_scope),
    GateRule("AG-11", "個資匯出需核准", "任何含個資欄位之批次匯出,無論筆數,一律需人工核准。",
             _rule_bulk_pii_export),
    GateRule("AG-20", "金流動作需核准", "退費屬金流動作,一律需人工核准。",
             _rule_refund_needs_approval),
    GateRule("AG-21", "單筆退費上限", f"單筆退費超過 {REFUND_SINGLE_LIMIT:,} 元,風險升級為 high。",
             _rule_refund_single_limit),
    GateRule("AG-22", "累計退費上限", f"同帳戶 30 日內累計退費超過 {REFUND_MONTHLY_LIMIT:,} 元,風險升級為 high。",
             _rule_refund_cumulative_limit),
    GateRule("AG-23", "退費入帳一致性", "退費入帳目標與受影響帳戶不一致時,風險升級為 high。",
             _rule_refund_redirect),
    GateRule("AG-24", "綁約期內變更資費", "綁約期內變更資費涉違約金,風險升級為 high 並需人工核准。",
             _rule_plan_change_contract),
    GateRule("AG-25", "月租調降需核准", "月租調降屬營收影響動作,風險升級為 high 並需人工核准。",
             _rule_plan_downgrade),
    GateRule("AG-05", "停話一律核准", "停話中斷用戶通信服務,一律需人工核准。",
             _rule_suspend),
    GateRule("AG-06", "SIM 補發一律核准", "SIM 補發使舊卡失效,為帳號接管主要途徑,一律需人工核准。",
             _rule_reissue_sim),
)


# --------------------------------------------------------------------------------------
# 裁決結果
# --------------------------------------------------------------------------------------
@dataclass
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    risk: str
    findings: list[Finding] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "risk": self.risk,
            "findings": [f.to_dict() for f in self.findings],
            "reasons": self.reasons,
        }


class PolicyEngine:
    """確定性政策裁決:基礎權限表 + 動態升級規則。"""

    def __init__(self, rules: tuple[GateRule, ...] = GATE_RULES) -> None:
        self.rules = rules

    def adjudicate(self, request: ActionRequest, shadow: ShadowTelecomEnv) -> PolicyDecision:
        base = ACTION_POLICY[request.kind]
        ctx = RuleContext(request=request, shadow=shadow)
        risk = base.base_risk
        allowed = not base.forbidden
        requires_approval = base.requires_approval
        findings: list[Finding] = []
        reasons: list[str] = [f"{request.kind.value}:{base.note}(基礎風險 {base.base_risk})"]

        for rule in self.rules:
            outcome = rule.evaluate(ctx)
            if outcome is None:
                continue
            finding, escalate_to = outcome
            findings.append(finding)
            reasons.append(f"[{rule.rule_id}] {finding.message}")
            if escalate_to is not None:
                risk = risk_max(risk, escalate_to)
            if finding.severity is Severity.BLOCK:
                allowed = False
            elif finding.severity is Severity.APPROVAL_REQUIRED:
                requires_approval = True

        if risk == "forbidden":
            allowed = False
        if risk == "high":
            requires_approval = True
        return PolicyDecision(
            allowed=allowed, requires_approval=requires_approval,
            risk=risk, findings=findings, reasons=reasons,
        )

    def describe(self) -> dict[str, Any]:
        """匯出現行政策(規則 + 動作權限表),對應 GET /api/gate/policy。"""
        return {
            "engine": "deterministic-rules",
            "llm_involved": False,
            "note": "政策裁決層刻意不用 LLM:可稽核、不可繞過、可重現(規格 §4.3)。",
            "action_policy": [
                {
                    "action": p.kind.value, "base_risk": p.base_risk,
                    "requires_approval": p.requires_approval,
                    "forbidden": p.forbidden, "note": p.note,
                }
                for p in ACTION_POLICY.values()
            ],
            "rules": [
                {"rule_id": r.rule_id, "title": r.title, "statute": r.statute}
                for r in self.rules
            ],
            "limits": {
                "refund_single_limit": REFUND_SINGLE_LIMIT,
                "refund_monthly_limit": REFUND_MONTHLY_LIMIT,
                "bulk_forbidden_count": BULK_FORBIDDEN_COUNT,
            },
        }


__all__ = [
    "ACTION_POLICY",
    "ActionPolicy",
    "GATE_RULES",
    "GateRule",
    "PolicyDecision",
    "PolicyEngine",
    "RuleContext",
    "RuleHit",
]
