"""Digital Twin：決定性、故障注入、動作真的改變狀態、Ground Truth 不外洩。"""

from __future__ import annotations

import pytest

from factory_guardian.domain import Action, ActionKind, MachineState
from factory_guardian.twin.disturbances import Disturbance, RealityGap, SensorFault, SignatureMismatch
from factory_guardian.twin.engine import FactoryTwin
from factory_guardian.twin.scenarios import (
    FALSE_POSITIVE_SCENARIOS,
    SCENARIOS,
    disturbances_of,
    get_scenario,
    reality_gap_of,
)


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


# ======================================================================================
# 干擾（無故障）與現實落差
# ======================================================================================
def _configured(scenario_id: str, seed: int = 20260809) -> FactoryTwin:
    scenario = get_scenario(scenario_id)
    twin = FactoryTwin(seed=seed)
    twin.schedule(scenario.injections)
    for disturbance in disturbances_of(scenario):
        twin.add_disturbance(disturbance)
    twin.apply_reality_gap(reality_gap_of(scenario))
    return twin


@pytest.mark.parametrize("scenario_id", list(FALSE_POSITIVE_SCENARIOS))
def test_disturbance_scenarios_have_no_fault_at_all(scenario_id):
    """干擾情境的 Ground Truth 必須是空的 —— 否則它量到的就不是誤報。"""
    twin = _configured(scenario_id)
    twin.run(get_scenario(scenario_id).horizon_ticks)
    assert get_scenario(scenario_id).ground_truth == {}
    assert twin.ground_truth == {}
    assert not any(e["type"] == "secondary_damage" for e in twin.event_log)
    # 沒有任何一台機器帶著故障標籤 —— 健康度會因為讀值越界而下降（那是門檻的定義），
    # 但那不是故障，所以這裡斷言的是「沒有故障」，不是「健康度沒掉」。
    assert all(rt.fault is None for rt in twin.runtime.values())


def test_load_step_moves_the_sensor_without_breaking_the_machine():
    """負載切換：讀值真的變了，但機台沒有故障標籤。"""
    twin = _configured("fp-load-step")
    before = twin.run(12).machines["M-A"].value("current")
    after = twin.run(20).machines["M-A"].value("current")
    assert after > before + 2.0
    assert twin.runtime["M-A"].fault is None


def test_sensor_only_disturbance_never_touches_the_physics():
    """感測器尖峰只動回報值：clean_signals、健康度與產出速率都不受影響。"""
    twin = _configured("fp-sensor-spike")
    twin.run(11)
    clean_before = twin.runtime["M-A"].clean_signals["vibration"]
    rate_before = twin.effective_rate("M-A")
    snapshot = twin.step()          # 第 12 tick 就是尖峰
    assert snapshot.machines["M-A"].value("vibration") > 7.0        # 讀值踩進危險帶
    assert twin.runtime["M-A"].clean_signals["vibration"] < 3.0     # 物理狀態沒動
    assert abs(twin.runtime["M-A"].clean_signals["vibration"] - clean_before) < 0.5
    assert twin.effective_rate("M-A") == pytest.approx(rate_before, rel=0.05)


def test_stuck_sensor_reports_a_frozen_value():
    twin = FactoryTwin(seed=3)
    twin.apply_reality_gap(RealityGap(sensor_faults=(
        SensorFault(machine_id="M-A", signal="vibration", mode="stuck", stuck_value=3.8),
    )))
    values = [twin.step().machines["M-A"].value("vibration") for _ in range(6)]
    assert values == [pytest.approx(3.8)] * 6


def test_drifting_sensor_walks_away_from_the_truth():
    twin = FactoryTwin(seed=3)
    twin.apply_reality_gap(RealityGap(sensor_faults=(
        SensorFault(machine_id="M-A", signal="temperature", mode="drift",
                    drift_per_tick=0.5, drift_max=6.0),
    )))
    twin.run(1)
    early = twin.snapshot().machines["M-A"].value("temperature")
    twin.run(14)
    late = twin.snapshot().machines["M-A"].value("temperature")
    assert late > early + 4.0
    # 但機台其實沒事：真實物理狀態仍在正常範圍
    assert twin.runtime["M-A"].clean_signals["temperature"] < 66.0


def test_signature_mismatch_bends_the_symptoms_away_from_the_manual():
    """同一個故障，在有落差的機台上，振動少很多、溫升多很多。"""
    plain = FactoryTwin(seed=20260809)
    plain.schedule(get_scenario("bearing-degradation").injections)
    plain.run(16)
    atypical = _configured("bearing-atypical")
    atypical.run(16)

    p = plain.snapshot().machines["M-A"]
    a = atypical.snapshot().machines["M-A"]
    assert a.value("vibration") < p.value("vibration") * 0.6
    assert a.value("temperature") > p.value("temperature")
    # 兩邊的 Ground Truth 仍然是同一個故障
    assert atypical.ground_truth == {"M-A": "bearing_degradation"}


def test_belief_model_drops_every_reality_gap():
    """規劃模型只知道手冊：干擾、感測器故障、指紋落差都不會被帶進去。"""
    twin = _configured("bearing-atypical")
    twin.add_disturbance(Disturbance(machine_id="M-A", label="x", deltas={"current": 2.0}, start_tick=0))
    twin.run(14)
    belief = twin.fork_as_belief("M-A", "bearing_degradation", twin.snapshot().machines["M-A"].health)
    assert belief.signature_mismatch == {}
    assert belief.sensor_faults == []
    assert belief.disturbances == []
    # 真實孿生體沒有被動到
    assert twin.signature_mismatch


def test_snapshot_never_exposes_disturbances_or_reality_gap():
    twin = _configured("bearing-atypical")
    payload = str(twin.run(14).to_dict())
    for token in ("signature_mismatch", "sensor_fault", "disturbance", "reality_gap"):
        assert token not in payload
    # 但 debug_state（只給評分用）看得到
    assert twin.debug_state()["signature_mismatch"]


# ======================================================================================
# 降速工作點與不完整維修
# ======================================================================================
def test_derate_factor_actually_changes_the_operating_point():
    """降到四成和降到八成必須跑出不同的結果，否則方案掃描是假的。"""
    results = {}
    for factor in (0.4, 0.8):
        twin = FactoryTwin(seed=20260809)
        twin.schedule(get_scenario("bearing-degradation").injections)
        twin.run(12)
        clone = twin.fork()
        results[factor] = clone.project(
            [Action(ActionKind.DERATE_MACHINE, "M-A", {"factor": factor})],
            ticks=25, target_machine="M-A",
        )
    assert results[0.8]["production_pct"] > results[0.4]["production_pct"] + 5
    assert results[0.8]["peak_vibration"] > results[0.4]["peak_vibration"]


def test_derate_without_factor_keeps_the_legacy_operating_point():
    """沒給 factor 時行為必須和加入這個參數之前完全相同。"""
    def run(params):
        twin = FactoryTwin(seed=20260809)
        twin.schedule(get_scenario("bearing-degradation").injections)
        twin.run(12)
        clone = twin.fork()
        return clone.project([Action(ActionKind.DERATE_MACHINE, "M-A", params)], ticks=25, target_machine="M-A")

    assert run({}) == run({"factor": 0.60})


def test_repair_shorter_than_the_fault_needs_leaves_it_unfixed():
    """工單工時不足 → 故障只被處理掉一部分。診斷錯的代價就在這裡。"""
    twin = FactoryTwin(seed=20260809)
    twin.schedule(get_scenario("bearing-degradation").injections)   # 需要 40 分鐘
    twin.run(14)
    twin.apply(Action(ActionKind.STOP_MACHINE, "M-A"))
    twin.apply(Action(ActionKind.START_MAINTENANCE, "M-A", {"duration_min": 30}))  # 冷卻系統的工時
    twin.run(32)
    assert twin.runtime["M-A"].fault == "bearing_degradation"      # 沒修好
    assert 0.0 < twin.runtime["M-A"].fault_progress < 0.5          # 但確實好了一部分
    assert any(e["type"] == "repair_incomplete" for e in twin.event_log)


def test_repair_with_the_right_duration_clears_the_fault():
    twin = FactoryTwin(seed=20260809)
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(14)
    twin.apply(Action(ActionKind.STOP_MACHINE, "M-A"))
    twin.apply(Action(ActionKind.START_MAINTENANCE, "M-A", {"duration_min": 40}))
    twin.run(42)
    assert twin.runtime["M-A"].fault is None
    assert any(e["type"] == "maintenance_done" for e in twin.event_log)
