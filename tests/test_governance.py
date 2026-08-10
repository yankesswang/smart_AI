"""治理層：Policy 權限、Safety 硬規則、方案排名。"""

from __future__ import annotations

from factory_guardian.agents.safety import SafetyAgent
from factory_guardian.domain import (
    Action,
    ActionKind,
    PlanProjection,
    RecoveryPlan,
    SafetyFinding,
    SafetyReview,
    SafetyVerdictKind,
)
from factory_guardian.optimizer import DEFAULT_WEIGHTS, extract_criteria, rank_plans
from factory_guardian.policy.engine import ACTION_POLICY, PolicyEngine, SafetyContext
from factory_guardian.twin.engine import FactoryTwin
from factory_guardian.twin.scenarios import get_scenario


def _projection(**kw) -> PlanProjection:
    base = dict(
        production_pct=80.0, production_pct_final=80.0, production_loss_units=10.0,
        max_order_delay_min=0.0, recovery_min=10.0, cost_ntd=10_000.0,
        residual_risk=0.1, machine_health_after=90.0, peak_vibration=3.0,
        peak_temperature=65.0, min_health=85.0, recovered=True, hazard_while_running=False,
    )
    base.update(kw)
    return PlanProjection(**base)


def _plan(plan_id: str, verdict: SafetyVerdictKind = SafetyVerdictKind.PASS, **proj) -> RecoveryPlan:
    plan = RecoveryPlan(
        plan_id=plan_id, title=plan_id, summary="",
        actions=[Action(ActionKind.RAISE_ALERT, "M-A")],
        projection=_projection(**proj),
    )
    plan.safety = SafetyReview(plan_id, verdict, [SafetyFinding("R", verdict, "test")] if verdict is not SafetyVerdictKind.PASS else [])
    return plan


# --------------------------------------------------------------------------- 動作權限
def test_action_policy_matches_the_spec_table():
    assert not ACTION_POLICY[ActionKind.RAISE_ALERT].requires_approval
    assert not ACTION_POLICY[ActionKind.CREATE_WORK_ORDER].requires_approval
    assert not ACTION_POLICY[ActionKind.UPDATE_SCHEDULE].requires_approval
    assert ACTION_POLICY[ActionKind.STOP_MACHINE].requires_approval
    assert ACTION_POLICY[ActionKind.START_MAINTENANCE].requires_approval
    assert ACTION_POLICY[ActionKind.SAFETY_OVERRIDE].forbidden


def test_low_risk_actions_run_without_approval():
    decision = PolicyEngine(require_approval=True).evaluate_actions(
        [ActionKind.RAISE_ALERT, ActionKind.CREATE_WORK_ORDER]
    )
    assert decision.allowed and not decision.requires_approval and decision.risk == "low"


def test_stopping_a_machine_needs_a_human():
    decision = PolicyEngine(require_approval=True).evaluate_actions([ActionKind.STOP_MACHINE])
    assert decision.allowed and decision.requires_approval and decision.risk == "high"


def test_safety_override_is_never_allowed():
    for require in (True, False):
        decision = PolicyEngine(require_approval=require).evaluate_actions([ActionKind.SAFETY_OVERRIDE])
        assert not decision.allowed


# --------------------------------------------------------------------------- Safety 規則
def _ctx(twin: FactoryTwin, *, keeps_running: bool, full_speed: bool, projection=None, kinds=None) -> SafetyContext:
    return SafetyContext(
        snapshot=twin.snapshot(), machine_id="M-A", action_kinds=kinds or set(),
        keeps_machine_running=keeps_running, keeps_full_speed=full_speed,
        plan_id="PLAN-X", projection=projection,
    )


def test_person_in_hazard_zone_blocks_any_running_plan():
    twin = FactoryTwin(seed=3)
    twin.schedule(get_scenario("hazard-zone").injections)
    twin.run(6)
    engine = PolicyEngine()
    running = engine.evaluate_safety(_ctx(twin, keeps_running=True, full_speed=True))
    assert engine.combine(running) is SafetyVerdictKind.BLOCK
    assert any(f.rule_id == "SR-01" for f in running)
    # 停機的方案就不會被這條規則擋
    stopped = engine.evaluate_safety(_ctx(twin, keeps_running=False, full_speed=False))
    assert not any(f.rule_id == "SR-01" for f in stopped)


def test_projection_blocks_a_plan_before_the_machine_is_already_dangerous():
    """振動現在還在警告區，但模擬顯示這個方案會把它推進危險區 → 先擋下來。"""
    twin = FactoryTwin(seed=3)
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(8)
    assert twin.snapshot().machines["M-A"].value("vibration") < 7.0, "測試前提：此刻還沒踩到危險門檻"

    engine = PolicyEngine()
    findings = engine.evaluate_safety(
        _ctx(twin, keeps_running=True, full_speed=True, projection=_projection(peak_vibration=13.0, min_health=20.0))
    )
    assert engine.combine(findings) is SafetyVerdictKind.BLOCK
    assert any(f.rule_id == "SR-02P" for f in findings)


def test_a_safe_projection_passes():
    twin = FactoryTwin(seed=3)
    twin.run(5)
    engine = PolicyEngine()
    findings = engine.evaluate_safety(
        _ctx(twin, keeps_running=True, full_speed=True, projection=_projection())
    )
    assert engine.combine(findings) is SafetyVerdictKind.PASS


def test_maintenance_requires_loto_approval():
    twin = FactoryTwin(seed=3)
    twin.run(3)
    engine = PolicyEngine()
    findings = engine.evaluate_safety(
        _ctx(twin, keeps_running=False, full_speed=False, kinds={ActionKind.START_MAINTENANCE})
    )
    assert engine.combine(findings) is SafetyVerdictKind.APPROVAL_REQUIRED
    assert any(f.rule_id == "SR-09" for f in findings)


def test_maintenance_while_running_is_blocked():
    twin = FactoryTwin(seed=3)
    twin.run(3)
    engine = PolicyEngine()
    findings = engine.evaluate_safety(
        _ctx(twin, keeps_running=True, full_speed=False, kinds={ActionKind.START_MAINTENANCE})
    )
    assert engine.combine(findings) is SafetyVerdictKind.BLOCK


def test_safety_agent_gates_execution_when_conditions_change(ctx):
    twin = FactoryTwin(seed=3)
    twin.schedule(get_scenario("hazard-zone").injections)
    twin.run(6)
    agent = SafetyAgent(ctx)
    allowed, reason = agent.gate_execution([Action(ActionKind.RAISE_ALERT, "M-A")], twin.snapshot(), "M-A")
    assert not allowed and reason
    allowed, _ = agent.gate_execution([Action(ActionKind.STOP_MACHINE, "M-A")], twin.snapshot(), "M-A")
    assert allowed


def test_safety_agent_checks_the_receiving_machine_too(ctx):
    """轉單方案不能把訂單丟給一台自己也快壞掉的機器。"""
    twin = FactoryTwin(seed=3)
    twin.run(4)
    twin.runtime["M-B"].clean_signals["vibration"] = 9.5
    twin.runtime["M-B"].signals["vibration"] = 9.5

    plan = RecoveryPlan(
        plan_id="PLAN-D", title="轉單", summary="",
        actions=[Action(ActionKind.TRANSFER_ORDER, "ORD-A001", {"order_id": "ORD-A001", "to_machine": "M-B"}),
                 Action(ActionKind.STOP_MACHINE, "M-A")],
        projection=_projection(),
    )
    review = SafetyAgent(ctx).review_plan(plan, twin.snapshot(), "M-A")
    assert review.verdict is SafetyVerdictKind.BLOCK
    assert any(f.rule_id == "SR-10" for f in review.findings)


# --------------------------------------------------------------------------- 排名
def test_blocked_plans_are_infeasible_and_ranked_last():
    plans = [_plan("A", SafetyVerdictKind.BLOCK, production_pct=100.0), _plan("B", production_pct=50.0)]
    result = rank_plans(plans)
    assert result.recommended.plan_id == "B"
    blocked = next(p for p in plans if p.plan_id == "A")
    assert not blocked.feasible and "BLOCK" in blocked.infeasible_reason
    assert blocked.rank == 2


def test_approval_requirement_is_only_a_small_penalty():
    """需要人簽名是治理程序，不是安全缺陷；不該讓明顯較差的方案勝出。"""
    needs_approval = _plan("D", SafetyVerdictKind.APPROVAL_REQUIRED, production_pct=90.0, max_order_delay_min=0.0)
    autonomous = _plan("B", SafetyVerdictKind.PASS, production_pct=45.0, max_order_delay_min=300.0)
    result = rank_plans([needs_approval, autonomous])
    assert result.recommended.plan_id == "D"


def test_scores_are_absolute_not_relative_to_the_candidate_set():
    """同一個方案，不管和誰一起比，分數都應該一樣（固定尺規而非 min–max）。"""
    plan_a = _plan("A", production_pct=90.0)
    alone = rank_plans([plan_a, _plan("Z", production_pct=90.0)]).ranked[0].score
    plan_a2 = _plan("A", production_pct=90.0)
    with_others = rank_plans([plan_a2, _plan("B", production_pct=10.0), _plan("C", production_pct=50.0)])
    scored = next(p for p in with_others.ranked if p.plan_id == "A")
    assert abs(alone - scored.score) < 1e-9


def test_never_recovering_is_penalised():
    recovers = _plan("R", recovery_min=30.0, recovered=True)
    never = _plan("N", recovery_min=30.0, recovered=False)
    assert extract_criteria(never)["recovery"] > extract_criteria(recovers)["recovery"]
    result = rank_plans([recovers, never])
    assert result.recommended.plan_id == "R"


def test_weights_sum_to_one_and_safety_dominates():
    assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9
    assert DEFAULT_WEIGHTS["safety"] == max(DEFAULT_WEIGHTS.values())


def test_ranking_is_deterministic():
    def build():
        return [_plan("A", production_pct=70.0), _plan("B", production_pct=70.0), _plan("C", production_pct=70.0)]

    first = [p.plan_id for p in rank_plans(build()).ranked]
    second = [p.plan_id for p in rank_plans(build()).ranked]
    assert first == second


def test_all_plans_blocked_yields_no_recommendation():
    result = rank_plans([_plan("A", SafetyVerdictKind.BLOCK), _plan("B", SafetyVerdictKind.BLOCK)])
    assert result.recommended is None
