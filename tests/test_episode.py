"""Episode KPI 與三組對照組 Benchmark（規格 §10）。"""

from __future__ import annotations

import pytest

from factory_guardian.benchmark import REPORT_COLUMNS, run_benchmark
from factory_guardian.episode import MANUAL_DIAGNOSIS_MIN, MODES, run_episode
from factory_guardian.twin.scenarios import (
    FALSE_POSITIVE_SCENARIOS,
    REALITY_GAP_SCENARIOS,
    SCENARIOS,
    get_scenario,
)

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


def test_no_false_positives_on_scenarios_that_really_have_a_fault():
    """有故障的情境裡，不該有任何一台**健康的**機台被告警。

    這條測試以前跑遍所有情境並要求誤報為 0 —— 但當時每一個情境都真的有故障，
    所以它證明不了任何事：不是零誤報，是沒有機會誤報。
    誤報要在無故障情境上量（見下面幾條）。
    """
    for scenario_id in SCENARIOS:
        if scenario_id in FALSE_POSITIVE_SCENARIOS:
            continue
        result = run_episode(get_scenario(scenario_id), mode="guardian", persist_audit=False, require_approval=False)
        assert result.kpi.false_positive_events == 0


# --------------------------------------------------------------------------------------
# Baseline C：現行流程
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("scenario_id", EQUIPMENT_SCENARIOS)
def test_baseline_c_spends_manual_diagnosis_time_before_touching_anything(scenario_id):
    """現行流程的特徵是「先花時間判定，機台照跑」。"""
    result = run_episode(get_scenario(scenario_id), mode="baseline-c", persist_audit=False, require_approval=False)
    kpi = result.kpi
    assert kpi.detected
    # 判定時間可能被二次損壞提前打斷（故障自己揭曉），但一定遠大於 Guardian 的確認時間
    assert kpi.time_to_diagnose_min is not None
    assert 0 < kpi.time_to_diagnose_min <= MANUAL_DIAGNOSIS_MIN
    assert kpi.human_interventions == 1


@pytest.mark.parametrize("scenario_id", EQUIPMENT_SCENARIOS)
def test_baseline_c_sits_between_the_two_extremes(scenario_id):
    """A 是「告警沒被接住」、B 是「技師零等待」，現行流程在兩者之間。

    這正是加入 Baseline C 的理由：拿 A 當現況會高估我們的價值，
    拿 B 當現況會低估現行流程 —— 兩個都撐不住評審的追問。
    """
    scenario = get_scenario(scenario_id)
    kpis = {
        mode: run_episode(scenario, mode=mode, persist_audit=False, require_approval=False).kpi
        for mode in MODES
    }
    a, b, c, g = kpis["baseline-a"], kpis["baseline-b"], kpis["baseline-c"], kpis["guardian"]
    assert a.production_attainment_pct < c.production_attainment_pct < b.production_attainment_pct
    assert g.production_attainment_pct > c.production_attainment_pct
    # 現行流程真正輸掉的是時間：判定根因期間機台持續劣化
    assert c.time_to_diagnose_min > (g.time_to_diagnose_min or 0)


def test_baseline_c_has_no_camera_so_it_cannot_see_an_intrusion():
    """現行流程的工安控制是程序性的，不是偵測性的。

    把 AI 攝影機送給對照組，等於假設現況已經有了我們要新增的能力。
    """
    scenario = get_scenario("hazard-zone")
    c = run_episode(scenario, mode="baseline-c", persist_audit=False, require_approval=False).kpi
    g = run_episode(scenario, mode="guardian", persist_audit=False, require_approval=False).kpi
    assert c.hazard_exposure_min > 10 * g.hazard_exposure_min


# --------------------------------------------------------------------------------------
# False Positive：無故障干擾情境
# --------------------------------------------------------------------------------------
#: 暫態型干擾（一兩個取樣就結束）。持續型干擾（負載切換、暖機）另有自己的判準。
TRANSIENT_SCENARIOS = ["fp-sensor-spike", "fp-restart-transient"]


@pytest.mark.parametrize("scenario_id", TRANSIENT_SCENARIOS)
def test_guardian_never_touches_healthy_equipment_on_a_transient(scenario_id):
    """暫態（單點尖峰、啟動突波）不該讓 full 模式產生任何告警或動作。"""
    result = run_episode(get_scenario(scenario_id), mode="guardian", persist_audit=False, require_approval=False)
    assert result.kpi.false_positive_events == 0
    assert result.kpi.false_positive_actions == 0


def test_threshold_only_alarms_more_often_on_transients_than_the_full_detector():
    """固定門檻打在原始讀值上，一個取樣就報警；full 模式要求 2-of-3 確認。"""
    for scenario_id in ("fp-sensor-spike", "fp-restart-transient"):
        a = run_episode(get_scenario(scenario_id), mode="baseline-a", persist_audit=False, require_approval=False)
        g = run_episode(get_scenario(scenario_id), mode="guardian", persist_audit=False, require_approval=False)
        assert a.kpi.false_positive_events > g.kpi.false_positive_events


def test_guardian_abstains_when_the_signal_settles_at_a_new_steady_state():
    """負載切換：訊號真的越界，但它停在那裡不動 —— 這不是劣化，不該動設備。"""
    result = run_episode(get_scenario("fp-load-step"), mode="guardian", persist_audit=False, require_approval=False)
    assert result.kpi.false_positive_events == 1      # 告警是對的：讀值確實超過危險門檻
    assert result.kpi.abstained                       # 但處置被主動放棄
    assert result.kpi.false_positive_actions == 0
    assert "穩態" in result.loop.abstain_reason


def test_stopping_a_healthy_machine_costs_production():
    """誤報的成本不在告警，在誤動作：偵測即停機在無故障情境下產能明顯較差。"""
    scenario = get_scenario("fp-load-step")
    b = run_episode(scenario, mode="baseline-b", persist_audit=False, require_approval=False).kpi
    g = run_episode(scenario, mode="guardian", persist_audit=False, require_approval=False).kpi
    assert b.false_positive_actions > 0
    assert g.production_attainment_pct > b.production_attainment_pct + 10


# --------------------------------------------------------------------------------------
# 現實落差：初次診斷 vs 最終診斷
# --------------------------------------------------------------------------------------
def test_reality_gap_costs_confidence_and_can_flip_the_first_diagnosis():
    """徵兆偏離手冊時，偵測當下的第一次比對可能是錯的，信心度也會明顯掉下來。

    這條測試守著的是「診斷正確率 100%」不再是循環論證的結果：
    指紋知識和 Simulator 的物理在這個情境上**不是**同一份數字。
    """
    typical = run_episode(get_scenario("bearing-degradation"), mode="guardian",
                          persist_audit=False, require_approval=False).kpi
    atypical = run_episode(get_scenario("bearing-atypical"), mode="guardian",
                           persist_audit=False, require_approval=False).kpi
    assert typical.first_diagnosis_correct is True
    assert atypical.first_diagnosis_correct is False      # 偵測當下判成冷卻失效
    assert atypical.diagnosis_correct is True             # 多看幾分鐘之後糾正回來
    assert atypical.diagnosis_confidence < typical.diagnosis_confidence - 0.2


@pytest.mark.parametrize("scenario_id", list(REALITY_GAP_SCENARIOS))
def test_reality_gap_scenarios_still_end_with_the_machine_repaired(scenario_id):
    result = run_episode(get_scenario(scenario_id), mode="guardian", persist_audit=False, require_approval=False)
    assert result.kpi.machine_health_final > 95
    assert not result.kpi.secondary_damage


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
    assert data["aggregate"]["guardian"]["first_diagnosis_accuracy_pct"] == 100.0
    assert data["disclaimer"]
    keys = {c["key"] for c in data["columns"]}
    assert keys == {k for k, _, _ in REPORT_COLUMNS}


def test_benchmark_aggregate_favours_guardian():
    report = run_benchmark(scenario_ids=EQUIPMENT_SCENARIOS, persist_audit=False)
    agg = report.aggregate()
    assert agg["guardian"]["production_attainment_pct"] > agg["baseline-a"]["production_attainment_pct"]
    assert agg["guardian"]["production_attainment_pct"] > agg["baseline-b"]["production_attainment_pct"]
    assert agg["guardian"]["secondary_damage"] < agg["baseline-a"]["secondary_damage"]
