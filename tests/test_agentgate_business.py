"""AgentGate 商業案例的守門測試(規格 §8)。

這裡守三件事,對應 `docs/agentgate/agentgate_business_case.md` 的三個承諾:

1. **數字可重現** —— 同樣的 seed 與視窗必然得到同樣的實測比例、同樣的 ROI。
   一份每次跑都不一樣的商業案例,在決賽問答時無法被驗證。
2. **假設全部具名** —— 報表裡不能出現沒有 ``basis`` 的數字,
   也不能出現「業界平均」這種沒有來源的說法。
3. **破口值可重現** —— 臨界值不是講講而已,它是解出來的,而且解得出同一個答案。
"""

from __future__ import annotations

import json

import pytest

from agentgate.business import (
    BASE,
    CONSERVATIVE,
    COVERAGE_MODES,
    OPTIMISTIC,
    audit_figures,
    breakeven,
    build_business_case,
    build_business_report,
    coverage_for,
    default_agent_seats,
    measure_harm_prevention,
    measure_ops,
    run_business_case,
    summarize_markdown,
)
from agentgate.business.assumptions import BASIS_LABEL, Basis, static_assumptions
from agentgate.business.model import (
    annual_actions,
    build_business_case_with_coverage,
    harm_expected_annual_ntd,
    measured_assumptions,
)

SEEDS = (20260817, 20260818, 20260819)


@pytest.fixture(scope="module")
def ops():
    return measure_ops(SEEDS, window_minutes=45)


@pytest.fixture(scope="module")
def harm():
    return measure_harm_prevention()


@pytest.fixture(scope="module")
def report():
    return build_business_report(seeds=SEEDS, window_minutes=45)


# ======================================================================================
# 一、數字可重現
# ======================================================================================
class TestReproducible:
    def test_同樣的_seed_必然量到同樣的比例(self):
        a = measure_ops(SEEDS, window_minutes=45)
        b = measure_ops(SEEDS, window_minutes=45)
        assert a.to_dict() == b.to_dict()

    def test_量測是真的跑過管線而不是查表(self, ops):
        # 每個 seed 都要真的產生數百件動作,而且三種裁決狀態的比例加起來 = 1。
        assert len(ops.samples) == len(SEEDS)
        for sample in ops.samples:
            assert sample.actions > 100
            total = sample.auto_pass + sample.approval + sample.blocked
            assert total == pytest.approx(1.0, abs=1e-9)

    def test_高風險比例與需覆核比例都在合理範圍且有順序關係(self, ops):
        assert 0.0 < ops.high_risk_share < ops.review_needed_share < 1.0
        assert 0.0 < ops.approval_rate < ops.auto_pass_rate < 1.0

    def test_整份報表可重現(self):
        a = build_business_report(seeds=SEEDS, window_minutes=45)
        b = build_business_report(seeds=SEEDS, window_minutes=45)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_年度動作總數是推導不是實測(self, ops):
        # 公式:每分鐘動作數 × 60 × 每日受治理時數 × 天數。改任一項就必須跟著變。
        from agentgate.business import assumptions as A

        expected = (ops.actions_per_minute * 60.0 * A.governed_hours_per_day()
                    * A.OPERATING_DAYS_PER_YEAR)
        assert annual_actions(ops) == pytest.approx(expected)
        assert annual_actions(ops, 0.5) == pytest.approx(expected * 0.5)

    def test_席位數直接數_console_不是寫死(self):
        from agentgate.console import SEATS

        assert default_agent_seats() == sum(len(v) for v in SEATS.values())


# ======================================================================================
# 二、假設全部具名
# ======================================================================================
class TestAssumptionsAreNamed:
    def test_報表裡沒有未標示來源的數字(self, report):
        assert audit_figures(report) == []

    def test_每個具名參數都有依據與推估過程(self, ops, harm):
        items = measured_assumptions(ops, harm) + static_assumptions()
        assert len(items) >= 20
        keys = [a.key for a in items]
        assert len(keys) == len(set(keys)), "假設參數的 key 不可重複"
        for item in items:
            assert item.key and item.label and item.unit
            assert item.basis in (Basis.MEASURED, Basis.DERIVED, Basis.ASSUMPTION)
            assert item.rationale.strip(), f"{item.key} 沒有寫推估過程"
            assert item.source.strip(), f"{item.key} 沒有寫來源"
            assert BASIS_LABEL[item.basis.value] in ("實測", "推導", "假設")

    def test_三個關鍵比例必須標為實測(self, ops, harm):
        by_key = {a.key: a for a in measured_assumptions(ops, harm)}
        for key in ("auto_pass_rate", "approval_rate", "high_risk_share",
                    "review_needed_share", "har_ungoverned", "har_governed"):
            assert by_key[key].basis is Basis.MEASURED, f"{key} 必須是實測"
        assert by_key["annual_actions"].basis is Basis.DERIVED

    def test_誤動作損失三項都必須標為假設(self, ops, harm):
        by_key = {a.key: a for a in static_assumptions()}
        for key in ("pii_breach_cost", "wrong_payment_cost", "sim_swap_cost",
                    "annual_pii_breach_events", "annual_wrong_payment_events",
                    "annual_sim_swap_events", "reviewer_hourly_cost",
                    "manual_review_minutes", "evidence_review_minutes"):
            assert by_key[key].basis is Basis.ASSUMPTION, f"{key} 必須誠實標為假設"

    def test_個資法罰則的公開性與適用性被分開講(self):
        by_key = {a.key: a for a in static_assumptions()}
        pii = by_key["pii_breach_cost"]
        assert "第 48 條" in pii.source
        assert "公開值" in pii.rationale and "假設" in pii.rationale

    def test_沒有任何一個數字被包裝成業界平均(self, report):
        blob = json.dumps(report, ensure_ascii=False)
        for banned in ("業界平均", "產業平均", "業界標準", "industry average"):
            assert banned not in blob, f"報表出現了沒有來源的說法:{banned}"

    def test_定價全部標為假設並附推估過程(self, report):
        assert report["pricing"]["prices"]["basis"] == Basis.ASSUMPTION.value
        tiers = report["pricing"]["tiers"]
        assert len(tiers) >= 7
        for tier in tiers:
            assert tier["rationale"].strip()

    def test_誠實邊界寫在報表裡(self, report):
        disclaimer = report["meta"]["disclaimer"]
        assert "SYNTHETIC DEMO DATA" in disclaimer
        assert "不宣稱與中華電信有任何既有合作關係" in disclaimer
        assert report["meta"]["not_counted"].strip()
        assert "caveat" in report["measurement"]


# ======================================================================================
# 三、ROI 公式本身
# ======================================================================================
class TestRoiModel:
    def test_年度效益等於三條效益線的和(self, ops, harm):
        case = build_business_case(ops, harm, BASE)
        assert case.annual_benefit_ntd == pytest.approx(
            sum(line.annual_ntd for line in case.lines))
        assert {line.key for line in case.lines} == {
            "review_avoided", "evidence_speedup", "harm_avoided"}

    def test_淨額等於效益減成本(self, ops, harm):
        case = build_business_case(ops, harm, BASE)
        assert case.steady_net_ntd == pytest.approx(
            case.annual_benefit_ntd - case.cost.annual_recurring_ntd)
        assert case.year_one_net_ntd == pytest.approx(
            case.annual_benefit_ntd - case.cost.year_one_ntd)

    def test_免覆核節省用的是實測的送核准比例(self, ops, harm):
        from agentgate.business import assumptions as A

        case = build_business_case(ops, harm, BASE, "tiered")
        line = next(l for l in case.lines if l.key == "review_avoided")
        expected = (case.annual_actions
                    * (ops.review_needed_share - ops.approval_rate)
                    * BASE.manual_review_minutes
                    * A.REVIEWER_HOURLY_COST_NTD / 60.0
                    * BASE.realization)
        assert line.annual_ntd == pytest.approx(expected)

    def test_誤動作損失那一項乘的是實測的_HAR_差(self, ops, harm):
        case = build_business_case(ops, harm, BASE)
        line = next(l for l in case.lines if l.key == "harm_avoided")
        expected = (harm_expected_annual_ntd(BASE.harm_multiplier)
                    * harm.prevented_share * BASE.realization)
        assert line.annual_ntd == pytest.approx(expected)
        assert harm.prevented_share == pytest.approx(
            harm.har_ungoverned - harm.har_governed)

    def test_三情境的順序是保守小於基準小於樂觀(self, report):
        benefits = [c["result"]["annual_benefit_ntd"] for c in report["cases"]]
        assert benefits == sorted(benefits)
        assert report["cases"][0]["case"]["key"] == "conservative"
        assert report["cases"][1]["case"]["key"] == "base"
        assert report["cases"][2]["case"]["key"] == "optimistic"

    def test_保守情境不成立而且我們不藏(self, ops, harm):
        conservative = build_business_case(ops, harm, CONSERVATIVE)
        assert conservative.steady_roi_pct is not None
        assert conservative.steady_roi_pct < 0, "保守情境如果會賺,它就不是保守情境"
        assert conservative.payback_months is None

    def test_基準情境成立且回收期算得出來(self, ops, harm):
        base = build_business_case(ops, harm, BASE)
        assert base.steady_roi_pct is not None and base.steady_roi_pct > 0
        assert base.payback_months is not None and base.payback_months > 0
        assert base.npv_ntd() > 0

    def test_樂觀情境不是靠改實測值換來的(self, ops, harm):
        # 三個情境用的是同一份實測量測,只有具名的情境參數不同。
        for case in (CONSERVATIVE, BASE, OPTIMISTIC):
            built = build_business_case(ops, harm, case)
            assert built.ops is ops
            assert built.harm is harm

    def test_人力等效必須算得出來而且被報出來(self, ops, harm):
        base = build_business_case(ops, harm, BASE)
        # 現況覆核覆蓋率換算成人力,一定要遠大於導入後 —— 這是整個價值主張。
        assert base.incumbent_review_fte > base.governed_review_fte * 5


# ======================================================================================
# 四、現況假設(最敏感的一根軸)
# ======================================================================================
class TestIncumbentAxis:
    def test_三種現況假設的覆核覆蓋率有明確順序(self, ops):
        gate_only = coverage_for("gate_only", ops)
        tiered = coverage_for("tiered", ops)
        full = coverage_for("full", ops)
        assert gate_only == pytest.approx(ops.approval_rate)
        assert tiered == pytest.approx(ops.review_needed_share)
        assert full == 1.0
        assert gate_only < tiered < full

    def test_基準情境刻意不採用規格字面的百分之百覆核(self, report):
        rows = {r["mode"]: r for r in report["incumbent_sensitivity"]}
        assert set(rows) == set(COVERAGE_MODES)
        base_mode = report["cases"][1]["incumbent"]["coverage_mode"]
        assert base_mode == "tiered"
        assert rows["full"]["annual_benefit_ntd"] > rows["tiered"]["annual_benefit_ntd"]
        assert rows["tiered"]["annual_benefit_ntd"] > rows["gate_only"]["annual_benefit_ntd"]

    def test_未知的現況模式要炸掉而不是默默回一個值(self, ops):
        with pytest.raises(ValueError):
            coverage_for("whatever", ops)


# ======================================================================================
# 五、破口分析
# ======================================================================================
class TestBreakeven:
    def test_破口值可重現(self, ops, harm):
        a = [p.to_dict() for p in breakeven(ops, harm, BASE)]
        b = [p.to_dict() for p in breakeven(ops, harm, BASE)]
        assert a == b

    def test_每個破口都給了臨界值或明講求不出來(self, report):
        keys = {p["key"] for p in report["breakeven"]}
        assert {"volume_multiplier", "manual_review_minutes", "incumbent_coverage",
                "approval_rate", "realization", "harm_multiplier",
                "cost_multiplier"} <= keys
        for point in report["breakeven"]:
            assert point["verdict"].strip()

    def test_覆核工時的臨界值真的是臨界值(self, ops, harm):
        point = next(p for p in breakeven(ops, harm, BASE)
                     if p.key == "manual_review_minutes")
        assert point.breakeven_value is not None
        below = build_business_case(
            ops, harm, BASE.replace(manual_review_minutes=point.breakeven_value * 0.9))
        above = build_business_case(
            ops, harm, BASE.replace(manual_review_minutes=point.breakeven_value * 1.1))
        assert below.steady_net_ntd < 0 < above.steady_net_ntd

    def test_動作量的臨界值真的是臨界值(self, ops, harm):
        point = next(p for p in breakeven(ops, harm, BASE)
                     if p.key == "volume_multiplier")
        assert point.breakeven_value is not None
        below = build_business_case(
            ops, harm, BASE.replace(volume_multiplier=point.breakeven_value * 0.8))
        above = build_business_case(
            ops, harm, BASE.replace(volume_multiplier=point.breakeven_value * 1.2))
        assert below.steady_net_ntd < 0 < above.steady_net_ntd

    def test_現況覆核覆蓋率的臨界值真的是臨界值(self, ops, harm):
        point = next(p for p in breakeven(ops, harm, BASE)
                     if p.key == "incumbent_coverage")
        assert point.breakeven_value is not None
        below = build_business_case_with_coverage(
            ops, harm, BASE, max(0.0, point.breakeven_value - 0.02))
        above = build_business_case_with_coverage(
            ops, harm, BASE, min(1.0, point.breakeven_value + 0.02))
        assert below.steady_net_ntd < 0 < above.steady_net_ntd

    def test_破口對應的年度動作量有被換算成人看得懂的數字(self, report):
        point = report["breakeven_actions_per_year"]
        assert point["value"] is not None and point["value"] > 0
        assert point["per_minute"] is not None and point["per_minute"] > 0

    def test_誤動作損失歸零時結論不能翻掉而且要主動說(self, ops, harm):
        # 這是我們最不可靠的假設。如果它一歸零 ROI 就垮,整份商業案例就是靠它撐的。
        zero_harm = build_business_case(ops, harm, BASE.replace(harm_multiplier=0.0))
        assert zero_harm.steady_net_ntd > 0
        point = next(p for p in breakeven(ops, harm, BASE) if p.key == "harm_multiplier")
        assert "歸零" in point.verdict


# ======================================================================================
# 六、拿掉未驗證假設之後(robustness)
# ======================================================================================
class TestRobustness:
    def test_拿掉證據包加速那一條之後結論仍成立(self, report):
        robust = report["robustness_no_evidence_speedup"]
        assert robust["steady_roi_pct"] is not None
        assert robust["steady_roi_pct"] > 0
        # 拿掉一條效益,ROI 必須明顯下降 —— 否則就代表那一條根本沒被算進去。
        assert robust["steady_roi_pct"] < report["cases"][1]["result"]["steady_roi_pct"]

    def test_拿掉之後送核准比例才有上限而且高於實測值(self, ops, report):
        points = {p["key"]: p for p in
                  report["robustness_no_evidence_speedup"]["breakeven"]}
        cap = points["approval_rate"]["breakeven_value"]
        assert cap is not None
        assert ops.approval_rate < cap < 1.0

    def test_停用之後那一條的金額是零(self, ops, harm):
        case = build_business_case(ops, harm, BASE, include_evidence_speedup=False)
        line = next(l for l in case.lines if l.key == "evidence_speedup")
        assert line.annual_ntd == 0.0


# ======================================================================================
# 七、中華電信可辨識收入與 CLI
# ======================================================================================
class TestChtAndCli:
    def test_中華電信收入單獨列且等於兩項之和(self, report):
        cht = report["cht_revenue"]
        assert cht["total_annual_ntd"] == pytest.approx(
            cht["hicloud_annual_ntd"] + cht["identity_verification_annual_ntd"])
        assert cht["identity_verification_calls"] > 0

    def test_身分驗證呼叫量等於送核准件數(self, ops, harm, report):
        base = build_business_case(ops, harm, BASE)
        expected = base.annual_actions * ops.approval_rate
        assert report["cht_revenue"]["identity_verification_calls"] == pytest.approx(
            round(expected, 0), rel=1e-6)

    def test_run_business_case_可以寫出_json(self, tmp_path):
        out = tmp_path / "business.json"
        report = run_business_case(out=out, seeds=SEEDS, window_minutes=45, quiet=True)
        assert out.exists()
        with out.open(encoding="utf-8") as fh:
            written = json.load(fh)
        assert written["meta"]["title"] == report["meta"]["title"]
        assert audit_figures(written) == []

    def test_摘要印得出三情境與破口(self, report):
        text = summarize_markdown(report)
        assert "三情境" in text and "破口分析" in text
        assert "中華電信可辨識收入" in text
        assert "SYNTHETIC DEMO DATA" in text
