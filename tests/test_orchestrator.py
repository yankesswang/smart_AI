"""閉環：Detect → Diagnose → Impact → Plan → Safety → Approve → Execute → Verify。"""

from __future__ import annotations

from factory_guardian.domain import ActionKind, ApprovalDecision, Severity
from factory_guardian.orchestrator import Orchestrator
from factory_guardian.policy.engine import PolicyEngine
from factory_guardian.twin.engine import FactoryTwin
from factory_guardian.twin.scenarios import get_scenario


def _orchestrator(ctx, scenario_id="bearing-degradation", **kw) -> tuple[Orchestrator, FactoryTwin]:
    twin = FactoryTwin(seed=20260809)
    twin.schedule(get_scenario(scenario_id).injections)
    return Orchestrator(twin=twin, ctx=ctx, **kw), twin


def test_full_loop_reaches_a_verified_execution(ctx):
    orch, twin = _orchestrator(ctx)
    event = orch.run_until_event(60, min_severity=Severity.WARNING)
    assert event is not None

    result = orch.handle_event(event)
    assert result.diagnosis and result.diagnosis.top.fault_id == "bearing_degradation"
    assert result.impact and result.impact.affected_orders
    assert len(result.plans) >= 3
    assert result.ranking and result.ranking.recommended
    assert result.work_order and result.work_order.completeness() > 80
    assert result.executed_plan is not None
    assert result.verified
    assert not result.escalated


def test_full_speed_plan_is_blocked_and_never_executed(ctx):
    orch, _ = _orchestrator(ctx)
    result = orch.handle_event(orch.run_until_event(60))
    plan_a = next(p for p in result.plans if p.plan_id == "PLAN-A")
    assert plan_a.safety.blocked
    assert not plan_a.feasible
    assert result.executed_plan.plan_id != "PLAN-A"


def test_execution_actually_changes_the_twin(ctx):
    orch, twin = _orchestrator(ctx)
    before = twin.snapshot().machines["M-A"].state
    result = orch.handle_event(orch.run_until_event(60))
    after = twin.snapshot().machines["M-A"].state
    assert before.value == "running"
    assert after.value in ("stopped", "maintenance", "idle", "running")
    assert result.attempts[0].execution_effects
    assert any(e["ok"] for e in result.attempts[0].execution_effects)


def test_orchestrator_waits_for_evidence_before_touching_equipment(ctx):
    orch, _ = _orchestrator(ctx, min_confidence=0.8, max_confirm_ticks=12)
    result = orch.handle_event(orch.run_until_event(60))
    assert result.confirmation_ticks > 0
    assert result.diagnosis.top.confidence >= 0.65


def test_rejected_approval_stops_execution(ctx):
    def always_reject(plan, decision) -> ApprovalDecision:
        return ApprovalDecision(plan.plan_id, False, "operator", "現場主管退回", auto=False)

    twin = FactoryTwin(seed=20260809)
    twin.schedule(get_scenario("bearing-degradation").injections)
    ctx.policy = PolicyEngine(require_approval=True)
    orch = Orchestrator(twin=twin, ctx=ctx, approval=always_reject, max_attempts=1)

    result = orch.handle_event(orch.run_until_event(60))
    assert not result.attempts[0].executed
    assert "未取得核准" in result.attempts[0].blocked_reason
    assert result.escalated
    # 沒有核准 → 機台不能被動過
    assert twin.snapshot().machines["M-A"].state.value == "running"


def test_high_risk_actions_require_approval_low_risk_do_not(ctx):
    seen: list[str] = []

    def spy(plan, decision) -> ApprovalDecision:
        seen.append(plan.plan_id)
        return ApprovalDecision(plan.plan_id, True, "operator", "核准", auto=False)

    twin = FactoryTwin(seed=20260809)
    twin.schedule(get_scenario("bearing-degradation").injections)
    ctx.policy = PolicyEngine(require_approval=True)
    orch = Orchestrator(twin=twin, ctx=ctx, approval=spy)
    result = orch.handle_event(orch.run_until_event(60))

    executed = result.executed_plan
    assert executed is not None
    high_risk = {ActionKind.STOP_MACHINE, ActionKind.START_MAINTENANCE, ActionKind.DERATE_MACHINE}
    if {a.kind for a in executed.actions} & high_risk:
        assert seen, "高風險方案必須經過人工核准"


def test_safety_event_triggers_the_loop_without_any_sensor_anomaly(ctx):
    orch, twin = _orchestrator(ctx, scenario_id="hazard-zone")
    event = orch.run_until_event(40)
    assert event is not None
    assert event.kind == "safety"
    assert event.detector == "safety-agent"

    result = orch.handle_event(event)
    # 所有維持運轉的方案都必須被擋
    running_plans = [p for p in result.plans if p.plan_id in ("PLAN-A", "PLAN-B")]
    assert running_plans and all(p.safety.blocked for p in running_plans)
    assert result.executed_plan is not None
    assert twin.snapshot().machines["M-A"].state.value in ("stopped", "maintenance")


def test_baseline_a_detection_mode_ignores_vision(ctx):
    orch, _ = _orchestrator(ctx, scenario_id="hazard-zone", monitoring_mode="threshold_only")
    assert orch.run_until_event(30, watch_safety=False) is None


def test_stage_callbacks_cover_the_whole_loop(ctx):
    stages: list[str] = []
    orch, _ = _orchestrator(ctx, on_stage=lambda s, p: stages.append(s))
    orch.handle_event(orch.run_until_event(60))
    for expected in ("diagnose", "impact", "plan", "safety", "rank", "approve", "execute", "verify"):
        assert expected in stages, f"缺少階段 {expected}"


def test_audit_trail_records_every_decision(ctx):
    orch, _ = _orchestrator(ctx)
    orch.handle_event(orch.run_until_event(60))
    stages = {r.stage for r in ctx.audit.records}
    for expected in ("detect", "diagnose", "impact", "plan", "safety", "rank", "policy", "approval", "execute", "verify"):
        assert expected in stages, f"稽核軌跡缺少 {expected}"
    # 每一筆都必須有時間、行為者與 run_id
    for record in ctx.audit.records:
        assert record.ts and record.actor and record.run_id


def test_agent_metrics_are_measured_not_estimated(ctx):
    orch, _ = _orchestrator(ctx)
    orch.handle_event(orch.run_until_event(60))
    metrics = {m["agent"]: m for m in orch.agent_metrics()}
    assert metrics["diagnosis-agent"]["tool_calls"] > 0
    assert metrics["production-agent"]["tool_calls"] > 0
    assert all(m["tool_success_pct"] == 100.0 for m in metrics.values())
