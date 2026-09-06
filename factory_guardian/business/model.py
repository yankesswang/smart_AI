"""年化模型 —— 把 110 分鐘的實測 KPI 換算成年度商業論據。

競賽研究文件 §5.5 直接給了公式：

    ROI =（避免停機損失 ＋ 節省工時 ＋ 降低報廢／能源／事故成本 － 方案成本）÷ 方案成本

這個模組把它算出來，而且守住三條規則：

1. **每一條效益都追得到一個實測 KPI 欄位**（``BenefitLine.kpi_fields``）。
   沒有 KPI 支撐的效益不會出現在表上 —— 例如「報廢成本」：本 MVP 沒有量測報廢率，
   所以 ROI **不計入報廢節省**。這是刻意留白，不是遺漏。
2. **實測 / 推導 / 假設三種來源全程標示**，報表可以被 :func:`audit_figures` 掃出未標示的數字。
3. **基準情境偏保守**，而且會誠實算出「在什麼假設下 ROI 不成立」（:func:`breakeven`）。

---
年化的三個步驟
---
::

    每事件效益(NTD) = Σ 各效益項( 現況KPI − Guardian KPI )      ← 實測差值 × 單價
    年度事件數      = 語料推導的事件率 × 產能衝擊比例 × 故障分布 × 頻率倍率
    年度效益        = 每事件效益 × 年度事件數 × 效益實現率 × 產線數

「現況 KPI」不是任一組對照組，而是兩組對照組的機率混合（見
``assumptions.INCUMBENT_ESCALATION_RATE`` 的說明）—— 因為逐 KPI 挑最好值會拼出一個
物理上不可能存在的超級對照組（例如同時擁有 Baseline A 的產能與 Baseline B 的零曝露，
而 A 的產能正是靠讓人曝露 88 分鐘換來的）。

---
一個必須講清楚的低估
---
每個 episode 只量 110 分鐘。Baseline A 在第 110 分鐘結束時設備健康度只剩 0.7 且已二次損壞，
那個事件的真實代價在視窗之後還會繼續發生 —— 我們不計。
另外，孿生體有一台完全閒置的替代機台 M-B 可以吸收轉單；沒有備援產能的產線損失會更大 —— 也不計。
**所有效益都截斷在量測視窗內。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..benchmark import BenchmarkReport
from . import assumptions as A
from .assumptions import BASIS_LABEL, Basis
from .competitors import competitor_report
from .pricing import (
    DEFAULT_PRICES,
    DeploymentScope,
    PriceBook,
    SolutionCost,
    build_solution_cost,
    plant_scope,
)

INCUMBENT_MODES = ("baseline-a", "baseline-b")
GUARDIAN_MODE = "guardian"


# ======================================================================================
# 敏感度情境
# ======================================================================================
@dataclass(frozen=True)
class SensitivityCase:
    """一組情境參數。三個情境只差在這四個數字上。"""

    key: str
    label: str
    escalation_rate: float          # 現況告警逃逸率
    realization: float              # 效益實現率
    frequency_multiplier: float     # 事件頻率倍率
    cost_multiplier: float          # 方案成本倍率
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": Basis.ASSUMPTION.value,
            "key": self.key,
            "label": self.label,
            "escalation_rate": round(self.escalation_rate, 4),
            "realization": round(self.realization, 4),
            "frequency_multiplier": round(self.frequency_multiplier, 4),
            "cost_multiplier": round(self.cost_multiplier, 4),
            "note": self.note,
        }

    def replace(self, **kwargs: float) -> "SensitivityCase":
        data = {
            "key": self.key, "label": self.label, "escalation_rate": self.escalation_rate,
            "realization": self.realization, "frequency_multiplier": self.frequency_multiplier,
            "cost_multiplier": self.cost_multiplier, "note": self.note,
        }
        data.update(kwargs)
        return SensitivityCase(**data)  # type: ignore[arg-type]


CONSERVATIVE = SensitivityCase(
    key="conservative", label="保守",
    escalation_rate=0.0, realization=0.60, frequency_multiplier=0.5, cost_multiplier=1.25,
    note="現況從不讓劣化走到二次損壞（等同 Baseline B：偵測即停機、技師零等待），"
         "效益只實現六成，事件數砍半，導入成本超支 25%。",
)
BASE = SensitivityCase(
    key="base", label="基準",
    escalation_rate=A.INCUMBENT_ESCALATION_RATE,
    realization=A.BENEFIT_REALIZATION_RATE,
    frequency_multiplier=1.0, cost_multiplier=1.0,
    note="四次告警有一次沒被及時接住；效益實現八成；事件數依語料推導；成本照定價。",
)
OPTIMISTIC = SensitivityCase(
    key="optimistic", label="樂觀",
    escalation_rate=0.50, realization=1.0, frequency_multiplier=1.6, cost_multiplier=0.9,
    note="現況一半的告警沒被接住；效益完全實現；事件數高於語料推導；規模化後成本下降一成。",
)
CASES: tuple[SensitivityCase, ...] = (CONSERVATIVE, BASE, OPTIMISTIC)


# ======================================================================================
# 效益項
# ======================================================================================
@dataclass(frozen=True)
class BenefitLine:
    """一條效益。``kpi_fields`` 是它追溯到的實測 KPI 欄位 —— 沒有這個欄位就不該有這條。"""

    key: str
    label: str
    kpi_fields: tuple[str, ...]
    basis: Basis
    per_event_ntd: float
    annual_ntd: float
    formula: str
    note: str = ""
    physical: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": self.basis.value,
            "basis_label": BASIS_LABEL[self.basis.value],
            "key": self.key,
            "label": self.label,
            "kpi_fields": list(self.kpi_fields),
            "per_event_ntd": round(self.per_event_ntd, 1),
            "annual_ntd": round(self.annual_ntd, 0),
            "formula": self.formula,
            "note": self.note,
            **{k: round(v, 3) for k, v in self.physical.items()},
        }


BENEFIT_LABELS: dict[str, str] = {
    "production_loss_avoided": "避免停機／降載的產出損失",
    "order_delay_cost_avoided": "交期延遲罰則與趕工成本",
    "secondary_damage_avoided": "二次損壞避免",
    "maintenance_labour_saved": "維修工時節省",
    "energy_waste_avoided": "能源浪費減少",
    "carbon_cost_avoided": "碳排成本減少",
    "safety_expected_loss_avoided": "工安事故期望損失減少",
}
BENEFIT_ORDER: tuple[str, ...] = tuple(BENEFIT_LABELS)


@dataclass
class ScenarioBenefit:
    """單一情境的效益拆解（每條產線）。"""

    scenario_id: str
    fault_id: str | None
    safety_only: bool
    annual_events: float
    duration_scale: float
    duration_scale_note: str
    lines: list[BenefitLine]
    deltas: dict[str, dict[str, float]]

    @property
    def per_event_ntd(self) -> float:
        return sum(line.per_event_ntd for line in self.lines)

    @property
    def annual_ntd(self) -> float:
        return sum(line.annual_ntd for line in self.lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": Basis.DERIVED.value,
            "scenario_id": self.scenario_id,
            "fault_id": self.fault_id or "—",
            "safety_only": self.safety_only,
            "annual_events": round(self.annual_events, 3),
            "duration_scale": round(self.duration_scale, 4),
            "duration_scale_note": self.duration_scale_note,
            "per_event_ntd": round(self.per_event_ntd, 1),
            "annual_ntd": round(self.annual_ntd, 0),
            "lines": [line.to_dict() for line in self.lines],
            "deltas_note": (
                "delta = 現況 − Guardian。對『越大越好』的欄位"
                "（例如 work_order_completeness_pct）負值代表 Guardian 較佳，"
                "效益公式已依各欄位的方向分別處理。"
            ),
            "measured_deltas": {
                key: {
                    "basis": Basis.MEASURED.value,
                    "incumbent": round(vals["incumbent"], 3),
                    "guardian": round(vals["guardian"], 3),
                    "delta": round(vals["delta"], 3),
                }
                for key, vals in self.deltas.items()
            },
        }


# ======================================================================================
# 商業案例
# ======================================================================================
@dataclass
class BusinessCase:
    case: SensitivityCase
    scope: DeploymentScope
    scenarios: list[ScenarioBenefit]
    cost: SolutionCost
    annual_events_per_line: float

    @property
    def annual_benefit_per_line_ntd(self) -> float:
        return sum(s.annual_ntd for s in self.scenarios)

    @property
    def annual_benefit_ntd(self) -> float:
        return self.annual_benefit_per_line_ntd * self.scope.lines

    @property
    def year_one_net_ntd(self) -> float:
        return self.annual_benefit_ntd - self.cost.year_one_ntd

    @property
    def steady_net_ntd(self) -> float:
        return self.annual_benefit_ntd - self.cost.annual_recurring_ntd

    @property
    def year_one_roi_pct(self) -> float | None:
        total = self.cost.year_one_ntd
        return None if total <= 0 else 100.0 * self.year_one_net_ntd / total

    @property
    def steady_roi_pct(self) -> float | None:
        total = self.cost.annual_recurring_ntd
        return None if total <= 0 else 100.0 * self.steady_net_ntd / total

    @property
    def payback_months(self) -> float | None:
        """簡單回收期：一次性投入 ÷ 每月淨效益（年度效益扣掉經常性費用後）。"""
        monthly_net = self.steady_net_ntd / 12.0
        if monthly_net <= 0:
            return None
        return self.cost.one_time_ntd / monthly_net

    def npv_ntd(self, years: int = 3, rate: float = A.DISCOUNT_RATE) -> float:
        npv = -self.cost.one_time_ntd
        for t in range(1, years + 1):
            npv += self.steady_net_ntd / ((1.0 + rate) ** t)
        return npv

    def benefit_rollup(self) -> dict[str, float]:
        """跨情境彙總每一條效益項的年度金額（每廠）。"""
        out: dict[str, float] = {key: 0.0 for key in BENEFIT_ORDER}
        for scenario in self.scenarios:
            for line in scenario.lines:
                out[line.key] = out.get(line.key, 0.0) + line.annual_ntd * self.scope.lines
        return out

    def physical_rollup(self) -> dict[str, float]:
        """年度的物理量彙總（能源 kWh、碳排 kg、危險曝露分鐘）—— 永續發展性用。"""
        out: dict[str, float] = {}
        for scenario in self.scenarios:
            for line in scenario.lines:
                for key, value in line.physical.items():
                    out[key] = out.get(key, 0.0) + value * self.scope.lines
        return out

    def to_dict(self) -> dict[str, Any]:
        payback = self.payback_months
        return {
            "case": self.case.to_dict(),
            "scope": self.scope.to_dict(),
            "cost": self.cost.to_dict(),
            "scenarios": [s.to_dict() for s in self.scenarios],
            "benefit_rollup": {
                "basis": Basis.DERIVED.value,
                **{f"{k}_ntd": round(v, 0) for k, v in self.benefit_rollup().items()},
            },
            "physical_rollup": {
                "basis": Basis.DERIVED.value,
                **{k: round(v, 3) for k, v in self.physical_rollup().items()},
            },
            "result": {
                "basis": Basis.DERIVED.value,
                "annual_events_per_line": round(self.annual_events_per_line, 2),
                "annual_benefit_per_line_ntd": round(self.annual_benefit_per_line_ntd, 0),
                "annual_benefit_ntd": round(self.annual_benefit_ntd, 0),
                "annual_cost_ntd": round(self.cost.annual_recurring_ntd, 0),
                "one_time_cost_ntd": round(self.cost.one_time_ntd, 0),
                "year_one_cost_ntd": round(self.cost.year_one_ntd, 0),
                "year_one_net_ntd": round(self.year_one_net_ntd, 0),
                "steady_net_ntd": round(self.steady_net_ntd, 0),
                "year_one_roi_pct": None if self.year_one_roi_pct is None else round(self.year_one_roi_pct, 1),
                "steady_roi_pct": None if self.steady_roi_pct is None else round(self.steady_roi_pct, 1),
                "payback_months": None if payback is None else round(payback, 1),
                "payback_note": "" if payback is not None else "淨效益為負，本情境不會回本。",
                "npv_3y_ntd": round(self.npv_ntd(3), 0),
                "roi_formula": "ROI =（年度效益 − 年度成本）÷ 年度成本（研究文件 §5.5）",
            },
        }


# ======================================================================================
# 核心：從 BenchmarkReport 推導效益
# ======================================================================================
def _kpis_by_mode(report: BenchmarkReport, scenario_id: str) -> dict[str, dict[str, Any]]:
    data = report.by_scenario(scenario_id)
    missing = [m for m in (*INCUMBENT_MODES, GUARDIAN_MODE) if m not in data]
    if missing:
        raise ValueError(
            f"情境 {scenario_id} 缺少模式 {missing}；商業案例需要三組對照組才能算差值。"
        )
    return data


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _incumbent(
    kpis: dict[str, dict[str, Any]], key: str, escalation: float, missing: float | None = None
) -> float:
    """現況 KPI＝逃逸率 × Baseline A ＋ (1−逃逸率) × Baseline B。

    ``missing`` 是當兩組對照組都沒有該欄位時（例如 ``time_to_diagnose_min``：
    對照組根本不做根因診斷）要採用的假設值。
    """
    a_raw = kpis["baseline-a"].get(key)
    b_raw = kpis["baseline-b"].get(key)
    if a_raw is None and b_raw is None and missing is not None:
        return missing
    a = _num(a_raw, _num(b_raw, missing or 0.0))
    b = _num(b_raw, _num(a_raw, missing or 0.0))
    return escalation * a + (1.0 - escalation) * b


def _order_delay_cost(delay_min: float, late_orders: float) -> float:
    """交期成本 = 最嚴重那張訂單的延遲罰則 ＋ 每張延遲訂單一次趕工。

    只用**最嚴重的一張**訂單計罰則（其餘延遲訂單的罰則不計），刻意低估。
    """
    return (
        delay_min * A.ORDER_DELAY_PENALTY_NTD_PER_MIN
        + late_orders * A.EXPEDITE_COST_PER_LATE_ORDER_NTD
    )


def _duration_scale(kpis: dict[str, dict[str, Any]], safety_only: bool) -> tuple[float, str]:
    """工安情境的量測視野縮放係數。

    ``hazard-zone`` 注入的是「人員全程滯留」的壓力測試，不是典型事件長度。
    縮放基準用**實測的最大曝露跨度**（不受逃逸率影響），所以係數在三個情境間是穩定的。
    """
    if not safety_only:
        return 1.0, "1.00（設備事件的量測視野即為事件本身，不縮放）"
    span = max(_num(kpis[m].get("hazard_exposure_min")) for m in INCUMBENT_MODES) - _num(
        kpis[GUARDIAN_MODE].get("hazard_exposure_min")
    )
    if span <= 0:
        return 1.0, "1.00（量測到的曝露差值為 0，不縮放）"
    scale = A.TYPICAL_INTRUSION_MIN / span
    return scale, (
        f"{scale:.3f} = 典型滯留 {A.TYPICAL_INTRUSION_MIN:.0f} min ÷ 實測曝露跨度 {span:.0f} min"
        "（壓力測試等比縮回典型事件長度）"
    )


def scenario_benefit(
    report: BenchmarkReport,
    scenario_id: str,
    case: SensitivityCase,
    annual_events: float,
) -> ScenarioBenefit:
    """算出單一情境、單一產線的效益拆解。"""
    kpis = _kpis_by_mode(report, scenario_id)
    guardian = kpis[GUARDIAN_MODE]
    ground_truth = next(
        (r.ground_truth for r in report.rows if r.scenario_id == scenario_id), {}
    )
    fault_id = next((v for k, v in ground_truth.items() if k != "SAFETY"), None)
    safety_only = fault_id is None
    e = case.escalation_rate

    scale, scale_note = _duration_scale(kpis, safety_only)
    realized = case.realization
    annual_factor = annual_events * scale * realized

    deltas: dict[str, dict[str, float]] = {}

    def record(key: str, incumbent: float, guardian_value: float) -> float:
        deltas[key] = {
            "incumbent": incumbent,
            "guardian": guardian_value,
            "delta": incumbent - guardian_value,
        }
        return incumbent - guardian_value

    lines: list[BenefitLine] = []

    def add(
        key: str, kpi_fields: tuple[str, ...], per_event: float, formula: str,
        basis: Basis = Basis.DERIVED, note: str = "", physical: dict[str, float] | None = None,
    ) -> None:
        lines.append(
            BenefitLine(
                key=key, label=BENEFIT_LABELS[key], kpi_fields=kpi_fields, basis=basis,
                per_event_ntd=per_event * scale,
                annual_ntd=per_event * annual_factor,
                formula=formula, note=note,
                physical={k: v * annual_factor for k, v in (physical or {}).items()},
            )
        )

    # -- 1. 避免停機／降載的產出損失 --------------------------------------------------
    inc_loss = _incumbent(kpis, "production_loss_ntd", e)
    delta_loss = record("production_loss_ntd", inc_loss, _num(guardian.get("production_loss_ntd")))
    add(
        "production_loss_avoided", ("production_loss_ntd",), delta_loss,
        f"現況 {inc_loss:,.0f} − Guardian {_num(guardian.get('production_loss_ntd')):,.0f} NTD"
        f"（production_loss_units × UNIT_MARGIN_NTD {A.ASSUMPTIONS['unit_margin_ntd'].value:g}）",
        basis=Basis.MEASURED,
        note="這就是 §5.5 公式裡的『避免停機損失』，以損失的邊際貢獻表達；"
             "同時涵蓋完全停機與劣化降載兩種產出損失，不與交期成本重複。",
    )

    # -- 2. 交期延遲罰則與趕工成本 ------------------------------------------------------
    inc_delay = _incumbent(kpis, "max_order_delay_min", e)
    inc_late = _incumbent(kpis, "late_orders", e)
    g_delay = _num(guardian.get("max_order_delay_min"))
    g_late = _num(guardian.get("late_orders"))
    record("max_order_delay_min", inc_delay, g_delay)
    record("late_orders", inc_late, g_late)
    delta_delay = _order_delay_cost(inc_delay, inc_late) - _order_delay_cost(g_delay, g_late)
    add(
        "order_delay_cost_avoided", ("max_order_delay_min", "late_orders"), delta_delay,
        f"（{inc_delay:,.0f}−{g_delay:,.0f} min × {A.ORDER_DELAY_PENALTY_NTD_PER_MIN:g} NTD/min）"
        f" + （{inc_late:.2f}−{g_late:.2f} 張 × {A.EXPEDITE_COST_PER_LATE_ORDER_NTD:,.0f} NTD）",
        note="罰則只計最嚴重的一張訂單，其餘延遲訂單不計 —— 刻意低估。",
    )

    # -- 3. 二次損壞避免 ----------------------------------------------------------------
    repair_cost = A.secondary_damage_cost(fault_id)
    inc_secondary = _incumbent(kpis, "secondary_damage", e)
    g_secondary = _num(guardian.get("secondary_damage"))
    delta_secondary = record("secondary_damage", inc_secondary, g_secondary)
    add(
        "secondary_damage_avoided", ("secondary_damage",), delta_secondary * repair_cost,
        f"（{inc_secondary:.2f}−{g_secondary:.2f} 次）× {repair_cost:,.0f} NTD"
        f"（twin/faults.py::{fault_id or 'default'}.secondary_damage_cost_ntd）",
        note="現況的二次損壞機率就是逃逸率本身 —— Baseline A 實測會二次損壞，Baseline B 不會。",
    )

    # -- 4. 維修工時節省 ----------------------------------------------------------------
    if safety_only:
        add(
            "maintenance_labour_saved", ("time_to_diagnose_min", "work_order_completeness_pct"),
            0.0, "工安情境無設備根因判定，此項為 0。",
            basis=Basis.DERIVED, note="不把工安事件算成維修工時節省。",
        )
    else:
        g_diag = _num(guardian.get("time_to_diagnose_min"), A.MANUAL_ROOT_CAUSE_MIN)
        inc_diag = _incumbent(kpis, "time_to_diagnose_min", e, missing=A.MANUAL_ROOT_CAUSE_MIN)
        record("time_to_diagnose_min", inc_diag, g_diag)
        diag_saving = max(0.0, inc_diag - g_diag) / 60.0 * A.MAINTENANCE_TECHNICIAN_HOURLY_NTD

        g_complete = _num(guardian.get("work_order_completeness_pct"), A.MANUAL_WORK_ORDER_COMPLETENESS_PCT)
        inc_complete = _incumbent(
            kpis, "work_order_completeness_pct", e, missing=A.MANUAL_WORK_ORDER_COMPLETENESS_PCT
        )
        record("work_order_completeness_pct", inc_complete, g_complete)
        trip_saving = (
            max(0.0, g_complete - inc_complete) / 100.0
            * A.WORK_ORDER_REWORK_TRIP_HOURS
            * A.MAINTENANCE_TECHNICIAN_HOURLY_NTD
        )
        add(
            "maintenance_labour_saved",
            ("time_to_diagnose_min", "work_order_completeness_pct"),
            diag_saving + trip_saving,
            f"根因判定（{inc_diag:.0f}−{g_diag:.0f} min）÷60 × {A.MAINTENANCE_TECHNICIAN_HOURLY_NTD:,.0f}"
            f" + 工單完整度（{g_complete:.0f}−{inc_complete:.0f}%）× "
            f"{A.WORK_ORDER_REWORK_TRIP_HOURS:g} hr × {A.MAINTENANCE_TECHNICIAN_HOURLY_NTD:,.0f}",
            note="現況的根因判定工時與工單完整度是假設值（對照組不做診斷、不開工單）；"
                 "Guardian 側是實測。",
        )

    # -- 5. 能源浪費減少 ----------------------------------------------------------------
    inc_energy_ntd = _incumbent(kpis, "energy_waste_ntd", e)
    g_energy_ntd = _num(guardian.get("energy_waste_ntd"))
    delta_energy = record("energy_waste_ntd", inc_energy_ntd, g_energy_ntd)
    inc_energy_kwh = _incumbent(kpis, "energy_waste_kwh", e)
    g_energy_kwh = _num(guardian.get("energy_waste_kwh"))
    record("energy_waste_kwh", inc_energy_kwh, g_energy_kwh)
    add(
        "energy_waste_avoided", ("energy_waste_ntd", "energy_waste_kwh"), delta_energy,
        f"（{inc_energy_kwh:.2f}−{g_energy_kwh:.2f} kWh）× "
        f"{A.ASSUMPTIONS['electricity_tariff_ntd_per_kwh'].value:g} NTD/kWh",
        basis=Basis.MEASURED,
        note="劣化多耗的電由孿生體逐 tick 積分（twin/energy.py），不是事後估算。"
             "Guardian 有時略高於 Baseline B —— 因為 B 直接把機台停掉，停機當然不浪費電。",
        physical={"energy_waste_avoided_kwh": inc_energy_kwh - g_energy_kwh},
    )

    # -- 6. 碳排成本減少 ----------------------------------------------------------------
    inc_co2 = _incumbent(kpis, "co2e_waste_kg", e)
    g_co2 = _num(guardian.get("co2e_waste_kg"))
    delta_co2 = record("co2e_waste_kg", inc_co2, g_co2)
    add(
        "carbon_cost_avoided", ("co2e_waste_kg",),
        delta_co2 / 1000.0 * A.CARBON_FEE_NTD_PER_TONNE,
        f"（{inc_co2:.2f}−{g_co2:.2f} kgCO2e）÷1000 × {A.CARBON_FEE_NTD_PER_TONNE:g} NTD/公噸",
        note="金額量級極小，對 ROI 不具實質影響；它的價值在 ESG 揭露與碳盤查，不在財務回收。",
        physical={"co2e_waste_avoided_kg": delta_co2},
    )

    # -- 7. 工安事故期望損失減少 --------------------------------------------------------
    inc_hazard = _incumbent(kpis, "hazard_exposure_min", e)
    g_hazard = _num(guardian.get("hazard_exposure_min"))
    delta_hazard = record("hazard_exposure_min", inc_hazard, g_hazard)
    add(
        "safety_expected_loss_avoided", ("hazard_exposure_min",),
        delta_hazard / 60.0 * A.HAZARD_INJURY_PROBABILITY_PER_EXPOSURE_HOUR * A.SAFETY_INCIDENT_COST_NTD,
        f"（{inc_hazard:.1f}−{g_hazard:.1f} min）÷60 × "
        f"{A.HAZARD_INJURY_PROBABILITY_PER_EXPOSURE_HOUR:g}/hr × {A.SAFETY_INCIDENT_COST_NTD:,.0f} NTD",
        note="曝露致傷機率沒有公開統計可引用，是純假設；破口分析會給出臨界值。",
        physical={"hazard_exposure_avoided_min": delta_hazard},
    )

    return ScenarioBenefit(
        scenario_id=scenario_id,
        fault_id=fault_id,
        safety_only=safety_only,
        annual_events=annual_events,
        duration_scale=scale,
        duration_scale_note=scale_note,
        lines=lines,
        deltas=deltas,
    )


# ======================================================================================
# 年度事件數配置
# ======================================================================================
def annual_events_by_scenario(
    report: BenchmarkReport, frequency_multiplier: float = 1.0
) -> dict[str, float]:
    """把年度事件數分配到各情境。

    設備事件池 = 語料推導的年事件率 × 產能衝擊比例 × 頻率倍率，
    再依語料的故障分布切給各故障；其中 ``SIMULTANEOUS_INTRUSION_RATE`` 的比例
    切給「同時有人員闖入」的複合情境 —— **是切出去，不是加上去**，所以不會重複計算。
    工安虞慮事件另有獨立頻率，因為它與設備故障無關。
    """
    equipment_pool = (
        A.ANNUAL_MAINTENANCE_EVENTS_PER_LINE
        * A.PRODUCTION_IMPACTING_EVENT_SHARE
        * frequency_multiplier
    )
    hazard_pool = A.ANNUAL_HAZARD_INTRUSION_EVENTS_PER_LINE * frequency_multiplier

    # 先分組：同一個 (fault_id, 是否含工安) 的情境平分該格的次數，避免重複計算。
    #
    # **無故障的干擾情境（fp-*）不是事件，年度次數為 0。** 它們是 Benchmark 用來量誤報率的
    # 壓力測試：機台完全健康，任何「效益」都只是 Guardian 沒白停機而 Baseline B 白停了的差額。
    # 把它們算進事件池，等於把「客戶工廠每年會發生 N 次假警報」當成效益來源年化 ——
    # 那個 N 沒有任何依據，而且會把工安事件池平分給它們、稀釋真正的工安效益。
    # 誤報的代價已經在 Benchmark 的誤報彙總與 §5 刻意不計入的項目裡誠實揭露，不進 ROI。
    buckets: dict[tuple[str | None, bool], list[str]] = {}
    out: dict[str, float] = {}
    for scenario_id in report.scenarios():
        gt = next((r.ground_truth for r in report.rows if r.scenario_id == scenario_id), {})
        if not gt:
            out[scenario_id] = 0.0
            continue
        fault_id = next((v for k, v in gt.items() if k != "SAFETY"), None)
        buckets.setdefault((fault_id, "SAFETY" in gt), []).append(scenario_id)

    for (fault_id, has_safety), scenario_ids in buckets.items():
        if fault_id is None:
            share = hazard_pool
        else:
            mix = A.FAULT_MIX.get(fault_id, 0.0)
            intrusion = A.SIMULTANEOUS_INTRUSION_RATE if has_safety else (1.0 - A.SIMULTANEOUS_INTRUSION_RATE)
            share = equipment_pool * mix * intrusion
        for scenario_id in scenario_ids:
            out[scenario_id] = share / len(scenario_ids)
    return out


# ======================================================================================
# 組裝
# ======================================================================================
def build_business_case(
    report: BenchmarkReport,
    case: SensitivityCase = BASE,
    scope: DeploymentScope | None = None,
    prices: PriceBook = DEFAULT_PRICES,
) -> BusinessCase:
    """吃 :class:`BenchmarkReport` 的實測 KPI，產出一個情境的商業案例。"""
    scope = scope or plant_scope()
    events = annual_events_by_scenario(report, case.frequency_multiplier)
    scenarios = [
        scenario_benefit(report, scenario_id, case, events.get(scenario_id, 0.0))
        for scenario_id in report.scenarios()
    ]
    annual_events_per_line = sum(events.values())
    cost = build_solution_cost(
        scope,
        annual_agent_runs=annual_events_per_line * scope.lines,
        prices=prices,
        cost_multiplier=case.cost_multiplier,
    )
    return BusinessCase(
        case=case, scope=scope, scenarios=scenarios, cost=cost,
        annual_events_per_line=annual_events_per_line,
    )


# ======================================================================================
# 破口分析：在什麼假設下 ROI 不成立
# ======================================================================================
def _solve(
    fn: Callable[[float], float], lo: float, hi: float, steps: int = 60
) -> float | None:
    """單調遞增函式的二分求根；不在區間內回傳 None。"""
    f_lo, f_hi = fn(lo), fn(hi)
    if f_lo >= 0:
        return lo
    if f_hi < 0:
        return None
    for _ in range(steps):
        mid = (lo + hi) / 2.0
        if fn(mid) < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


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
            "breakeven_value": None if self.breakeven_value is None else round(self.breakeven_value, 4),
            "unit": self.unit,
            "verdict": self.verdict,
        }


def breakeven(
    report: BenchmarkReport,
    case: SensitivityCase = BASE,
    scope: DeploymentScope | None = None,
    prices: PriceBook = DEFAULT_PRICES,
) -> list[BreakevenPoint]:
    """算出讓「年度效益 = 年度經常性成本」的臨界參數值。

    這一節存在的理由：全部樂觀的 ROI 沒有說服力，
    **誠實說出在什麼假設下方案不成立才有**（研究文件 §5.3：使用明確公式與保守假設）。
    """
    scope = scope or plant_scope()

    def net(new_case: SensitivityCase, lines: int | None = None) -> float:
        target_scope = scope if lines is None else DeploymentScope(
            key=scope.key, label=scope.label, lines=lines,
            machines_per_line=scope.machines_per_line, cameras_per_line=scope.cameras_per_line,
            managed_tier=scope.managed_tier, note=scope.note,
        )
        bc = build_business_case(report, new_case, target_scope, prices)
        return bc.steady_net_ntd

    points: list[BreakevenPoint] = []

    def add(key: str, label: str, base_value: float, value: float | None, unit: str, ok_note: str, fail_note: str) -> None:
        verdict = fail_note if value is None else ok_note.format(value=value)
        points.append(BreakevenPoint(key, label, base_value, value, unit, verdict))

    escalation = _solve(lambda x: net(case.replace(escalation_rate=x)), 0.0, 1.0)
    add(
        "escalation_rate", "現況告警逃逸率", case.escalation_rate, escalation, "比例",
        "逃逸率需 ≥ {value:.1%} 才回本；低於此值代表現行流程已能接住所有劣化，本方案在財務上不成立。",
        "即使現況 100% 的告警都逃逸，年度效益仍不足以覆蓋經常性成本。",
    )

    realization = _solve(lambda x: net(case.replace(realization=x)), 0.0, 2.0)
    add(
        "realization", "效益實現率", case.realization, realization, "比例",
        "實現率需 ≥ {value:.1%}；低於此值代表現場落地折損過大，方案不成立。",
        "即使效益 200% 實現仍無法覆蓋成本。",
    )

    frequency = _solve(lambda x: net(case.replace(frequency_multiplier=x)), 0.0, 6.0)
    add(
        "frequency_multiplier", "事件頻率倍率", case.frequency_multiplier, frequency, "倍",
        "事件頻率需 ≥ 語料推導值的 {value:.2f} 倍；設備狀況比語料好的工廠，本方案不划算。",
        "即使事件頻率是語料推導值的 6 倍仍無法回本。",
    )

    # 成本倍率是「越大越差」，所以代入 6−x 反轉單調方向再求根，
    # 得到讓淨效益歸零的**最大**成本倍率。
    cost_cap = _solve(lambda x: net(case.replace(cost_multiplier=6.0 - x)), 0.0, 6.0)
    cost_cap = None if cost_cap is None else 6.0 - cost_cap
    add(
        "cost_multiplier", "方案成本倍率上限", case.cost_multiplier, cost_cap, "倍",
        "成本不得超過定價的 {value:.2f} 倍；超過即無法回本。",
        "在任何成本水準下都無法回本。",
    )

    min_lines: int | None = None
    for lines in range(1, 25):
        if net(case, lines=lines) > 0:
            min_lines = lines
            break
    add(
        "min_lines", "回本所需最少產線數", float(scope.lines), None if min_lines is None else float(min_lines),
        "條",
        "至少需部署 {value:.0f} 條產線；廠級固定成本（5G 專網 + MEC + 平台基礎費）必須攤在多條線上。",
        "24 條產線以內都無法回本。",
    )
    return points


def safety_tradeoff(report: BenchmarkReport) -> dict[str, Any] | None:
    """工安情境的取捨算式：**每移除一分鐘人員曝露，放棄多少邊際貢獻。**

    這一段刻意拿最不利的比較基準（Baseline A：產能最漂亮的那一組），
    而且刻意不把致傷機率調到讓數字好看的位置，改成直接給出臨界值。
    工安是硬限制，不是加權項 —— 它不需要靠 ROI 撐起來。
    """
    scenario_id = next(
        (
            sid
            for sid in report.scenarios()
            if all(
                k == "SAFETY"
                for k in next((r.ground_truth for r in report.rows if r.scenario_id == sid), {})
            )
            and any(r.ground_truth for r in report.rows if r.scenario_id == sid)
        ),
        None,
    )
    if scenario_id is None:
        return None
    kpis = _kpis_by_mode(report, scenario_id)
    guardian, baseline_a = kpis[GUARDIAN_MODE], kpis["baseline-a"]

    exposure_avoided = _num(baseline_a.get("hazard_exposure_min")) - _num(
        guardian.get("hazard_exposure_min")
    )
    production_cost = _num(guardian.get("production_loss_ntd")) - _num(
        baseline_a.get("production_loss_ntd")
    )
    if exposure_avoided <= 0:
        return None
    hours = exposure_avoided / 60.0
    expected_loss = hours * A.HAZARD_INJURY_PROBABILITY_PER_EXPOSURE_HOUR * A.SAFETY_INCIDENT_COST_NTD
    return {
        "basis": Basis.DERIVED.value,
        "scenario_id": scenario_id,
        "exposure_avoided_min": round(exposure_avoided, 1),
        "production_cost_ntd": round(production_cost, 0),
        "ntd_per_exposure_minute": round(production_cost / exposure_avoided, 1),
        "expected_loss_avoided_ntd": round(expected_loss, 0),
        "net_ntd": round(expected_loss - production_cost, 0),
        "breakeven_injury_probability_per_hour": round(
            production_cost / (hours * A.SAFETY_INCIDENT_COST_NTD), 5
        ),
        "breakeven_incident_cost_ntd": round(
            production_cost / (hours * A.HAZARD_INJURY_PROBABILITY_PER_EXPOSURE_HOUR), 0
        ),
        "comparison": "對照 Baseline A（產能最漂亮的一組），刻意採用對我方最不利的比較基準。",
        "stance": (
            "在假設的致傷機率下這一項在純財務上是負的，我們不調整假設讓它變好看。"
            "工安是硬限制，不是加權項。"
        ),
    }


def incumbent_sensitivity(
    report: BenchmarkReport,
    case: SensitivityCase = BASE,
    scope: DeploymentScope | None = None,
    prices: PriceBook = DEFAULT_PRICES,
) -> list[dict[str, Any]]:
    """把「拿什麼跟我們比」這件事的影響攤開 —— 這是整份 ROI 最敏感的一根軸。"""
    scope = scope or plant_scope()
    variants = (
        (0.0, "純 Baseline B（偵測即停機、技師零等待）", "最強的現況假設：告警永遠被接住。"),
        (case.escalation_rate, f"混合現況（逃逸率 {case.escalation_rate:.0%}）", "基準情境採用值。"),
        (1.0, "純 Baseline A（固定門檻告警、之後不處理）", "最弱的現況假設：告警從不被接住。"),
    )
    out: list[dict[str, Any]] = []
    for escalation, label, note in variants:
        bc = build_business_case(report, case.replace(escalation_rate=escalation), scope, prices)
        payback = bc.payback_months
        out.append(
            {
                "basis": Basis.DERIVED.value,
                "label": label,
                "note": note,
                "escalation_rate": round(escalation, 3),
                "annual_benefit_ntd": round(bc.annual_benefit_ntd, 0),
                "steady_roi_pct": None if bc.steady_roi_pct is None else round(bc.steady_roi_pct, 1),
                "payback_months": None if payback is None else round(payback, 1),
            }
        )
    return out


# ======================================================================================
# 完整報表
# ======================================================================================
def build_business_report(
    report: BenchmarkReport,
    scope: DeploymentScope | None = None,
    prices: PriceBook = DEFAULT_PRICES,
    cases: Iterable[SensitivityCase] = CASES,
    entry_scope: DeploymentScope | None = None,
) -> dict[str, Any]:
    """完整商業案例報表：三情境 ROI、成本結構、破口分析與競品比較。"""
    from .pricing import pilot_scope

    scope = scope or plant_scope()
    entry_scope = entry_scope or pilot_scope()
    cases = list(cases)
    built = [build_business_case(report, c, scope, prices) for c in cases]
    entry = build_business_case(report, BASE, entry_scope, prices)

    return {
        "meta": {
            "title": "Factory Guardian AI｜商業案例",
            "roi_formula": "ROI =（避免停機損失 ＋ 節省工時 ＋ 降低報廢／能源／事故成本 － 方案成本）÷ 方案成本",
            "roi_source": "2026 中華電信智慧創新應用大賽研究文件 §5.5",
            "kpi_source": "factory_guardian.benchmark.run_benchmark()：相同 seed、相同情境、相同總時長的三組對照組",
            "scenarios": list(report.scenarios()),
            "not_counted": (
                "本 MVP 未量測報廢率，故 ROI 不計入報廢節省；"
                "每個 episode 只量 110 分鐘，視窗之後仍會發生的代價不計；"
                "孿生體有一台完全閒置的替代機台可吸收轉單，沒有備援產能的產線損失會更大 —— 也不計。"
            ),
            "disclaimer": A.DISCLAIMER,
        },
        "assumptions": A.assumptions_to_list(),
        "cases": [bc.to_dict() for bc in built],
        "entry_deployment": entry.to_dict(),
        "breakeven": [p.to_dict() for p in breakeven(report, BASE, scope, prices)],
        "safety_tradeoff": safety_tradeoff(report),
        "incumbent_sensitivity": incumbent_sensitivity(report, BASE, scope, prices),
        "competitors": competitor_report(),
    }


# ======================================================================================
# 報表稽核：不允許未標示來源的數字
# ======================================================================================
def audit_figures(obj: Any, path: str = "$") -> list[str]:
    """回傳報表中「所在字典沒有 ``basis`` 欄位」的數字路徑。

    測試用這個函式守住一條規則：**商業案例裡不能出現沒標明來源的數字。**
    規則是「一個數值純量的最近一層字典必須帶 ``basis``」；串列繼承其所屬字典。
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
    "SensitivityCase",
    "CONSERVATIVE",
    "BASE",
    "OPTIMISTIC",
    "CASES",
    "BenefitLine",
    "BENEFIT_LABELS",
    "BENEFIT_ORDER",
    "ScenarioBenefit",
    "BusinessCase",
    "BreakevenPoint",
    "scenario_benefit",
    "annual_events_by_scenario",
    "build_business_case",
    "build_business_report",
    "breakeven",
    "safety_tradeoff",
    "incumbent_sensitivity",
    "audit_figures",
]
