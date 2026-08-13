"""Digital Twin：決定性、故障注入、動作真的改變狀態、Ground Truth 不外洩。"""

from __future__ import annotations

import pytest

from factory_guardian.domain import Action, ActionKind, MachineState
from factory_guardian.twin.engine import FactoryTwin
from factory_guardian.twin.scenarios import SCENARIOS, get_scenario


def test_baseline_is_healthy_and_producing(twin: FactoryTwin):
    snapshot = twin.run(10)
    assert snapshot.factory_health > 95
    assert snapshot.production_pct > 90
    for machine in snapshot.machines.values():
        assert machine.worst_band.value == "normal"


def test_simulation_is_deterministic():
    a = FactoryTwin(seed=42)
    b = FactoryTwin(seed=42)
    a.schedule(get_scenario("bearing-degradation").injections)
    b.schedule(get_scenario("bearing-degradation").injections)
    sa, sb = a.run(25), b.run(25)
    assert sa.to_dict() == sb.to_dict()


def test_different_seeds_diverge():
    a, b = FactoryTwin(seed=1), FactoryTwin(seed=2)
    assert a.run(5).to_dict() != b.run(5).to_dict()


def test_snapshot_never_exposes_ground_truth(twin: FactoryTwin):
    """Agent 看得到的快照裡不能有任何故障標籤 —— 這是整個評估的前提。"""
    twin.schedule(get_scenario("bearing-degradation").injections)
    snapshot = twin.run(15)
    payload = str(snapshot.to_dict())
    assert "bearing_degradation" not in payload
    assert "fault" not in payload
    assert "fault_progress" not in payload
    # 但引擎內部確實知道
    assert twin.ground_truth == {"M-A": "bearing_degradation"}


@pytest.mark.parametrize(
    "scenario_id,signal,rising",
    [
        ("bearing-degradation", "vibration", True),
        ("cooling-failure", "temperature", True),
        ("motor-overload", "current", True),
    ],
)
def test_fault_injection_moves_the_expected_signal(scenario_id, signal, rising):
    twin = FactoryTwin(seed=7)
    before = twin.run(2).machines["M-A"].value(signal)
    twin.schedule(get_scenario(scenario_id).injections)
    after = twin.run(18).machines["M-A"].value(signal)
    assert (after > before) is rising


def test_motor_overload_drops_rpm():
    twin = FactoryTwin(seed=7)
    twin.schedule(get_scenario("motor-overload").injections)
    snapshot = twin.run(20)
    assert snapshot.machines["M-A"].value("rpm_pct") < 95.0


def test_cooling_failure_leaves_vibration_alone():
    twin = FactoryTwin(seed=7)
    twin.schedule(get_scenario("cooling-failure").injections)
    machine = twin.run(20).machines["M-A"]
    assert machine.value("temperature") > 80
    assert machine.value("vibration") < 4.0


def test_health_degrades_with_fault(twin: FactoryTwin):
    twin.schedule(get_scenario("bearing-degradation").injections)
    assert twin.run(4).machines["M-A"].health > 95
    assert twin.run(12).machines["M-A"].health < 80


def test_stop_and_maintenance_restores_machine(twin: FactoryTwin):
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(14)
    assert twin.snapshot().machines["M-A"].health < 90

    twin.apply(Action(ActionKind.STOP_MACHINE, "M-A"))
    twin.apply(Action(ActionKind.START_MAINTENANCE, "M-A", {"duration_min": 40}))
    assert twin.snapshot().machines["M-A"].state is MachineState.MAINTENANCE

    twin.run(45)
    assert twin.runtime["M-A"].fault is None
    assert twin.snapshot().machines["M-A"].health > 95
    assert twin.snapshot().machines["M-A"].state in (MachineState.RUNNING, MachineState.IDLE)


def test_transfer_order_moves_work_and_incurs_changeover(twin: FactoryTwin):
    twin.run(3)
    effect = twin.apply(
        Action(ActionKind.TRANSFER_ORDER, "ORD-A001", {"order_id": "ORD-A001", "to_machine": "M-B"})
    )
    assert effect.ok
    assert twin.orders["ORD-A001"].assigned_machine == "M-B"
    assert twin.runtime["M-B"].changeover_remaining_min > 0
    # 換線期間 M-B 沒有產出
    assert twin.effective_rate("M-B") == 0.0
    twin.run(15)
    assert twin.effective_rate("M-B") > 0.0


def test_transfer_rejects_a_machine_from_another_process_stage(twin: FactoryTwin):
    """包裝機碰得到 P-100，但它不是加工階段的替代機台。"""
    effect = twin.apply(
        Action(ActionKind.TRANSFER_ORDER, "ORD-A001", {"order_id": "ORD-A001", "to_machine": "M-C"})
    )
    assert not effect.ok
    assert twin.orders["ORD-A001"].assigned_machine == "M-A"


def test_safety_override_is_always_refused(twin: FactoryTwin):
    effect = twin.apply(Action(ActionKind.SAFETY_OVERRIDE, "M-A"))
    assert not effect.ok


def test_fork_is_isolated(twin: FactoryTwin):
    twin.run(5)
    clone = twin.fork()
    clone.apply(Action(ActionKind.STOP_MACHINE, "M-A"))
    clone.run(10)
    assert twin.snapshot().machines["M-A"].state is MachineState.RUNNING
    assert clone.snapshot().machines["M-A"].state is MachineState.STOPPED


def test_fork_as_belief_uses_believed_fault_not_the_real_one(twin: FactoryTwin):
    """規劃模型帶的是診斷結果，不是模擬器的答案。"""
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(14)
    health = twin.snapshot().machines["M-A"].health

    belief = twin.fork_as_belief("M-A", "bearing_degradation", health)
    assert belief.runtime["M-A"].fault == "bearing_degradation"
    assert twin.runtime["M-A"].fault == "bearing_degradation"      # 真實孿生體沒被動到
    assert belief is not twin
    # 診斷正確時，反推的嚴重程度應該讓信念模型重現觀測到的健康度
    assert abs(belief.snapshot().machines["M-A"].health - health) < 12


def test_belief_model_reflects_a_wrong_diagnosis(twin: FactoryTwin):
    """診斷錯了，規劃模型就會跟著錯 —— 這正是驗證階段存在的理由。

    冷卻失效只會推高溫度，它在物理上無法解釋軸承劣化造成的健康度下滑，
    所以信念模型重現不出觀測值。方案投影因此偏樂觀，最後由 Verification Agent
    在真實孿生體上抓出來。
    """
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(16)
    observed = twin.snapshot().machines["M-A"].health

    wrong = twin.fork_as_belief("M-A", "cooling_failure", observed)
    assert wrong.runtime["M-A"].fault == "cooling_failure"
    assert wrong.snapshot().machines["M-A"].health > observed + 5


def test_project_reports_trajectory_extremes(twin: FactoryTwin):
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(14)
    belief = twin.fork_as_belief("M-A", "bearing_degradation", twin.snapshot().machines["M-A"].health)
    metrics = belief.project([Action(ActionKind.RAISE_ALERT, "M-A")], ticks=25, target_machine="M-A")
    assert metrics["peak_vibration"] > 7.0        # 什麼都不做會走進危險區
    assert metrics["min_health"] < 60
    assert "fault" not in metrics and "fault_progress" not in metrics


def test_kpi_counts_only_scheduled_orders(twin: FactoryTwin):
    kpi = twin.run(5)
    metrics = twin.kpi()
    assert metrics["scheduled_orders"] >= 1
    assert metrics["backlog_orders"] >= 1
    assert metrics["max_order_delay_min"] == 0.0
    assert kpi.production_pct > 0


def test_hazard_exposure_accumulates_then_clears_on_stop():
    twin = FactoryTwin(seed=3)
    twin.schedule(get_scenario("hazard-zone").injections)
    twin.run(8)
    camera = twin.snapshot().cameras[0]
    assert camera.person_in_hazard_zone
    assert camera.ppe_compliant is False
    assert "護具不全" in get_scenario("hazard-zone").description
    assert twin.hazard_exposure_min > 0
    exposure = twin.hazard_exposure_min

    twin.apply(Action(ActionKind.STOP_MACHINE, "M-A"))
    twin.run(10)
    # 機台停下來 → 現場淨空 → 曝露不再累積
    assert twin.hazard_exposure_min == exposure


def test_unattended_fault_eventually_causes_secondary_damage():
    twin = FactoryTwin(seed=5)
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(110)
    assert any(e["type"] == "secondary_damage" for e in twin.event_log)


def test_all_scenarios_are_runnable():
    for scenario_id in SCENARIOS:
        twin = FactoryTwin(seed=11)
        twin.schedule(get_scenario(scenario_id).injections)
        snapshot = twin.run(30)
        assert snapshot.tick == 30
