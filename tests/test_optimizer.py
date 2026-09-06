"""排名引擎與權重穩健性掃描。

這一份要回答的是評審一定會問的那句話：「權重是你自己訂的，改一改推薦是不是就換人了？」
所以判準全部圍繞三件事 —— 掃描本身可重現、掃描不會動到排名結果、
以及「推薦很穩」和「推薦很脆弱」兩種情況都要被如實報出來。
"""

from __future__ import annotations

import pytest

from factory_guardian.domain import (
    Action,
    ActionKind,
    PlanProjection,
    RecoveryPlan,
    SafetyFinding,
    SafetyReview,
    SafetyVerdictKind,
)
from factory_guardian.optimizer import (
    DEFAULT_WEIGHTS,
    explain_robustness,
    normalize_weights,
    rank_plans,
    robustness_scan,
    score_plan,
)


def _plan(
    plan_id: str,
    production: float,
    delay: float,
    recovery: float,
    cost: float,
    residual: float,
    verdict: SafetyVerdictKind = SafetyVerdictKind.PASS,
) -> RecoveryPlan:
    projection = PlanProjection(
        production_pct=production,
        production_pct_final=production,
        production_loss_units=0.0,
        max_order_delay_min=delay,
        recovery_min=recovery,
        cost_ntd=cost,
        residual_risk=residual,
        machine_health_after=100.0 - 100.0 * residual,
    )
    plan = RecoveryPlan(
        plan_id=plan_id, title=plan_id, summary=plan_id,
        actions=[Action(ActionKind.RAISE_ALERT, "M-A")],
        projection=projection,
    )
    findings = (
        [SafetyFinding("SR-02P", SafetyVerdictKind.BLOCK, "測試用阻擋")]
        if verdict is SafetyVerdictKind.BLOCK else []
    )
    plan.safety = SafetyReview(plan_id=plan_id, verdict=verdict, findings=findings)
    return plan


def _clear_winner() -> list[RecoveryPlan]:
    """一個各項都明顯較好的方案 + 兩個較差的。"""
    return [
        _plan("PLAN-D", production=85.0, delay=0.0, recovery=10.0, cost=15_000.0, residual=0.0),
        _plan("PLAN-C", production=45.0, delay=30.0, recovery=40.0, cost=45_000.0, residual=0.1),
        _plan("PLAN-A", production=95.0, delay=5.0, recovery=5.0, cost=5_000.0, residual=0.9,
              verdict=SafetyVerdictKind.BLOCK),
    ]


def _near_tie() -> list[RecoveryPlan]:
    """兩個分數幾乎一樣的方案：一個產出高、一個交期好，權重動一點就會換人。"""
    return [
        _plan("PLAN-X", production=80.0, delay=24.0, recovery=20.0, cost=20_000.0, residual=0.05),
        _plan("PLAN-Y", production=72.0, delay=6.0, recovery=22.0, cost=21_000.0, residual=0.05),
    ]


# --------------------------------------------------------------------------------- 基本
def test_score_plan_matches_rank_plans():
    """參數搜尋比較變體用的評分，必須和正式排名是同一把尺。"""
    plans = _clear_winner()
    scores = {plan.plan_id: score_plan(plan)[0] for plan in plans}
    rank_plans(plans)
    for plan in plans:
        if plan.feasible:
            assert plan.score == pytest.approx(scores[plan.plan_id])


def test_ranking_result_carries_robustness():
    result = rank_plans(_clear_winner())
    payload = result.to_dict()
    assert payload["robustness"] is not None
    assert payload["robustness"]["baseline_plan_id"] == payload["recommended_plan_id"]


# --------------------------------------------------------------------------- 穩健性掃描
def test_robustness_scan_is_reproducible():
    """固定種子：同一批方案跑兩次，掃描結果必須逐欄位相同。"""
    first = robustness_scan(_clear_winner())
    second = robustness_scan(_clear_winner())
    assert first == second


def test_robustness_scan_does_not_touch_the_ranking():
    """掃描是事後檢查，不准改動任何方案的分數、名次或可行性。"""
    plans = _clear_winner()
    result = rank_plans(plans, robustness=False)
    before = [(p.plan_id, p.rank, p.score, p.feasible) for p in result.ranked]
    robustness_scan(plans)
    after = [(p.plan_id, p.rank, p.score, p.feasible) for p in result.ranked]
    assert before == after


def test_a_clear_winner_survives_every_perturbation():
    scan = robustness_scan(_clear_winner(), perturbation=0.20)
    assert scan["baseline_plan_id"] == "PLAN-D"
    assert scan["stable_fraction"] == 1.0
    assert scan["flips"] == []
    assert scan["scenarios"] == 12 + scan["samples"]     # 六個準則各 ±20% 的網格 + 抽樣
    assert scan["baseline_top_gap"] > 0
    assert scan["min_top_gap"] <= scan["baseline_top_gap"]
    # 被 Safety BLOCK 的方案不能出現在可行集合裡
    assert scan["feasible_plans"] == 2


def test_a_near_tie_is_reported_as_fragile_not_as_stable():
    """兩個方案幾乎同分時，系統必須說實話：這個推薦是脆弱的。"""
    scan = robustness_scan(_near_tie(), perturbation=0.20)
    assert 0.0 < scan["stable_fraction"] < 1.0
    assert scan["flips"], "±20% 網格內應該至少有一個準則會翻轉推薦"
    assert scan["min_top_gap"] < scan["baseline_top_gap"]
    sensitive = scan["most_sensitive_criterion"]
    assert sensitive in DEFAULT_WEIGHTS
    assert scan["criteria"][sensitive]["within_perturbation"] is True


def test_critical_weight_actually_flips_the_recommendation():
    """臨界權重不是裝飾：把權重推過它，推薦真的要換人。"""
    plans = _near_tie()
    scan = robustness_scan(plans)
    criterion = scan["most_sensitive_criterion"]
    critical = scan["criteria"][criterion]["critical_weight"]
    baseline = scan["baseline_plan_id"]

    def winner_at(weight_of_criterion: float) -> str:
        weights = dict(normalize_weights(DEFAULT_WEIGHTS))
        rest = 1.0 - weights[criterion]
        scale = (1.0 - weight_of_criterion) / rest if rest > 0 else 1.0
        weights = {c: (weight_of_criterion if c == criterion else w * scale) for c, w in weights.items()}
        return str(robustness_scan(plans, weights=weights, samples=0)["baseline_plan_id"])

    direction = scan["criteria"][criterion]["direction"]
    nudge = 0.02 if direction == "up" else -0.02
    assert winner_at(critical - nudge) == baseline, "臨界值的這一側推薦不該變"
    assert winner_at(critical + nudge) != baseline, "跨過臨界值推薦必須換人"


def test_a_single_feasible_plan_is_reported_as_no_choice_not_as_robust():
    """只剩一個可行方案時，100% 穩定是「沒得選」，不是「比較出來的穩健」。"""
    plans = [
        _plan("PLAN-C", production=45.0, delay=30.0, recovery=40.0, cost=45_000.0, residual=0.1),
        _plan("PLAN-A", production=95.0, delay=0.0, recovery=0.0, cost=1_000.0, residual=0.9,
              verdict=SafetyVerdictKind.BLOCK),
    ]
    scan = robustness_scan(plans)
    assert scan["feasible_plans"] == 1
    assert scan["stable_fraction"] == 1.0
    assert scan["scenarios"] == 0
    assert "只有一個可行方案" in str(scan["note"])


def test_no_feasible_plan_reports_nothing_instead_of_pretending():
    scan = robustness_scan([
        _plan("PLAN-A", production=95.0, delay=0.0, recovery=0.0, cost=1_000.0, residual=0.9,
              verdict=SafetyVerdictKind.BLOCK),
    ])
    assert scan["baseline_plan_id"] is None
    assert scan["stable_fraction"] is None


def test_explain_robustness_writes_the_dashboard_line():
    scan = robustness_scan(_clear_winner())
    line = explain_robustness(scan)
    assert "±20% 權重擾動下推薦不變比例 100%" in line
    assert "最敏感準則" in line
    assert explain_robustness(None) == "未執行權重穩健性掃描。"
