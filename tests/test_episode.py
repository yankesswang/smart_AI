"""Episode KPI 與三組對照組 Benchmark（規格 §10）。"""

from __future__ import annotations

import pytest

from factory_guardian.benchmark import REPORT_COLUMNS, run_benchmark
from factory_guardian.episode import MODES, run_episode
from factory_guardian.twin.scenarios import SCENARIOS, get_scenario

EQUIPMENT_SCENARIOS = ["bearing-degradation", "cooling-failure", "motor-overload"]


@pytest.mark.parametrize("scenario_id", list(SCENARIOS))
def test_every_scenario_runs_in_every_mode(scenario_id):
    for mode in MODES:
        result = run_episode(get_scenario(scenario_id), mode=mode, persist_audit=False, require_approval=False)
        assert result.mode == mode
        assert result.kpi.production_attainment_pct >= 0


@pytest.mark.parametrize("scenario_id", EQUIPMENT_SCENARIOS)
def test_guardian_diagnoses_correctly_and_verifies(scenario_id):
    result = run_episode(get_scenario(scenario_id), mode="guardian", persist_audit=False, require_approval=False)
    assert result.kpi.detected
    assert result.kpi.diagnosis_correct is True
    assert result.kpi.verification_passed is True
    assert result.kpi.work_order_completeness_pct == 100.0
    assert result.kpi.safety_violations_executed == 0


@pytest.mark.parametrize("scenario_id", EQUIPMENT_SCENARIOS)
def test_guardian_beats_both_baselines_on_production(scenario_id):
    scenario = get_scenario(scenario_id)
    results = {
        mode: run_episode(scenario, mode=mode, persist_audit=False, require_approval=False)
        for mode in MODES
    }
    guardian = results["guardian"].kpi.production_attainment_pct
    assert guardian > results["baseline-a"].kpi.production_attainment_pct
    assert guardian > results["baseline-b"].kpi.production_attainment_pct


@pytest.mark.parametrize("scenario_id", EQUIPMENT_SCENARIOS)
def test_doing_nothing_lets_the_machine_destroy_itself(scenario_id):
    """Baseline A 只告警不處置，設備會一路壞到二次損壞。"""
    baseline = run_episode(get_scenario(scenario_id), mode="baseline-a", persist_audit=False, require_approval=False)
    guardian = run_episode(get_scenario(scenario_id), mode="guardian", persist_audit=False, require_approval=False)
    assert baseline.kpi.secondary_damage
    assert not guardian.kpi.secondary_damage
    assert guardian.kpi.machine_health_final > baseline.kpi.machine_health_final


def test_guardian_cuts_hazard_exposure_dramatically():
    scenario = get_scenario("hazard-zone")
    baseline = run_episode(scenario, mode="baseline-a", persist_audit=False, require_approval=False)
    guardian = run_episode(scenario, mode="guardian", persist_audit=False, require_approval=False)
    # 這正是「工安是硬限制」要付出也值得付出的代價
    assert guardian.kpi.hazard_exposure_min < baseline.kpi.hazard_exposure_min / 5
    assert guardian.kpi.production_attainment_pct < baseline.kpi.production_attainment_pct


def test_full_mode_detects_no_later_than_threshold_only():
    for scenario_id in EQUIPMENT_SCENARIOS:
        scenario = get_scenario(scenario_id)
        a = run_episode(scenario, mode="baseline-a", persist_audit=False, require_approval=False)
        g = run_episode(scenario, mode="guardian", persist_audit=False, require_approval=False)
        assert g.kpi.detection_latency_min <= a.kpi.detection_latency_min


def test_no_false_positives_across_scenarios():
    for scenario_id in SCENARIOS:
        result = run_episode(get_scenario(scenario_id), mode="guardian", persist_audit=False, require_approval=False)
        assert result.kpi.false_positive_events == 0


def test_all_modes_run_the_same_number_of_ticks():
    """KPI 要能互相比較，前提是三組跑一樣久。"""
    from factory_guardian.audit import AuditLog

    for scenario_id in SCENARIOS:
        scenario = get_scenario(scenario_id)
        for mode in MODES:
            audit = AuditLog(persist=False)
            result = run_episode(scenario, mode=mode, audit=audit, persist_audit=False, require_approval=False)
            assert result.horizon_ticks == scenario.horizon_ticks
            assert not audit.by_stage("horizon_overrun"), f"{scenario_id}/{mode} 的閉環超出情境視野"


def test_episode_is_reproducible():
    """同樣的種子與情境，必須得到同樣的決策與同樣的 KPI。

    唯一排除的是掛鐘計時（decision_latency_ms）—— 那量的是這台機器跑多快，
    本來就不可能兩次一模一樣，拿它來斷言只會讓測試變得脆弱。
    """
    wallclock = {"decision_latency_ms"}
    scenario = get_scenario("bearing-degradation")
    a = run_episode(scenario, mode="guardian", persist_audit=False, require_approval=False)
    b = run_episode(scenario, mode="guardian", persist_audit=False, require_approval=False)
    ka = {k: v for k, v in a.kpi.to_dict().items() if k not in wallclock}
    kb = {k: v for k, v in b.kpi.to_dict().items() if k not in wallclock}
    assert ka == kb
    assert a.loop.executed_plan.plan_id == b.loop.executed_plan.plan_id
    assert a.loop.ranking.to_dict()["ranking"] == b.loop.ranking.to_dict()["ranking"]


def test_ground_truth_never_reaches_the_agents():
    """把整份稽核軌跡搜一遍：Agent 的輸入裡不該出現故障標籤。"""
    from factory_guardian.audit import AuditLog

    audit = AuditLog(persist=False)
    run_episode(get_scenario("bearing-degradation"), mode="guardian", audit=audit,
                persist_audit=False, require_approval=False)
    audit_records = audit.to_list()

    agent_stages = {"detect", "diagnose", "impact", "plan", "safety", "work_order", "rank"}
    for record in audit_records:
        if record["stage"] in agent_stages:
            payload = str(record["detail"])
            # 診斷紀錄本來就會提到候選故障名稱（那是它的輸出），
            # 但不能出現「ground_truth」這個欄位。
            assert "ground_truth" not in payload


def test_benchmark_report_shape():
    report = run_benchmark(scenario_ids=["bearing-degradation"], persist_audit=False)
    assert len(report.rows) == len(MODES)
    data = report.to_dict()
    assert data["aggregate"]["guardian"]["diagnosis_accuracy_pct"] == 100.0
    assert data["disclaimer"]
    keys = {c["key"] for c in data["columns"]}
    assert keys == {k for k, _, _ in REPORT_COLUMNS}


def test_benchmark_aggregate_favours_guardian():
    report = run_benchmark(scenario_ids=EQUIPMENT_SCENARIOS, persist_audit=False)
    agg = report.aggregate()
    assert agg["guardian"]["production_attainment_pct"] > agg["baseline-a"]["production_attainment_pct"]
    assert agg["guardian"]["production_attainment_pct"] > agg["baseline-b"]["production_attainment_pct"]
    assert agg["guardian"]["secondary_damage"] < agg["baseline-a"]["secondary_damage"]
