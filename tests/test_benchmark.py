"""Benchmark 報表本身的測試。

這一份守的不是「我們的數字有多好」，而是「這張表有沒有把該說的話說完整」：

* 四組對照組都在表上（Baseline C 是後來才加的，加對照組不該悄悄改變既有的鍵）。
* 三族情境分開彙總（設備故障 / 無故障干擾 / 現實落差）——
  混在一起平均，誤報率會被有故障的情境稀釋，產能會被沒事發生的情境拉高。
* 誤報 KPI 只在無故障情境上有意義。
* 商業案例層讀得到的鍵沒有被改掉。
"""

from __future__ import annotations

import pytest

from factory_guardian.benchmark import MODE_LABELS, REPORT_COLUMNS, run_benchmark
from factory_guardian.episode import MODES
from factory_guardian.twin.scenarios import (
    EQUIPMENT_SCENARIOS,
    FALSE_POSITIVE_SCENARIOS,
    REALITY_GAP_SCENARIOS,
)


@pytest.fixture(scope="module")
def small_report():
    """一個設備故障 + 一個無故障干擾 + 一個現實落差，四個模式全跑。"""
    return run_benchmark(
        scenario_ids=["bearing-degradation", "fp-load-step", "bearing-atypical"],
        persist_audit=False,
    )


def test_every_mode_has_a_label_and_a_row(small_report):
    assert set(MODES) == set(MODE_LABELS)
    assert "baseline-c" in MODES
    for scenario_id in small_report.scenarios():
        assert set(small_report.by_scenario(scenario_id)) == set(MODES)


def test_report_columns_cover_the_new_kpis(small_report):
    keys = {k for k, _, _ in REPORT_COLUMNS}
    for required in (
        "first_diagnosis_correct",
        "diagnosis_correct",
        "replans",
        "replan_recovery_min",
        "false_positive_events",
        "false_alarm_per_hour",
        "false_positive_actions",
    ):
        assert required in keys
    data = small_report.to_dict()
    assert {c["key"] for c in data["columns"]} == keys


def test_families_split_the_scenarios(small_report):
    families = small_report.families()
    assert families["equipment"] == ["bearing-degradation", "bearing-atypical"]
    assert families["false_positive"] == ["fp-load-step"]
    assert families["reality_gap"] == ["bearing-atypical"]
    # 三族的定義來自情境本身，不是報表自己編的
    assert set(families["equipment"]) <= set(EQUIPMENT_SCENARIOS)
    assert set(families["false_positive"]) <= set(FALSE_POSITIVE_SCENARIOS)
    assert set(families["reality_gap"]) <= set(REALITY_GAP_SCENARIOS)


def test_aggregate_can_be_scoped_to_one_family(small_report):
    everything = small_report.aggregate()
    equipment = small_report.aggregate(small_report.families()["equipment"])
    assert everything["guardian"]["episodes"] == 3
    assert equipment["guardian"]["episodes"] == 2
    # 把無故障情境混進來會把產能拉高 —— 這正是要分族的理由
    assert everything["baseline-a"]["production_attainment_pct"] > \
           equipment["baseline-a"]["production_attainment_pct"]


def test_false_positive_summary_only_covers_no_fault_scenarios(small_report):
    fp = small_report.false_positive_summary()
    assert set(fp) == set(MODES)
    for row in fp.values():
        assert row["scenarios"] == 1
    # 誤報後動到設備的是「偵測即停機」；Guardian 主動棄權
    assert fp["baseline-b"]["false_positive_actions"] > 0
    assert fp["guardian"]["false_positive_actions"] == 0
    assert fp["guardian"]["abstained"] == 1
    # 現行流程沒有停機，但每一次誤報都派了一趟技師
    assert fp["baseline-c"]["false_positive_actions"] == 0
    assert fp["baseline-c"]["human_interventions"] == 1


def test_baseline_c_column_is_present_in_the_serialised_report(small_report):
    data = small_report.to_dict()
    assert "baseline-c" in data["mode_labels"]
    assert any(r["mode"] == "baseline-c" for r in data["rows"])
    assert "false_positive" in data and "aggregate_by_family" in data


def test_existing_keys_are_untouched_for_the_business_layer(small_report):
    """商業案例層吃的是 `aggregate`（全情境）與 `rows`，這兩個鍵的形狀不能變。"""
    data = small_report.to_dict()
    assert set(data["aggregate"]) == set(MODES)
    agg = data["aggregate"]["guardian"]
    for key in ("production_attainment_pct", "max_order_delay_min", "hazard_exposure_min",
                "machine_health_final", "diagnosis_accuracy_pct", "episodes"):
        assert key in agg
    row = data["rows"][0]
    assert set(row) == {"scenario_id", "mode", "kpi", "ground_truth"}


def test_first_pass_accuracy_is_lower_than_final_on_a_reality_gap_scenario():
    """現實落差情境上，初次診斷正確率**應該**比最終低 —— 那正是它存在的理由。"""
    report = run_benchmark(scenario_ids=list(REALITY_GAP_SCENARIOS), persist_audit=False)
    agg = report.aggregate(list(REALITY_GAP_SCENARIOS))["guardian"]
    assert agg["first_diagnosis_accuracy_pct"] < agg["diagnosis_accuracy_pct"]
    assert agg["diagnosis_accuracy_pct"] == 100.0
