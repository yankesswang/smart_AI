"""商業案例層的測試。

這一層不做量測，只做換算，所以測試要守的東西也不一樣：

* 換算**對得上輸入的 KPI**（不是「跑得動」而已）。
* 保守情境永遠不會比樂觀情境好看。
* 假設參數往好的方向動，結果就往好的方向動（單調性）—— 沒有這條，敏感度分析是假的。
* 報表裡**不存在沒標明來源的數字**。
* 每一條效益都追得到一個真實存在的 ``EpisodeKPI`` 欄位（不能憑空生出效益）。
"""

from __future__ import annotations

import json
import math

import pytest

from factory_guardian.benchmark import BenchmarkReport, BenchmarkRow
from factory_guardian.business import assumptions as A
from factory_guardian.business import (
    BASE,
    CASES,
    CONSERVATIVE,
    OPTIMISTIC,
    annual_events_by_scenario,
    audit_figures,
    breakeven,
    build_business_case,
    build_business_report,
    competitor_report,
    pilot_scope,
    plant_scope,
    safety_tradeoff,
)
from factory_guardian.business.model import BENEFIT_ORDER, scenario_benefit
from factory_guardian.business.pricing import DEFAULT_PRICES, build_solution_cost
from factory_guardian.cli import main
from factory_guardian.episode import EpisodeKPI

EQUIPMENT = "t-bearing"
SAFETY = "t-hazard"


def _row(scenario_id: str, mode: str, ground_truth: dict[str, str], **kpi: object) -> BenchmarkRow:
    """用 ``EpisodeKPI`` 的預設值當底，只覆寫測試關心的欄位。

    以預設值當底是刻意的：如果有人在 EpisodeKPI 加了欄位而商業層沒跟上，
    這裡不會默默產生 KeyError 掩蓋掉問題。
    """
    data = EpisodeKPI().to_dict()
    data.update(kpi)
    return BenchmarkRow(scenario_id=scenario_id, mode=mode, kpi=data, ground_truth=ground_truth)


@pytest.fixture
def report() -> BenchmarkReport:
    """一份可以手算的合成 Benchmark 結果。"""
    equipment_gt = {"M-A": "bearing_degradation"}
    safety_gt = {"SAFETY": "hazard_zone_intrusion"}
    return BenchmarkReport(
        rows=[
            _row(EQUIPMENT, "baseline-a", equipment_gt,
                 production_loss_ntd=30_000.0, max_order_delay_min=1_000.0, late_orders=2,
                 secondary_damage=True, energy_waste_ntd=20.0, energy_waste_kwh=6.0,
                 co2e_waste_kg=3.0, hazard_exposure_min=0.0),
            _row(EQUIPMENT, "baseline-b", equipment_gt,
                 production_loss_ntd=10_000.0, max_order_delay_min=0.0, late_orders=0,
                 secondary_damage=False, energy_waste_ntd=1.0, energy_waste_kwh=0.3,
                 co2e_waste_kg=0.15, hazard_exposure_min=0.0),
            _row(EQUIPMENT, "guardian", equipment_gt,
                 production_loss_ntd=1_000.0, max_order_delay_min=0.0, late_orders=0,
                 secondary_damage=False, energy_waste_ntd=0.5, energy_waste_kwh=0.15,
                 co2e_waste_kg=0.07, hazard_exposure_min=0.0,
                 time_to_diagnose_min=5.0, work_order_completeness_pct=100.0),
            _row(SAFETY, "baseline-a", safety_gt,
                 production_loss_ntd=500.0, hazard_exposure_min=80.0),
            _row(SAFETY, "baseline-b", safety_gt,
                 production_loss_ntd=14_000.0, hazard_exposure_min=0.0),
            _row(SAFETY, "guardian", safety_gt,
                 production_loss_ntd=11_000.0, hazard_exposure_min=1.0,
                 work_order_completeness_pct=80.0),
        ]
    )


# ======================================================================================
# 1. 換算對得上輸入的 KPI
# ======================================================================================
def test_每一條效益都算得出與輸入_kpi_一致的數字(report: BenchmarkReport) -> None:
    case = BASE
    e = case.escalation_rate
    benefit = scenario_benefit(report, EQUIPMENT, case, annual_events=1.0)
    lines = {line.key: line for line in benefit.lines}

    # 現況 = 逃逸率 × Baseline A + (1−逃逸率) × Baseline B
    inc_loss = e * 30_000.0 + (1 - e) * 10_000.0
    assert lines["production_loss_avoided"].per_event_ntd == pytest.approx(inc_loss - 1_000.0)

    inc_delay = e * 1_000.0
    inc_late = e * 2.0
    expected_delay = (
        inc_delay * A.ORDER_DELAY_PENALTY_NTD_PER_MIN
        + inc_late * A.EXPEDITE_COST_PER_LATE_ORDER_NTD
    )
    assert lines["order_delay_cost_avoided"].per_event_ntd == pytest.approx(expected_delay)

    # 二次損壞成本直接讀 twin/faults.py 的既有常數，不另外定義一份。
    expected_secondary = e * A.secondary_damage_cost("bearing_degradation")
    assert lines["secondary_damage_avoided"].per_event_ntd == pytest.approx(expected_secondary)

    expected_labour = (
        (A.MANUAL_ROOT_CAUSE_MIN - 5.0) / 60.0 * A.MAINTENANCE_TECHNICIAN_HOURLY_NTD
        + (100.0 - A.MANUAL_WORK_ORDER_COMPLETENESS_PCT) / 100.0
        * A.WORK_ORDER_REWORK_TRIP_HOURS
        * A.MAINTENANCE_TECHNICIAN_HOURLY_NTD
    )
    assert lines["maintenance_labour_saved"].per_event_ntd == pytest.approx(expected_labour)

    inc_energy = e * 20.0 + (1 - e) * 1.0
    assert lines["energy_waste_avoided"].per_event_ntd == pytest.approx(inc_energy - 0.5)

    inc_co2 = e * 3.0 + (1 - e) * 0.15
    assert lines["carbon_cost_avoided"].per_event_ntd == pytest.approx(
        (inc_co2 - 0.07) / 1000.0 * A.CARBON_FEE_NTD_PER_TONNE
    )

    # 設備情境沒有危險曝露 → 工安效益必須是 0，不能憑空生出來。
    assert lines["safety_expected_loss_avoided"].per_event_ntd == pytest.approx(0.0)


def test_roi_等於效益減成本除以成本(report: BenchmarkReport) -> None:
    bc = build_business_case(report, BASE, plant_scope(4))
    annual = bc.annual_benefit_ntd
    recurring = bc.cost.annual_recurring_ntd
    year_one = bc.cost.year_one_ntd

    assert bc.steady_roi_pct == pytest.approx(100.0 * (annual - recurring) / recurring)
    assert bc.year_one_roi_pct == pytest.approx(100.0 * (annual - year_one) / year_one)
    # 年度效益 = 每線效益 × 產線數，沒有其他隱藏乘數。
    assert annual == pytest.approx(bc.annual_benefit_per_line_ntd * 4)


def test_回收期等於一次性投入除以每月淨效益(report: BenchmarkReport) -> None:
    bc = build_business_case(report, BASE, plant_scope(6))
    monthly_net = (bc.annual_benefit_ntd - bc.cost.annual_recurring_ntd) / 12.0
    assert monthly_net > 0
    assert bc.payback_months == pytest.approx(bc.cost.one_time_ntd / monthly_net)


def test_淨效益為負時不謊報回收期(report: BenchmarkReport) -> None:
    # 單線試點的效益攤不平廠級固定成本，這時必須回報「不會回本」而不是一個大數字。
    bc = build_business_case(report, CONSERVATIVE, pilot_scope())
    assert bc.steady_net_ntd < 0
    assert bc.payback_months is None
    assert "不會回本" in bc.to_dict()["result"]["payback_note"]


# ======================================================================================
# 2. 保守 ≤ 基準 ≤ 樂觀
# ======================================================================================
def test_保守情境不會比樂觀情境好看(report: BenchmarkReport) -> None:
    scope = plant_scope(6)
    built = {c.key: build_business_case(report, c, scope) for c in CASES}
    conservative, base, optimistic = built["conservative"], built["base"], built["optimistic"]

    assert conservative.annual_benefit_ntd < base.annual_benefit_ntd < optimistic.annual_benefit_ntd
    assert conservative.steady_net_ntd < base.steady_net_ntd < optimistic.steady_net_ntd
    assert conservative.steady_roi_pct < base.steady_roi_pct < optimistic.steady_roi_pct
    assert conservative.year_one_roi_pct < base.year_one_roi_pct < optimistic.year_one_roi_pct
    assert conservative.npv_ntd(3) < base.npv_ntd(3) < optimistic.npv_ntd(3)

    # 成本方向相反：保守情境假設成本超支。
    assert conservative.cost.annual_recurring_ntd > base.cost.annual_recurring_ntd
    assert base.cost.annual_recurring_ntd > optimistic.cost.annual_recurring_ntd


def test_回收期在三情境間單調變差(report: BenchmarkReport) -> None:
    scope = plant_scope(6)
    paybacks = [
        build_business_case(report, c, scope).payback_months or math.inf
        for c in (OPTIMISTIC, BASE, CONSERVATIVE)
    ]
    assert paybacks[0] <= paybacks[1] <= paybacks[2]


def test_基準情境偏保守(report: BenchmarkReport) -> None:
    """研究文件 §5.3 明講「使用明確公式與保守假設」，所以基準不能是中間值就算數。"""
    assert BASE.realization < 1.0                      # 效益不會全額實現
    assert BASE.escalation_rate < 0.5                  # 現況假設偏強（不預設現行流程很糟）
    assert CONSERVATIVE.escalation_rate == 0.0         # 保守情境用最強的現況：Baseline B
    assert BASE.frequency_multiplier <= OPTIMISTIC.frequency_multiplier


# ======================================================================================
# 3. 單調性 —— 假設參數動，結果必須跟著動
# ======================================================================================
@pytest.mark.parametrize(
    "field,values",
    [
        ("escalation_rate", (0.0, 0.2, 0.4, 0.8, 1.0)),
        ("realization", (0.2, 0.5, 0.8, 1.0)),
        ("frequency_multiplier", (0.25, 0.5, 1.0, 2.0)),
    ],
)
def test_效益隨假設參數單調遞增(report: BenchmarkReport, field: str, values: tuple[float, ...]) -> None:
    scope = plant_scope(6)
    benefits = [
        build_business_case(report, BASE.replace(**{field: v}), scope).annual_benefit_ntd
        for v in values
    ]
    assert benefits == sorted(benefits)
    assert benefits[0] < benefits[-1]      # 不能只是「沒變差」，必須真的有反應


def test_roi_隨方案成本單調遞減(report: BenchmarkReport) -> None:
    scope = plant_scope(6)
    rois = [
        build_business_case(report, BASE.replace(cost_multiplier=m), scope).steady_roi_pct
        for m in (0.8, 1.0, 1.5, 2.0)
    ]
    assert rois == sorted(rois, reverse=True)


def test_效益隨產線數線性放大(report: BenchmarkReport) -> None:
    one = build_business_case(report, BASE, plant_scope(1)).annual_benefit_ntd
    five = build_business_case(report, BASE, plant_scope(5)).annual_benefit_ntd
    assert five == pytest.approx(one * 5)


# ======================================================================================
# 4. 報表不含未標示來源的數字
# ======================================================================================
def test_報表沒有未標示來源的數字(report: BenchmarkReport) -> None:
    data = build_business_report(report, scope=plant_scope(6))
    assert audit_figures(data) == []


def test_稽核器真的抓得到未標示的數字() -> None:
    """如果稽核器永遠回空串列，上面那條測試就是假的。"""
    assert audit_figures({"count": 3}) == ["$.count"]
    assert audit_figures({"basis": "measured", "count": 3}) == []
    assert audit_figures({"rows": [{"n": 1}]}) == ["$.rows[0].n"]
    assert audit_figures({"basis": "derived", "rows": [{"n": 1}]}) == []
    # 布林與字串不是「數字」，不該被當成未標示的憑空數值。
    assert audit_figures({"flag": True, "name": "x", "empty": None}) == []


def test_每個假設參數都標明依據與來源() -> None:
    assert A.ASSUMPTIONS, "假設表不能是空的 —— 那等於沒有揭露任何假設"
    for key, item in A.ASSUMPTIONS.items():
        assert item.key == key
        assert item.basis in tuple(A.Basis)
        assert item.source.strip(), f"{key} 沒有寫來源"
        assert item.rationale.strip(), f"{key} 沒有寫推估過程"
        assert item.unit.strip(), f"{key} 沒有單位"
    # 不允許把假設包裝成產業統計。
    for item in A.ASSUMPTIONS.values():
        if item.basis is A.Basis.ASSUMPTION:
            assert "業界平均" not in item.source


def test_每條效益都追得到真實存在的_kpi_欄位(report: BenchmarkReport) -> None:
    """效益不能憑空生出來：``kpi_fields`` 必須是 EpisodeKPI 真的有的欄位。"""
    valid = set(EpisodeKPI().to_dict())
    benefit = scenario_benefit(report, EQUIPMENT, BASE, annual_events=1.0)
    seen = set()
    for line in benefit.lines:
        assert line.kpi_fields, f"{line.key} 沒有標出它追溯的 KPI 欄位"
        for field in line.kpi_fields:
            assert field in valid, f"{line.key} 引用了不存在的 KPI 欄位 {field}"
        seen.add(line.key)
    assert seen == set(BENEFIT_ORDER)


# ======================================================================================
# 5. 年度事件數配置
# ======================================================================================
def test_年度事件數不會重複計算(report: BenchmarkReport) -> None:
    events = annual_events_by_scenario(report)
    equipment_pool = A.ANNUAL_MAINTENANCE_EVENTS_PER_LINE * A.PRODUCTION_IMPACTING_EVENT_SHARE
    # 設備情境的事件數總和不得超過設備事件池；複合情境是從池子裡切出去，不是加上去。
    assert events[EQUIPMENT] <= equipment_pool + 1e-9
    assert events[SAFETY] == pytest.approx(A.ANNUAL_HAZARD_INTRUSION_EVENTS_PER_LINE)


def test_事件數隨頻率倍率等比縮放(report: BenchmarkReport) -> None:
    base = annual_events_by_scenario(report, 1.0)
    doubled = annual_events_by_scenario(report, 2.0)
    for key, value in base.items():
        assert doubled[key] == pytest.approx(value * 2.0)


def test_工安壓力測試會被縮回典型事件長度(report: BenchmarkReport) -> None:
    """hazard-zone 注入的是全程滯留，直接年化會同時高估產能代價與工安效益。"""
    benefit = scenario_benefit(report, SAFETY, BASE, annual_events=1.0)
    span = 80.0 - 1.0
    assert benefit.duration_scale == pytest.approx(A.TYPICAL_INTRUSION_MIN / span)
    assert benefit.duration_scale < 1.0
    # 設備情境不縮放。
    assert scenario_benefit(report, EQUIPMENT, BASE, 1.0).duration_scale == pytest.approx(1.0)


def test_工安情境不會被算成維修工時節省(report: BenchmarkReport) -> None:
    lines = {ln.key: ln for ln in scenario_benefit(report, SAFETY, BASE, 1.0).lines}
    assert lines["maintenance_labour_saved"].per_event_ntd == pytest.approx(0.0)
    assert lines["safety_expected_loss_avoided"].per_event_ntd > 0


# ======================================================================================
# 6. 破口分析
# ======================================================================================
def test_破口分析的臨界值真的讓淨效益歸零(report: BenchmarkReport) -> None:
    scope = plant_scope(6)
    points = {p.key: p for p in breakeven(report, BASE, scope)}
    for field in ("escalation_rate", "realization", "frequency_multiplier", "cost_multiplier"):
        value = points[field].breakeven_value
        assert value is not None, f"{field} 應該在合理區間內找得到臨界值"
        bc = build_business_case(report, BASE.replace(**{field: value}), scope)
        assert bc.steady_net_ntd == pytest.approx(0.0, abs=max(1.0, abs(bc.annual_benefit_ntd) * 1e-3))


def test_破口分析會給出回本所需的最少產線數(report: BenchmarkReport) -> None:
    points = {p.key: p for p in breakeven(report, BASE, plant_scope(6))}
    min_lines = points["min_lines"].breakeven_value
    assert min_lines is not None and min_lines >= 1
    below = build_business_case(report, BASE, plant_scope(max(1, int(min_lines) - 1)))
    at = build_business_case(report, BASE, plant_scope(int(min_lines)))
    assert at.steady_net_ntd > 0
    if int(min_lines) > 1:
        assert below.steady_net_ntd <= 0


def test_工安取捨算式可重現且不美化假設(report: BenchmarkReport) -> None:
    """工安是硬限制，不是加權項 —— 所以這一段必須誠實呈現它在財務上是負的。"""
    data = safety_tradeoff(report)
    assert data is not None
    exposure = 80.0 - 1.0
    cost = 11_000.0 - 500.0                       # Guardian 比 Baseline A 多損失的邊際貢獻
    assert data["exposure_avoided_min"] == pytest.approx(exposure)
    assert data["production_cost_ntd"] == pytest.approx(cost)
    assert data["ntd_per_exposure_minute"] == pytest.approx(cost / exposure, rel=1e-3)

    hours = exposure / 60.0
    expected = hours * A.HAZARD_INJURY_PROBABILITY_PER_EXPOSURE_HOUR * A.SAFETY_INCIDENT_COST_NTD
    assert data["expected_loss_avoided_ntd"] == pytest.approx(expected, abs=1.0)
    assert data["net_ntd"] < 0, "在假設的致傷機率下這一項就是負的，不該被算成正效益"

    # 臨界值代回去必須讓期望損失剛好等於放棄的產能。
    prob = data["breakeven_injury_probability_per_hour"]
    assert hours * prob * A.SAFETY_INCIDENT_COST_NTD == pytest.approx(cost, rel=1e-3)
    incident_cost = data["breakeven_incident_cost_ntd"]
    assert hours * A.HAZARD_INJURY_PROBABILITY_PER_EXPOSURE_HOUR * incident_cost == pytest.approx(
        cost, rel=1e-3
    )
    assert prob > A.HAZARD_INJURY_PROBABILITY_PER_EXPOSURE_HOUR


def test_現況假設敏感度由弱到強單調(report: BenchmarkReport) -> None:
    data = build_business_report(report, scope=plant_scope(6))
    benefits = [row["annual_benefit_ntd"] for row in data["incumbent_sensitivity"]]
    assert benefits == sorted(benefits)


# ======================================================================================
# 7. 成本結構與定價
# ======================================================================================
def test_成本隨規模成長且級距正確() -> None:
    small = build_solution_cost(plant_scope(1))
    medium = build_solution_cost(plant_scope(4))
    large = build_solution_cost(plant_scope(9))
    assert small.monthly_ntd < medium.monthly_ntd < large.monthly_ntd
    assert small.one_time_ntd < medium.one_time_ntd < large.one_time_ntd
    assert DEFAULT_PRICES.integration_fee(1)[0] == DEFAULT_PRICES.integration_small_ntd
    assert DEFAULT_PRICES.integration_fee(4)[0] == DEFAULT_PRICES.integration_medium_ntd
    assert DEFAULT_PRICES.integration_fee(9)[0] == DEFAULT_PRICES.integration_large_ntd


def test_成本涵蓋提案_12_1_的四種收費方式與中華電信網路() -> None:
    streams = {item.stream for item in build_solution_cost(plant_scope(3)).items}
    assert streams == {
        "platform_saas", "ai_usage", "system_integration", "managed_service", "cht_network",
    }


def test_成本倍率同時作用於一次性與經常性費用() -> None:
    base = build_solution_cost(plant_scope(3))
    scaled = build_solution_cost(plant_scope(3), cost_multiplier=2.0)
    assert scaled.one_time_ntd == pytest.approx(base.one_time_ntd * 2)
    assert scaled.annual_recurring_ntd == pytest.approx(base.annual_recurring_ntd * 2)


# ======================================================================================
# 8. 競品比較
# ======================================================================================
def test_競品比較涵蓋研究文件_4_5_點名的三件作品() -> None:
    data = competitor_report()
    keys = {c["key"] for c in data["competitors"]}
    assert keys == {"smart-tool-holder", "digital-twin-inspection", "partial-discharge"}
    for item in data["competitors"]:
        assert item["overlap"].strip(), "沒有寫重疊等於沒有正面回答評審的疑慮"
        assert item["avoidance"].strip()
        assert item["unknown"].strip(), "公開資料未揭露的部分必須標示，不能推測"
        assert item["source"].strip()
    # 每一條比較軸的「我們」都要指得到 repo 裡的位置，不能只是形容詞。
    for axis in data["axes"]:
        assert axis["evidence"].strip()


# ======================================================================================
# 9. 報表結構與 CLI
# ======================================================================================
def test_報表結構完整(report: BenchmarkReport) -> None:
    data = build_business_report(report, scope=plant_scope(6))
    assert set(data) >= {
        "meta", "assumptions", "cases", "entry_deployment",
        "breakeven", "safety_tradeoff", "incumbent_sensitivity", "competitors",
    }
    assert [c["case"]["key"] for c in data["cases"]] == ["conservative", "base", "optimistic"]
    assert data["meta"]["not_counted"], "刻意不計入的項目必須寫出來"
    assert "報廢" in data["meta"]["not_counted"], "未量測報廢率這件事必須誠實揭露"
    assert data["meta"]["disclaimer"]


def test_benchmark_報表可以序列化後重建(report: BenchmarkReport) -> None:
    restored = BenchmarkReport.from_dict(json.loads(json.dumps(report.to_dict(), default=str)))
    assert restored.scenarios() == report.scenarios()
    before = build_business_case(report, BASE, plant_scope(3)).annual_benefit_ntd
    after = build_business_case(restored, BASE, plant_scope(3)).annual_benefit_ntd
    assert after == pytest.approx(before)


def test_cli_可以由既有_benchmark_json_產出商業案例(report: BenchmarkReport, tmp_path) -> None:
    path = tmp_path / "bench.json"
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, default=str), encoding="utf-8")
    out = tmp_path / "business.json"
    assert main(["business-case", "--from-json", str(path), "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert audit_figures(data) == []
    assert data["cases"][1]["case"]["key"] == "base"


def test_缺少對照組時明確報錯而不是默默算出數字() -> None:
    partial = BenchmarkReport(
        rows=[_row(EQUIPMENT, "guardian", {"M-A": "bearing_degradation"}, production_loss_ntd=100.0)]
    )
    with pytest.raises(ValueError, match="缺少模式"):
        build_business_case(partial, BASE, plant_scope(1))


def test_no_fault_disturbance_scenarios_carry_zero_annual_events():
    """fp-* 干擾情境是量誤報率的壓力測試，不是事件：年度次數必須為 0，
    否則它們會分走工安事件池、並把「沒白停機」的差額年化成效益。"""
    from factory_guardian.benchmark import BenchmarkReport, run_benchmark  # noqa: F401
    import json, pathlib
    report = BenchmarkReport.from_dict(json.loads(pathlib.Path("benchmark.json").read_text()))
    events = annual_events_by_scenario(report)
    no_fault = [sid for sid in report.scenarios()
                if not next((r.ground_truth for r in report.rows if r.scenario_id == sid), {})]
    assert no_fault, "benchmark.json 應含至少一個無故障干擾情境"
    for sid in no_fault:
        assert events[sid] == 0.0, f"{sid} 不該被算成事件"
    # 工安事件池不可被稀釋：純工安情境仍拿到完整的每年 12 次
    hazard = [sid for sid in report.scenarios()
              if (gt := next((r.ground_truth for r in report.rows if r.scenario_id == sid), {}))
              and all(k == "SAFETY" for k in gt)]
    assert hazard and abs(sum(events[s] for s in hazard) - 12.0) < 1e-6
