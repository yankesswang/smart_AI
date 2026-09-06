"""年化模型 —— 把 45 分鐘的當班回填換算成年度商業論據(規格 §8.2)。

規格 §8.2 直接給了公式::

    年度節省 = 動作總數 × (1 − 高風險比例) × 單次覆核工時 × 人力成本
             + 避免的誤動作損失期望值
             − 平台成本

這個模組把它算出來,而且守住四條規則:

1. **三個比例全部實測**:高風險比例、自動放行率、送核准比例由
   :func:`~.measurement.measure_ops` 從 console 回填的當班資料直接數,不是假設。
2. **實測 / 推導 / 假設三種來源全程標示**,報表可以被 :func:`audit_figures`
   掃出未標示的數字。
3. **基準情境偏保守**,而且會誠實算出「在什麼假設下 ROI 不成立」(:func:`breakeven`)。
4. **最敏感的那根軸單獨開一節**(:func:`incumbent_sensitivity`)。

---
規格公式的一個修正:``(1 − 高風險比例)`` 的分母是什麼
---
規格寫的是「沒有治理層時,企業唯一的安全選項是 Agent 動作 100% 人工覆核」。
把 100% 當現況會讓效益虛胖,因為**沒有企業會這樣做** —— 它太貴了,
所以它們的實際選擇是規格 §1.1 的第一個壞選項:不讓 Agent 動手。

所以這裡把現況參數化成 ``incumbent_review_coverage``,並提供三種取值:

===================  ==========================================================
``full`` = 1.0       規格 §8.2 的字面值:100% 人工覆核。
``tiered`` = 實測    分層覆核:只讓 Agent 自動做唯讀查詢,風險 ≥ medium 的動作
                     退回人工。取值 = 實測的「風險 ≥ medium 動作比例」。
                     **基準情境採用這一種。**
``gate_only`` = 實測 只覆核 AgentGate 也會送簽的那些(= 送核准比例)。
                     等於假設現況已經有一個和我們一樣準的分流器,
                     覆核工時節省歸零,只剩證據包加速與誤動作損失避免。
===================  ==========================================================

年度效益因此拆成三條,每一條都追得到一個實測比例::

    免覆核節省 = N × max(0, 現況覆核覆蓋率 − 送核准比例) × 單次覆核工時 × 人力成本
    證據包加速 = N × min(現況覆核覆蓋率, 送核准比例) × (單次覆核工時 − 證據包工時) × 人力成本
    誤動作損失避免 = Σ 事件數 × 單次損失 × (B0 HAR − B3 HAR)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from . import assumptions as A
from .assumptions import BASIS_LABEL, Assumption, Basis
from .measurement import (
    DEFAULT_SEEDS,
    DEFAULT_WINDOW_MINUTES,
    HarmMeasurement,
    OpsMeasurement,
    measure_harm_prevention,
    measure_ops,
)
from .pricing import (
    DEFAULT_PRICES,
    PlatformCost,
    PriceBook,
    build_platform_cost,
    default_agent_seats,
)

#: 現況覆核覆蓋率的三種取法。
COVERAGE_MODES = ("gate_only", "tiered", "full")


# ======================================================================================
# 敏感度情境
# ======================================================================================
@dataclass(frozen=True)
class ScenarioCase:
    """一組情境參數。三個情境只差在這五個數字上。"""

    key: str
    label: str
    volume_multiplier: float       # 年度動作量倍率
    manual_review_minutes: float   # 無證據包時的單次覆核工時
    realization: float             # 效益實現率
    harm_multiplier: float         # 誤動作事件頻率倍率
    cost_multiplier: float         # 方案成本倍率
    note: str = ""

    def replace(self, **kwargs: float) -> "ScenarioCase":
        data = {
            "key": self.key, "label": self.label,
            "volume_multiplier": self.volume_multiplier,
            "manual_review_minutes": self.manual_review_minutes,
            "realization": self.realization,
            "harm_multiplier": self.harm_multiplier,
            "cost_multiplier": self.cost_multiplier,
            "note": self.note,
        }
        data.update(kwargs)
        return ScenarioCase(**data)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": Basis.ASSUMPTION.value,
            "key": self.key,
            "label": self.label,
            "volume_multiplier": round(self.volume_multiplier, 4),
            "manual_review_minutes": round(self.manual_review_minutes, 4),
            "realization": round(self.realization, 4),
            "harm_multiplier": round(self.harm_multiplier, 4),
            "cost_multiplier": round(self.cost_multiplier, 4),
            "note": self.note,
        }


CONSERVATIVE = ScenarioCase(
    "conservative", "保守", 0.35, 1.5, 0.6, 0.5, 1.25,
    "動作量只有 Demo 流量的三分之一;覆核只要 90 秒;效益打六折;誤動作事件減半;成本高兩成五。",
)
BASE = ScenarioCase(
    "base", "基準", 1.00, A.MANUAL_REVIEW_MINUTES, A.BENEFIT_REALIZATION, 1.0, 1.00,
    "動作量取實測回填速率年化;覆核 3 分鐘;效益實現率 80%。",
)
OPTIMISTIC = ScenarioCase(
    "optimistic", "樂觀", 1.50, 5.0, 1.0, 1.5, 0.90,
    "動作量 1.5 倍;覆核 5 分鐘(接近真實客服的查證時間);效益全額實現。",
)
CASES: tuple[ScenarioCase, ...] = (CONSERVATIVE, BASE, OPTIMISTIC)


# ======================================================================================
# 效益條目
# ======================================================================================
BENEFIT_LABELS: dict[str, str] = {
    "review_avoided": "免覆核節省(自動放行的動作不再需要人看)",
    "evidence_speedup": "證據包加速(仍需人簽的那些,15 秒取代 3 分鐘)",
    "harm_avoided": "誤動作損失避免(個資裁罰、錯誤金流、SIM swap)",
}
BENEFIT_ORDER = ("review_avoided", "evidence_speedup", "harm_avoided")


@dataclass(frozen=True)
class BenefitLine:
    key: str
    annual_ntd: float
    formula: str
    traces_to: str
    basis: Basis = Basis.DERIVED

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": self.basis.value,
            "basis_label": BASIS_LABEL[self.basis.value],
            "key": self.key,
            "label": BENEFIT_LABELS[self.key],
            "annual_ntd": round(self.annual_ntd, 0),
            "formula": self.formula,
            "traces_to": self.traces_to,
        }


@dataclass(frozen=True)
class BusinessCase:
    case: ScenarioCase
    ops: OpsMeasurement
    harm: HarmMeasurement
    coverage_mode: str
    incumbent_coverage: float
    annual_actions: float
    lines: list[BenefitLine]
    cost: PlatformCost

    # ---- 效益 --------------------------------------------------------------------
    @property
    def annual_benefit_ntd(self) -> float:
        return sum(line.annual_ntd for line in self.lines)

    @property
    def steady_net_ntd(self) -> float:
        return self.annual_benefit_ntd - self.cost.annual_recurring_ntd

    @property
    def year_one_net_ntd(self) -> float:
        return self.annual_benefit_ntd - self.cost.year_one_ntd

    @property
    def steady_roi_pct(self) -> float | None:
        total = self.cost.annual_recurring_ntd
        return None if total <= 0 else 100.0 * self.steady_net_ntd / total

    @property
    def year_one_roi_pct(self) -> float | None:
        total = self.cost.year_one_ntd
        return None if total <= 0 else 100.0 * self.year_one_net_ntd / total

    @property
    def payback_months(self) -> float | None:
        monthly = self.steady_net_ntd / 12.0
        return None if monthly <= 0 else self.cost.one_time / monthly

    def npv_ntd(self, years: int = A.NPV_YEARS, rate: float = A.DISCOUNT_RATE) -> float:
        npv = -self.cost.one_time
        for t in range(1, years + 1):
            npv += self.steady_net_ntd / ((1.0 + rate) ** t)
        return npv

    # ---- 人力等效 -----------------------------------------------------------------
    @property
    def incumbent_review_fte(self) -> float:
        """現況那個覆核覆蓋率換算成幾個全職人力。**這個數字必須主動講。**"""
        minutes = (self.annual_actions * self.incumbent_coverage
                   * self.case.manual_review_minutes)
        return minutes / 60.0 / 2000.0

    @property
    def governed_review_fte(self) -> float:
        minutes = (self.annual_actions * self.ops.approval_rate
                   * A.EVIDENCE_REVIEW_MINUTES)
        return minutes / 60.0 / 2000.0

    def to_dict(self) -> dict[str, Any]:
        payback = self.payback_months
        return {
            "case": self.case.to_dict(),
            "incumbent": {
                "basis": Basis.ASSUMPTION.value,
                "coverage_mode": self.coverage_mode,
                "coverage": round(self.incumbent_coverage, 4),
                "review_fte": round(self.incumbent_review_fte, 1),
            },
            "volume": {
                "basis": Basis.DERIVED.value,
                "annual_actions": round(self.annual_actions, 0),
                "annual_approvals": round(
                    self.annual_actions * self.ops.approval_rate, 0),
                "annual_auto_released": round(
                    self.annual_actions * self.ops.auto_pass_rate, 0),
                "governed_review_fte": round(self.governed_review_fte, 2),
                "formula": "每分鐘動作數(實測) × 60 × 每日受治理時數 × 年營運天數 × 動作量倍率",
            },
            "benefits": [line.to_dict() for line in self.lines],
            "cost": self.cost.to_dict(),
            "result": {
                "basis": Basis.DERIVED.value,
                "annual_benefit_ntd": round(self.annual_benefit_ntd, 0),
                "annual_cost_ntd": round(self.cost.annual_recurring_ntd, 0),
                "one_time_ntd": round(self.cost.one_time, 0),
                "year_one_net_ntd": round(self.year_one_net_ntd, 0),
                "steady_net_ntd": round(self.steady_net_ntd, 0),
                "year_one_roi_pct": (None if self.year_one_roi_pct is None
                                     else round(self.year_one_roi_pct, 1)),
                "steady_roi_pct": (None if self.steady_roi_pct is None
                                   else round(self.steady_roi_pct, 1)),
                "payback_months": None if payback is None else round(payback, 1),
                "npv_3y_ntd": round(self.npv_ntd(), 0),
                "discount_rate": A.DISCOUNT_RATE,
            },
        }


# ======================================================================================
# 組裝
# ======================================================================================
def coverage_for(mode: str, ops: OpsMeasurement) -> float:
    """三種現況假設的覆核覆蓋率。兩種是實測值,一種是規格字面值。"""
    if mode == "full":
        return 1.0
    if mode == "tiered":
        return ops.review_needed_share
    if mode == "gate_only":
        return ops.approval_rate
    raise ValueError(f"未知的現況模式:{mode}(可用:{COVERAGE_MODES})")


def annual_actions(ops: OpsMeasurement, volume_multiplier: float = 1.0) -> float:
    """年度動作總數。**推導**:實測的每分鐘動作數 × 60 × 每日受治理時數 × 天數。"""
    return (
        ops.actions_per_minute
        * 60.0
        * A.governed_hours_per_day()
        * A.OPERATING_DAYS_PER_YEAR
        * volume_multiplier
    )


def harm_expected_annual_ntd(multiplier: float = 1.0) -> float:
    """年度誤動作損失期望值(無治理時)。三個事件類別各自具名。"""
    return multiplier * (
        A.ANNUAL_PII_BREACH_EVENTS * A.PII_BREACH_COST_NTD
        + A.ANNUAL_WRONG_PAYMENT_EVENTS * A.WRONG_PAYMENT_COST_NTD
        + A.ANNUAL_SIM_SWAP_EVENTS * A.SIM_SWAP_COST_NTD
    )


def build_business_case(
    ops: OpsMeasurement,
    harm: HarmMeasurement,
    case: ScenarioCase = BASE,
    coverage_mode: str = A.INCUMBENT_REVIEW_COVERAGE_MODE,
    prices: PriceBook = DEFAULT_PRICES,
    seats: int | None = None,
    include_evidence_speedup: bool = True,
) -> BusinessCase:
    """把實測比例 + 具名假設 → 年度效益、成本與 ROI。

    ``include_evidence_speedup=False`` 拿掉「證據包加速」那一條。
    那一條建立在規格 §4.5 的 **15 秒設計目標**上,而那個目標**還沒有做過真人計時** ——
    所以我們必須能回答「如果那條不算,結論會不會翻掉」。
    """
    coverage = coverage_for(coverage_mode, ops)
    n = annual_actions(ops, case.volume_multiplier)
    approvals = n * ops.approval_rate
    wage_per_min = A.REVIEWER_HOURLY_COST_NTD / 60.0
    r = case.realization

    avoided_share = max(0.0, coverage - ops.approval_rate)
    still_reviewed_share = min(coverage, ops.approval_rate)
    speedup_minutes = max(0.0, case.manual_review_minutes - A.EVIDENCE_REVIEW_MINUTES)

    lines = [
        BenefitLine(
            "review_avoided",
            n * avoided_share * case.manual_review_minutes * wage_per_min * r,
            f"{n:,.0f} × ({coverage:.4f} − {ops.approval_rate:.4f}) × "
            f"{case.manual_review_minutes} 分 × {A.REVIEWER_HOURLY_COST_NTD}/60 × {r}",
            "實測:送核准比例(console 回填當班資料);假設:覆核工時、人力成本",
        ),
        BenefitLine(
            "evidence_speedup",
            n * still_reviewed_share * speedup_minutes * wage_per_min * r,
            f"{n:,.0f} × {still_reviewed_share:.4f} × "
            f"({case.manual_review_minutes} − {A.EVIDENCE_REVIEW_MINUTES}) 分 × "
            f"{A.REVIEWER_HOURLY_COST_NTD}/60 × {r}",
            "實測:送核准比例;假設:證據包工時(規格 §4.5 的設計目標,尚未真人計時)",
        ) if include_evidence_speedup else BenefitLine(
            "evidence_speedup", 0.0, "已停用(robustness 檢查:拿掉未驗證的 15 秒設計目標)",
            "—",
        ),
        BenefitLine(
            "harm_avoided",
            harm_expected_annual_ntd(case.harm_multiplier)
            * harm.prevented_share * r,
            f"(0.5×{A.PII_BREACH_COST_NTD:,.0f} + 24×{A.WRONG_PAYMENT_COST_NTD:,.0f} "
            f"+ 2×{A.SIM_SWAP_COST_NTD:,.0f}) × {case.harm_multiplier} × "
            f"{harm.prevented_share:.4f} × {r}",
            "實測:B0 與 B3 的 HAR(validation.run_validation());假設:事件頻率與單次損失",
        ),
    ]
    cost = build_platform_cost(
        annual_decisions=n, annual_approvals=approvals, seats=seats,
        prices=prices, cost_multiplier=case.cost_multiplier,
    )
    return BusinessCase(
        case=case, ops=ops, harm=harm, coverage_mode=coverage_mode,
        incumbent_coverage=coverage, annual_actions=n, lines=lines, cost=cost,
    )


# ======================================================================================
# 破口分析
# ======================================================================================
def _threshold(
    fn: Callable[[float], float], lo: float, hi: float, steps: int = 60
) -> tuple[float | None, str]:
    """單調遞增函式的臨界點。回傳 ``(值, 狀態)``。

    狀態有三種,而且**必須分開**:

    * ``always`` —— 在區間下界就已經回本 → 這個參數再怎麼差都不會讓結論翻掉。
    * ``never``  —— 在區間上界仍不回本 → 這個參數再怎麼好都救不了。
    * ``found``  —— 解出臨界值。

    早期版本把 ``always`` 和 ``found`` 混在一起(直接回傳下界),結果報表會把
    「搜尋區間的下界」當成臨界值印出來 —— 那是一個看起來很具體、其實不成立的數字。
    """
    if fn(lo) >= 0:
        return lo, "always"
    if fn(hi) < 0:
        return None, "never"
    for _ in range(steps):
        mid = (lo + hi) / 2.0
        if fn(mid) < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0, "found"


@dataclass(frozen=True)
class BreakevenPoint:
    key: str
    label: str
    base_value: float
    breakeven_value: float | None
    unit: str
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": Basis.DERIVED.value,
            "key": self.key,
            "label": self.label,
            "base_value": round(self.base_value, 4),
            "breakeven_value": (None if self.breakeven_value is None
                                else round(self.breakeven_value, 4)),
            "unit": self.unit,
            "verdict": self.verdict,
        }


def _ops_with_approval(ops: OpsMeasurement, approval: float) -> OpsMeasurement:
    """做一個「送核准比例被換掉」的量測副本。用於破口分析,不改原始實測值。"""

    class _Override(OpsMeasurement):
        @property
        def approval_rate(self) -> float:  # type: ignore[override]
            return approval

    clone = _Override(samples=list(ops.samples), window_minutes=ops.window_minutes,
                      kind_mix=dict(ops.kind_mix), gate_mix=dict(ops.gate_mix))
    return clone


def breakeven(
    ops: OpsMeasurement,
    harm: HarmMeasurement,
    case: ScenarioCase = BASE,
    coverage_mode: str = A.INCUMBENT_REVIEW_COVERAGE_MODE,
    prices: PriceBook = DEFAULT_PRICES,
    include_evidence_speedup: bool = True,
) -> list[BreakevenPoint]:
    """讓「年度效益 = 年度經常性成本」的臨界參數值。

    全部樂觀的 ROI 沒有說服力,誠實說出臨界值才有。
    """
    coverage_base = coverage_for(coverage_mode, ops)

    def net(c: ScenarioCase, measurement: OpsMeasurement | None = None,
            coverage_override: float | None = None) -> float:
        m = measurement or ops
        if coverage_override is None:
            bc = build_business_case(m, harm, c, coverage_mode, prices,
                                     include_evidence_speedup=include_evidence_speedup)
        else:
            bc = build_business_case_with_coverage(
                m, harm, c, coverage_override, prices,
                include_evidence_speedup=include_evidence_speedup)
        return bc.steady_net_ntd

    points: list[BreakevenPoint] = []

    def add(key: str, label: str, base: float, result: tuple[float | None, str],
            unit: str, found: str, never: str, always: str,
            transform: Callable[[float], float] | None = None) -> None:
        value, status = result
        if status == "found" and value is not None:
            shown = transform(value) if transform else value
            points.append(BreakevenPoint(key, label, base, shown, unit,
                                         found.format(value=shown)))
        elif status == "never":
            points.append(BreakevenPoint(key, label, base, None, unit, never))
        else:
            points.append(BreakevenPoint(key, label, base, None, unit, always))

    add("volume_multiplier", "年度動作量倍率", case.volume_multiplier,
        _threshold(lambda x: net(case.replace(volume_multiplier=x)), 0.0, 6.0), "倍",
        "動作量需 ≥ 實測回填速率年化值的 {value:.3f} 倍"
        "(換算成件數見報表 breakeven_actions_per_year);低於此值,固定成本攤不平。",
        "即使動作量是實測值的 6 倍仍無法回本。",
        "任何動作量都回本 —— 這不可能,固定成本一定要攤;若出現此結論請先檢查定價。")

    add("manual_review_minutes", "單次覆核工時", case.manual_review_minutes,
        _threshold(lambda x: net(case.replace(manual_review_minutes=x)), 0.0, 30.0),
        "分鐘",
        "現況一次覆核需 ≥ {value:.2f} 分鐘;比這更快就代表人工覆核本來就不貴,本方案不成立。",
        "即使一次覆核要 30 分鐘仍無法回本。",
        "覆核工時再短都回本 —— 代表效益不是來自工時節省,請看誤動作損失那一條。")

    add("incumbent_coverage", "現況覆核覆蓋率", coverage_base,
        _threshold(lambda x: net(case, coverage_override=x), 0.0, 1.0), "比例",
        "現況需覆核 ≥ {value:.1%} 的動作;低於此值代表現況本來就沒在覆核,沒有工時可省。",
        "即使現況 100% 覆核仍無法回本。",
        "**即使現況完全沒有人工覆核,方案仍成立** —— 效益全部來自證據包加速與"
        "誤動作損失避免;這代表結論壓在那兩項假設上,見 robustness 一節。")

    # 送核准比例越高越差 → 代入 (1 − x) 反轉單調方向,再把根換算回來。
    add("approval_rate", "送核准比例上限", ops.approval_rate,
        _threshold(lambda x: net(case, measurement=_ops_with_approval(ops, 1.0 - x)),
                   0.0, 1.0), "比例",
        "送核准比例不得超過 {value:.1%}(實測 " + f"{ops.approval_rate:.1%}" +
        ");超過就代表治理層自己製造了太多人工。",
        "在任何送核准比例下都無法回本。",
        "**沒有上限** —— 即使 100% 的動作都送人簽,證據包加速那一條仍足以覆蓋成本。"
        "但那一條建立在尚未真人計時的 15 秒設計目標上,所以報表另附 robustness 一節,"
        "把它拿掉重算。",
        transform=lambda x: 1.0 - x)

    add("realization", "效益實現率", case.realization,
        _threshold(lambda x: net(case.replace(realization=x)), 0.0, 2.0), "比例",
        "實現率需 ≥ {value:.1%};低於此值代表現場落地折損過大。",
        "即使效益 200% 實現仍無法覆蓋成本。",
        "實現率再低都回本 —— 若出現此結論代表成本結構有誤,請檢查定價。")

    add("harm_multiplier", "誤動作損失倍率", case.harm_multiplier,
        _threshold(lambda x: net(case.replace(harm_multiplier=x)), 0.0, 50.0), "倍",
        "誤動作損失需 ≥ 假設值的 {value:.2f} 倍。",
        "即使誤動作損失是假設值的 50 倍仍無法回本。",
        "**即使誤動作損失歸零,方案仍成立。** 這一項(個資裁罰、錯誤金流、SIM swap "
        "的金額與頻率)是我們最不可靠的假設,而它對結論沒有影響 —— 這件事要主動講。")

    add("cost_multiplier", "方案成本倍率上限", case.cost_multiplier,
        _threshold(lambda x: net(case.replace(cost_multiplier=8.0 - x)), 0.0, 8.0), "倍",
        "成本不得超過定價的 {value:.2f} 倍;超過即無法回本。",
        "在任何成本水準下都無法回本。",
        "成本再高都回本 —— 若出現此結論代表效益被高估,請檢查假設。",
        transform=lambda x: 8.0 - x)

    return points


def build_business_case_with_coverage(
    ops: OpsMeasurement,
    harm: HarmMeasurement,
    case: ScenarioCase,
    coverage: float,
    prices: PriceBook = DEFAULT_PRICES,
    include_evidence_speedup: bool = True,
) -> BusinessCase:
    """給定一個明確的現況覆核覆蓋率(破口分析與現況敏感度用)。"""

    class _Fixed(OpsMeasurement):
        @property
        def review_needed_share(self) -> float:  # type: ignore[override]
            return coverage

    clone = _Fixed(samples=list(ops.samples), window_minutes=ops.window_minutes,
                   kind_mix=dict(ops.kind_mix), gate_mix=dict(ops.gate_mix))
    return build_business_case(clone, harm, case, "tiered", prices,
                               include_evidence_speedup=include_evidence_speedup)


def incumbent_sensitivity(
    ops: OpsMeasurement,
    harm: HarmMeasurement,
    case: ScenarioCase = BASE,
    prices: PriceBook = DEFAULT_PRICES,
) -> list[dict[str, Any]]:
    """最敏感的一根軸:拿什麼當現況。同一份實測比例,換一個現況假設,ROI 差好幾倍。"""
    labels = {
        "gate_only": "現況已有一個和我們一樣準的分流器(覆核覆蓋率 = 送核准比例)",
        "tiered": "分層覆核:唯讀自動、改狀態退回人工 —— **我們採用的**",
        "full": "全人工覆核(規格 §8.2 字面值)",
    }
    rows: list[dict[str, Any]] = []
    for mode in COVERAGE_MODES:
        bc = build_business_case(ops, harm, case, mode, prices)
        rows.append({
            "basis": Basis.DERIVED.value,
            "mode": mode,
            "label": labels[mode],
            "coverage": round(bc.incumbent_coverage, 4),
            "incumbent_review_fte": round(bc.incumbent_review_fte, 1),
            "governed_review_fte": round(bc.governed_review_fte, 2),
            "annual_benefit_ntd": round(bc.annual_benefit_ntd, 0),
            "steady_roi_pct": (None if bc.steady_roi_pct is None
                               else round(bc.steady_roi_pct, 1)),
            "payback_months": (None if bc.payback_months is None
                               else round(bc.payback_months, 1)),
        })
    return rows


# ======================================================================================
# 報表
# ======================================================================================
def measured_assumptions(ops: OpsMeasurement, harm: HarmMeasurement) -> list[Assumption]:
    """把實測與推導的那幾個數字也登記成具名參數,和假設放在同一張表上。"""
    n = annual_actions(ops)
    return [
        Assumption(
            "actions_per_minute", "每分鐘動作數", ops.actions_per_minute, "件/分鐘",
            Basis.MEASURED,
            f"console.OpsSimulator.warm_start({ops.window_minutes} 分鐘)"
            f" × {len(ops.samples)} 個 seed",
            "直接數回填當班歷史產生的工單數 ÷ 視窗長度。流量**組成**是 console.py 的"
            "設計(假設),在該組成下的件數才是實測。",
        ),
        Assumption(
            "auto_pass_rate", "自動放行率", ops.auto_pass_rate, "比例",
            Basis.MEASURED, "同上;裁決 status == executed 的比例",
            f"{len(ops.samples)} 個 seed 的平均;"
            f"區間 {ops.spread('auto_pass')[0]:.3f}–{ops.spread('auto_pass')[1]:.3f}。",
        ),
        Assumption(
            "approval_rate", "送核准比例", ops.approval_rate, "比例",
            Basis.MEASURED, "同上;裁決 status == pending_approval 的比例",
            f"區間 {ops.spread('approval')[0]:.3f}–{ops.spread('approval')[1]:.3f}。"
            "這是規格 §8.2 公式裡「高風險比例」的實測值 —— 導入後仍需人簽的那一塊。",
        ),
        Assumption(
            "blocked_rate", "攔截比例", ops.blocked_rate, "比例",
            Basis.MEASURED, "同上;裁決 status == blocked 的比例",
            f"區間 {ops.spread('blocked')[0]:.3f}–{ops.spread('blocked')[1]:.3f}。"
            "攻擊佔約 12%(console.py 刻意高於真實環境),所以這個數字偏高。",
        ),
        Assumption(
            "high_risk_share", "高風險動作比例", ops.high_risk_share, "比例",
            Basis.MEASURED, "同上;risk ∈ {high, forbidden} 的比例",
            f"區間 {ops.spread('high_risk')[0]:.3f}–{ops.spread('high_risk')[1]:.3f}。",
        ),
        Assumption(
            "review_needed_share", "風險 ≥ medium 動作比例", ops.review_needed_share,
            "比例", Basis.MEASURED, "同上;risk ∈ {medium, high, forbidden} 的比例",
            f"區間 {ops.spread('review_needed')[0]:.3f}–"
            f"{ops.spread('review_needed')[1]:.3f}。"
            "基準情境用它當「現況分層覆核」的覆核覆蓋率。",
        ),
        Assumption(
            "audit_records_per_action", "每次裁決的稽核紀錄數",
            ops.audit_records_per_action, "筆/次", Basis.MEASURED,
            "同上;AuditChain.records ÷ 動作數",
            "裁決計價的成本結構依據 —— 主要成本在保存,不在算力。",
        ),
        Assumption(
            "har_ungoverned", "無治理時的有害動作放行率", harm.har_ungoverned, "比例",
            Basis.MEASURED, "validation.run_validation() 的 B0",
            "自建 120 條情境測試集;誤動作損失避免那一項的乘數。",
        ),
        Assumption(
            "har_governed", "AgentGate 的有害動作放行率", harm.har_governed, "比例",
            Basis.MEASURED, "validation.run_validation() 的 B3", "同上。",
        ),
        Assumption(
            "agent_seats", "受治理 Agent 席位數", float(default_agent_seats()), "席",
            Basis.MEASURED, "console.SEATS", "直接數席位表,不寫死。",
        ),
        Assumption(
            "annual_actions", "年度動作總數", n, "件/年", Basis.DERIVED,
            "每分鐘動作數 × 60 × 每日受治理時數 × 年營運天數",
            f"{ops.actions_per_minute:.3f} × 60 × {A.governed_hours_per_day():.0f} × "
            f"{A.OPERATING_DAYS_PER_YEAR:.0f} = {n:,.0f}。"
            "**這是推導,不是實測** —— 實測的只有「每分鐘幾件」。",
        ),
    ]


def build_business_report(
    ops: OpsMeasurement | None = None,
    harm: HarmMeasurement | None = None,
    cases: Iterable[ScenarioCase] = CASES,
    prices: PriceBook = DEFAULT_PRICES,
    coverage_mode: str = A.INCUMBENT_REVIEW_COVERAGE_MODE,
    seeds: Iterable[int] = DEFAULT_SEEDS,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> dict[str, Any]:
    """完整商業案例報表:三情境 ROI、成本結構、破口分析、現況敏感度與中華電信收入。"""
    ops = ops or measure_ops(tuple(seeds), window_minutes)
    harm = harm or measure_harm_prevention()
    cases = list(cases)
    built = [build_business_case(ops, harm, c, coverage_mode, prices) for c in cases]
    base_case = build_business_case(ops, harm, BASE, coverage_mode, prices)

    points = breakeven(ops, harm, BASE, coverage_mode, prices)
    volume_point = next((p for p in points if p.key == "volume_multiplier"), None)
    breakeven_actions = (
        None if volume_point is None or volume_point.breakeven_value is None
        else annual_actions(ops, volume_point.breakeven_value)
    )

    return {
        "meta": {
            "title": "AgentGate｜商業案例",
            "roi_formula": (
                "年度節省 = 動作總數 × (1 − 高風險比例) × 單次覆核工時 × 人力成本 "
                "+ 避免的誤動作損失期望值 − 平台成本"
            ),
            "roi_source": "AgentGate 技術規格與競賽提案 §8.2",
            "measurement_source": (
                "agentgate.console.OpsSimulator.warm_start()(回填當班歷史)"
                " + agentgate.validation.run_validation()(120 條情境 × 六 baseline)"
            ),
            "not_counted": (
                "不計入 Agent 自動化本身的價值(那是 Agent 專案的 ROI,不是治理層的);"
                "不計入商譽、客戶流失、保險費率與監理檢查成本(無法追溯到任何實測量);"
                "不計入稽核與法遵作業的節省(可回溯的稽核軌跡確實會省下調閱工時,"
                "但我們沒有量測那個流程)。"
            ),
            "disclaimer": A.DISCLAIMER,
        },
        "measurement": ops.to_dict(),
        "harm_prevention": harm.to_dict(),
        "assumptions": [a.to_dict() for a in
                        (measured_assumptions(ops, harm) + A.static_assumptions())],
        "pricing": {
            "prices": prices.to_dict(),
            "tiers": prices.rationales(),
        },
        "cases": [bc.to_dict() for bc in built],
        "breakeven": [p.to_dict() for p in points],
        "breakeven_actions_per_year": {
            "basis": Basis.DERIVED.value,
            "value": None if breakeven_actions is None else round(breakeven_actions, 0),
            "per_minute": (None if breakeven_actions is None else round(
                breakeven_actions / 60.0 / A.governed_hours_per_day()
                / A.OPERATING_DAYS_PER_YEAR, 3)),
        },
        "incumbent_sensitivity": incumbent_sensitivity(ops, harm, BASE, prices),
        "robustness_no_evidence_speedup": _robustness(ops, harm, coverage_mode, prices),
        "cht_revenue": {
            "basis": Basis.DERIVED.value,
            "hicloud_annual_ntd": round(base_case.cost.cht_hicloud, 0),
            "identity_verification_annual_ntd": round(base_case.cost.cht_identity, 0),
            "identity_verification_calls": round(base_case.cost.annual_approvals, 0),
            "identity_verification_unit_ntd": prices.cht_identity_verify_ntd,
            "total_annual_ntd": round(base_case.cost.cht_recognisable_annual_ntd, 0),
            "note": (
                "門號級身分驗證的呼叫量 = 送核准件數(每次 G4 核准呼叫一次)。"
                "若整包方案由中華電信轉售,可辨識收入為年度經常性成本全額 "
                f"{base_case.cost.annual_recurring_ntd:,.0f} + 一次性 "
                f"{base_case.cost.one_time:,.0f} NTD。"
            ),
        },
    }


def _robustness(
    ops: OpsMeasurement,
    harm: HarmMeasurement,
    coverage_mode: str,
    prices: PriceBook,
) -> dict[str, Any]:
    """把「證據包加速」那一條拿掉重算。

    為什麼要有這一節:那一條建立在規格 §4.5 的 15 秒設計目標上,而那個目標
    **還沒有做過真人計時**。一份把結論壓在未驗證假設上的商業案例,在問答時會被拆穿。
    這一節回答的是:拿掉它,基準情境還成不成立、破口臨界值變成多少。
    """
    bc = build_business_case(ops, harm, BASE, coverage_mode, prices,
                             include_evidence_speedup=False)
    points = breakeven(ops, harm, BASE, coverage_mode, prices,
                       include_evidence_speedup=False)
    return {
        "basis": Basis.DERIVED.value,
        "note": "拿掉「證據包加速」(15 秒為規格 §4.5 的設計目標,尚未真人計時)後重算。",
        "annual_benefit_ntd": round(bc.annual_benefit_ntd, 0),
        "annual_cost_ntd": round(bc.cost.annual_recurring_ntd, 0),
        "steady_roi_pct": (None if bc.steady_roi_pct is None
                           else round(bc.steady_roi_pct, 1)),
        "payback_months": (None if bc.payback_months is None
                           else round(bc.payback_months, 1)),
        "breakeven": [p.to_dict() for p in points],
    }


# ======================================================================================
# 報表稽核:不允許未標示來源的數字
# ======================================================================================
def audit_figures(obj: Any, path: str = "$") -> list[str]:
    """回傳報表中「所在字典沒有 ``basis`` 欄位」的數字路徑。

    測試用這個函式守住一條規則:**商業案例裡不能出現沒標明來源的數字。**
    """
    return _audit(obj, path, labelled=False)


def _audit(obj: Any, path: str, labelled: bool) -> list[str]:
    problems: list[str] = []
    if isinstance(obj, dict):
        here = labelled or ("basis" in obj)
        for key, value in obj.items():
            problems.extend(_audit(value, f"{path}.{key}", here))
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            problems.extend(_audit(value, f"{path}[{index}]", labelled))
    elif isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return problems
    elif isinstance(obj, (int, float)):
        if not labelled:
            problems.append(path)
    return problems


__all__ = [
    "BASE",
    "BENEFIT_LABELS",
    "BENEFIT_ORDER",
    "CASES",
    "CONSERVATIVE",
    "COVERAGE_MODES",
    "OPTIMISTIC",
    "BenefitLine",
    "BreakevenPoint",
    "BusinessCase",
    "ScenarioCase",
    "annual_actions",
    "audit_figures",
    "breakeven",
    "build_business_case",
    "build_business_case_with_coverage",
    "build_business_report",
    "coverage_for",
    "harm_expected_annual_ntd",
    "incumbent_sensitivity",
    "measured_assumptions",
]
