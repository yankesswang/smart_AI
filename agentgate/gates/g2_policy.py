"""G2 政策裁決(Adjudication)。

這一層是**規則**,不是 LLM(規格 §4.3)。三個理由:
1. 可稽核性:主管機關要的是「依據第 X 條規則」,不是「模型認為風險較高」。
2. 不可繞過:LLM 判斷可被提示詞影響;確定性規則不能。
3. 可重現:同樣輸入必然同樣輸出。

風險分級不是靜態常數,而是動作 × 範圍 × 主體的函式(規格 §4.1)。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
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
# 限額(可被外部政策檔覆寫)
# --------------------------------------------------------------------------------------
# 為什麼要能覆寫:限額是**營運政策**,不是工程常數。真實導入時它會被法遵/帳務調整,
# 而每一次調整都必須能在稽核紀錄上分得出來 —— 所以限額進 policy_version 的雜湊,
# 每筆 g2_adjudication 都帶 policy_version。改了限額,版本號就變,事後查得出
# 「那天那筆是用哪一版規則裁的」。
DEFAULT_LIMITS: dict[str, int] = {
    "refund_single_limit": REFUND_SINGLE_LIMIT,
    "refund_monthly_limit": REFUND_MONTHLY_LIMIT,
    "bulk_forbidden_count": BULK_FORBIDDEN_COUNT,
}

# 環境變數:指向 YAML/JSON 政策覆寫檔(見 load_policy_overrides)。
POLICY_FILE_ENV = "AGENTGATE_POLICY_FILE"


def _sha256_obj(obj: Any) -> str:
    canonical = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_policy_overrides(path: str | Path) -> tuple[dict[str, int], str]:
    """從 YAML / JSON 載入限額覆寫。回傳 (限額字典, 檔案內容 sha256)。

    只認得 DEFAULT_LIMITS 裡已存在的鍵 —— 政策檔不能憑空新增一條規則,
    那會讓「規則有哪些」不再由程式碼決定,稽核就追不到條文原文了。
    """
    file_path = Path(path)
    raw_bytes = file_path.read_bytes()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    text = raw_bytes.decode("utf-8")
    if file_path.suffix.lower() in {".yaml", ".yml"}:
        import yaml  # 延遲匯入:JSON 路徑不需要 pyyaml

        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"政策檔 {file_path} 的頂層必須是物件")
    limits = dict(DEFAULT_LIMITS)
    section = data.get("limits", data)
    unknown = [k for k in section if k not in DEFAULT_LIMITS]
    if unknown:
        raise ValueError(f"政策檔 {file_path} 含未知限額鍵:{sorted(unknown)}")
    for key, value in section.items():
        limits[key] = int(value)
    return limits, file_hash


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
    limits: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LIMITS))

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
    # 條文**樣板**(核准介面直接引用)。含 {refund_single_limit} 之類的佔位符,
    # 由生效中的限額渲染 —— 限額被政策檔覆寫時,條文原文會跟著變,
    # 不會出現「條文寫 10,000 但實際擋在 5,000」這種對不起來的稽核紀錄。
    statute: str
    check: Callable[[RuleContext], RuleHit | None]

    def render_statute(self, limits: dict[str, int]) -> str:
        try:
            return self.statute.format(**limits)
        except (KeyError, IndexError, ValueError):
            return self.statute

    def evaluate(self, ctx: RuleContext) -> tuple[Finding, str | None] | None:
        hit = self.check(ctx)
        if hit is None:
            return None
        finding = Finding(
            rule_id=self.rule_id, title=self.title, severity=hit.severity,
            message=hit.message, statute=self.render_statute(ctx.limits),
            evidence=tuple(hit.evidence), escalate_to=hit.escalate_to or "",
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
    limit = ctx.limits["bulk_forbidden_count"]
    declared = req.params.get("declared_count")
    if declared is None:
        return RuleHit(
            message="批次查詢未申報筆數上限(declared_count),視同無界查詢,禁止。",
            severity=Severity.BLOCK,
            escalate_to="forbidden",
        )
    if declared > limit:
        return RuleHit(
            message=f"批次查詢申報筆數 {declared} 超過上限 {limit},禁止。",
            severity=Severity.BLOCK,
            escalate_to="forbidden",
            evidence=[Evidence("rule", "AG-10",
                               f"declared_count = {declared} > {limit}")],
        )
    return None


def _rule_bulk_pii_export(ctx: RuleContext) -> RuleHit | None:
    req = ctx.request
    if req.kind is not ActionKind.READ_BULK:
        return None
    declared = req.params.get("declared_count")
    if declared is None or declared > ctx.limits["bulk_forbidden_count"]:
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
    limit = ctx.limits["refund_single_limit"]
    amount = float(req.params.get("amount", 0))
    if amount <= limit:
        return None
    return RuleHit(
        message=f"退費金額 {amount:,.0f} 元超過單筆上限 {limit:,} 元,風險升級為 high。",
        severity=Severity.APPROVAL_REQUIRED,
        escalate_to="high",
        evidence=[Evidence("rule", "AG-21", f"amount = {amount:,.0f} > {limit:,}")],
    )


def _rule_refund_cumulative_limit(ctx: RuleContext) -> RuleHit | None:
    req = ctx.request
    if req.kind is not ActionKind.ISSUE_REFUND:
        return None
    account = ctx.shadow.get_account(ctx.subject)
    if account is None:
        return None
    limit = ctx.limits["refund_monthly_limit"]
    amount = float(req.params.get("amount", 0))
    cumulative = sum(account.refunds_30d) + amount
    if cumulative <= limit:
        return None
    return RuleHit(
        message=(
            f"帳戶 {ctx.subject} 30 日內累計退費將達 {cumulative:,.0f} 元,"
            f"超過累計上限 {limit:,} 元,風險升級為 high。"
        ),
        severity=Severity.APPROVAL_REQUIRED,
        escalate_to="high",
        evidence=[Evidence("rule", "AG-22",
                           f"30 日累計 {sum(account.refunds_30d):,} + 本次 {amount:,.0f} "
                           f"> {limit:,}")],
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
    GateRule("AG-10", "批次查詢筆數上限",
             "批次查詢/匯出須申報筆數且不得超過 {bulk_forbidden_count} 筆;超過即禁止。",
             _rule_bulk_scope),
    GateRule("AG-11", "個資匯出需核准", "任何含個資欄位之批次匯出,無論筆數,一律需人工核准。",
             _rule_bulk_pii_export),
    GateRule("AG-20", "金流動作需核准", "退費屬金流動作,一律需人工核准。",
             _rule_refund_needs_approval),
    GateRule("AG-21", "單筆退費上限",
             "單筆退費超過 {refund_single_limit:,} 元,風險升級為 high。",
             _rule_refund_single_limit),
    GateRule("AG-22", "累計退費上限",
             "同帳戶 30 日內累計退費超過 {refund_monthly_limit:,} 元,風險升級為 high。",
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
    policy_version: str = ""      # 生效政策的內容雜湊(見 PolicyEngine.version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "risk": self.risk,
            "findings": [f.to_dict() for f in self.findings],
            "reasons": self.reasons,
            "policy_version": self.policy_version,
        }


class PolicyEngine:
    """確定性政策裁決:基礎權限表 + 動態升級規則。

    ``overrides_path`` 指向一份 YAML/JSON 政策檔,可覆寫限額(見 load_policy_overrides)。
    該檔的內容雜湊會進 ``version()``,所以「這筆裁決是用哪一版政策做的」查得出來。
    """

    def __init__(
        self,
        rules: tuple[GateRule, ...] = GATE_RULES,
        limits: dict[str, int] | None = None,
        overrides_path: str | Path | None = None,
    ) -> None:
        self.rules = rules
        self.overrides_path: str | None = None
        self.overrides_hash: str = ""
        resolved = dict(DEFAULT_LIMITS)
        if overrides_path is not None:
            loaded, file_hash = load_policy_overrides(overrides_path)
            resolved.update(loaded)
            self.overrides_path = str(overrides_path)
            self.overrides_hash = file_hash
        if limits:
            unknown = [k for k in limits if k not in DEFAULT_LIMITS]
            if unknown:
                raise ValueError(f"未知限額鍵:{sorted(unknown)}")
            resolved.update({k: int(v) for k, v in limits.items()})
        self.limits: dict[str, int] = resolved
        # 規則與限額在建構後不再變動,版本雜湊算一次就好 ——
        # adjudicate() 每次重算會讓決策延遲 p95 從 0.1 ms 漲到 0.8 ms,
        # 而「線上可用」是這個專案量測的指標之一,不能被自己的稽核欄位吃掉。
        self._version: dict[str, Any] | None = None

    @classmethod
    def from_env(cls, rules: tuple[GateRule, ...] = GATE_RULES) -> "PolicyEngine":
        """依 AGENTGATE_POLICY_FILE 環境變數載入政策檔;沒設就是內建預設。"""
        path = os.environ.get(POLICY_FILE_ENV)
        return cls(rules, overrides_path=path) if path else cls(rules)

    # -- 政策版本 -----------------------------------------------------------------------
    def version(self) -> dict[str, Any]:
        """回傳規則條文與權限表的內容雜湊(確定性)。

        為什麼要有版本:稽核鏈證明「這筆紀錄沒被改過」,但證明不了
        「當時生效的規則長什麼樣」。規則改一個字、限額改一塊錢,版本號就變 ——
        每筆 g2_adjudication 都帶 policy_version,事後才對得回當時的條文原文。

        雜湊只涵蓋**會影響裁決結果**的東西:規則 ID/標題/條文樣板、動作權限表、
        生效限額、政策檔內容。註解與程式碼排版不進雜湊(改註解不該讓版本號跳動)。
        """
        if self._version is not None:
            return self._version
        rules_payload = [
            {"rule_id": r.rule_id, "title": r.title, "statute": r.statute}
            for r in self.rules
        ]
        action_payload = [
            {"action": p.kind.value, "base_risk": p.base_risk,
             "requires_approval": p.requires_approval, "forbidden": p.forbidden}
            for p in ACTION_POLICY.values()
        ]
        limits_payload = dict(sorted(self.limits.items()))
        components = {
            "rules": _sha256_obj(rules_payload),
            "action_policy": _sha256_obj(action_payload),
            "limits": _sha256_obj(limits_payload),
            "overrides_file": self.overrides_hash,
        }
        digest = _sha256_obj(components)
        self._version = {
            "policy_version": f"pv-{digest[:16]}",
            "components": components,
            "limits": limits_payload,
            "rule_count": len(self.rules),
            "overrides": {
                "path": self.overrides_path,
                "sha256": self.overrides_hash or None,
                "active": bool(self.overrides_path),
            },
        }
        return self._version

    def adjudicate(self, request: ActionRequest, shadow: ShadowTelecomEnv) -> PolicyDecision:
        base = ACTION_POLICY[request.kind]
        ctx = RuleContext(request=request, shadow=shadow, limits=self.limits)
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
            policy_version=self.version()["policy_version"],
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
                {"rule_id": r.rule_id, "title": r.title,
                 "statute": r.render_statute(self.limits)}
                for r in self.rules
            ],
            "limits": dict(self.limits),
            "version": self.version(),
        }


__all__ = [
    "ACTION_POLICY",
    "ActionPolicy",
    "DEFAULT_LIMITS",
    "GATE_RULES",
    "POLICY_FILE_ENV",
    "GateRule",
    "PolicyDecision",
    "PolicyEngine",
    "RuleContext",
    "RuleHit",
    "load_policy_overrides",
]
