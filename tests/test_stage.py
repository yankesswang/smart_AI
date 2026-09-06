"""舞台 Demo 的測試。

這一份要擋住的是**競賽現場那一次**會發生的事，所以判準刻意全部是結構性的：
不斷言「信心度 = 0.69」這種數字（診斷模型還在演進，寫死只會製造假失敗），
而是斷言「診斷結果 = Ground Truth」「信心度 ≥ 動設備門檻」「八段都演到了」。

四組：

1. 劇本本身對得上研究文件 §5.1 的八段。
2. 確定性：同一指令跑兩次，決策指紋一模一樣；節奏不影響決策。
3. 最壞情況（無金鑰 ＋ 廠區對外鏈路中斷）仍然跑得完並通過所有判準。
4. 可靠性 harness 自己算得對（含刻意注入失敗的統計）。
"""

from __future__ import annotations

import json

import pytest

from factory_guardian.cli import build_parser
from factory_guardian.deployment.link import cloud_link_down, is_cloud_up
from factory_guardian.llm import MODE_EDGE_AUTONOMOUS
from factory_guardian.stage import (
    ACT_IDS,
    SCRIPT,
    SPEEDS,
    TOTAL_SECONDS,
    Metric,
    ReliabilityReport,
    RunOutcome,
    StageDirector,
    percentile,
    resolve_speed,
    run_reliability,
    script_dict,
)
from factory_guardian.stage.director import stage_settings
from factory_guardian.twin.scenarios import (
    FALSE_POSITIVE_SCENARIOS,
    REALITY_GAP_SCENARIOS,
    SCENARIOS,
    get_scenario,
)

#: 研究文件 §5.1 表格的八個「畫面」，逐字。
DOC_SCREENS = (
    "正常狀態",
    "異常出現",
    "多模態診斷",
    "證據與替代假設",
    "處置建議",
    "核准與工單",
    "維修與驗證",
    "成果總結",
)

#: 研究文件 §5.1 表格的八個時間窗。
DOC_WINDOWS = (
    (0, 25),
    (25, 55),
    (55, 90),
    (90, 125),
    (125, 155),
    (155, 185),
    (185, 215),
    (215, 240),
)


def _run(**kwargs):
    """跑一場舞台 Demo，預設不落地稽核檔、不等待。"""
    kwargs.setdefault("speed", "fast")
    kwargs.setdefault("persist_audit", False)
    return StageDirector(**kwargs).run()


# ======================================================================================
# 1. 劇本：對得上研究文件 §5.1
# ======================================================================================
def test_script_covers_exactly_the_eight_acts_of_doc_5_1():
    assert len(SCRIPT) == 8
    assert ACT_IDS == ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8")
    assert tuple(a.screen for a in SCRIPT) == DOC_SCREENS


def test_script_timeline_matches_doc_5_1_and_is_contiguous():
    """八段的時間窗必須首尾相接、總長四分鐘 —— 中間不能有講不出話的空白。"""
    assert tuple((int(a.start_s), int(a.end_s)) for a in SCRIPT) == DOC_WINDOWS
    assert SCRIPT[0].start_s == 0.0
    assert SCRIPT[-1].end_s == TOTAL_SECONDS == 240.0
    for previous, current in zip(SCRIPT, SCRIPT[1:]):
        assert current.start_s == previous.end_s, f"{previous.act_id} 與 {current.act_id} 之間有空白"
    assert sum(a.duration_s for a in SCRIPT) == TOTAL_SECONDS


def test_every_act_declares_what_it_proves_and_how_it_is_narrated():
    for act in SCRIPT:
        assert act.doc_claim.strip(), f"{act.act_id} 沒有文件 §5.1 的原始論點"
        assert act.proves.strip(), f"{act.act_id} 沒有寫出這一段要證明什麼"
        assert act.cue.strip(), f"{act.act_id} 沒有台上口白，runbook 會缺一段"
        assert act.required, f"{act.act_id} 沒有必要資料，等於這一段可以空手上台"


def test_every_required_field_is_actually_shown_somewhere():
    """必要欄位如果沒被任何 metric 或 detail 帶到畫面上，它就只是個沒人看的檢查。"""
    for act in SCRIPT:
        shown = {m.key for m in act.metrics} | set(act.details)
        assert set(act.required) <= shown, f"{act.act_id} 的 {set(act.required) - shown} 不會出現在畫面上"


def test_script_carries_no_prerecorded_numbers():
    """劇本只准寫「要念哪個欄位」，不准寫值 —— 值一律由當次執行提供。

    這條是「不要預錄字串」那個要求的可執行版本。
    """
    for act in SCRIPT:
        for key in [m.key for m in act.metrics] + list(act.details) + list(act.required):
            assert not any(ch.isdigit() for ch in key), f"{act.act_id} 的欄位名 {key} 裡有數字"


def test_metric_rendering_never_invents_a_value():
    assert Metric("k", "標籤").render(None) == "—"
    assert Metric("k", "標籤", "%", 1).render(97.44) == "97.4%"
    assert Metric("k", "標籤").render(True) == "是"
    assert Metric("k", "標籤").render(False) == "否"
    assert Metric("k", "標籤", " NTD", 0).render(32367.4) == "32,367 NTD"


def test_script_dict_is_serialisable():
    data = script_dict()
    assert len(data["acts"]) == 8
    json.dumps(data, ensure_ascii=False)          # runbook / 前端會直接吃這份


# ======================================================================================
# 2. 一場真的舞台 Demo
# ======================================================================================
def test_stage_run_plays_all_eight_acts_with_live_numbers():
    run = _run()

    assert run.ok, run.failures()
    assert [r.act.act_id for r in run.acts] == list(ACT_IDS)
    assert run.data_loss_pct == 0.0
    assert all(r.ok for r in run.acts)

    # 每一段都要有真的值可以念（不是全部 ——）。
    for report in run.acts:
        rendered = [value for _, value in report.values()]
        assert rendered, f"{report.act.act_id} 沒有任何數字"
        assert any(value != "—" for value in rendered), f"{report.act.act_id} 全部欄位都沒拿到值"


def test_stage_conclusions_are_structural_not_hardcoded():
    """所有判準都問「該發生的有沒有發生」，不比對任何預期數值。"""
    run = _run()
    scenario = get_scenario(run.scenario_id)
    truth = next(v for k, v in scenario.ground_truth.items() if k != "SAFETY")

    assert run.fingerprint["fault"] == truth
    # 信心度只要求「達到動設備門檻」，不寫死數值 —— 診斷模型還會演進。
    threshold = 0.65
    assert run.fingerprint["confidence"] >= threshold
    assert run.fingerprint["verified"] is True
    assert run.fingerprint["escalated"] is False
    assert run.fingerprint["executed"], "沒有任何方案被執行"
    assert {c.name for c in run.checks if not c.passed} == set()


def test_high_risk_action_stops_at_a_human_on_stage():
    """A6 存在的全部意義：舞台上系統要真的停下來等人簽名。"""
    run = _run()
    approvals = run.fingerprint["approvals"]
    assert approvals
    assert all(approver == "stage-supervisor" and approved for _, approver, approved in approvals)
    assert run.settings["require_approval"] is True


def test_stage_writes_a_full_audit_trail():
    run = _run()
    assert run.audit_records > 0
    assert any(c.name == "稽核軌跡涵蓋閉環每一站" and c.passed for c in run.checks)


#: 能上台演完八段的情境。
#
# 排除兩類，理由不同但同樣重要：
# * **無故障干擾情境**（fp-*）：根本沒有事故可以演。它們存在的目的正好相反 ——
#   證明系統在這些情境下**不會**演出八段（不誤報、不誤動作）。
# * **現實落差情境**：它們的重點就是診斷信心度不足與重新規劃，
#   拿「信心度必須達到動設備門檻」這把尺去量，等於在正確的行為上判自己失敗。
STAGEABLE = sorted(set(SCENARIOS) - set(FALSE_POSITIVE_SCENARIOS) - set(REALITY_GAP_SCENARIOS))


@pytest.mark.parametrize("scenario_id", STAGEABLE)
def test_every_scenario_can_be_staged(scenario_id):
    """每個「有事故可演」的情境都要能上台。

    純工安情境（hazard-zone）沒有壞掉的零件可以填，工單完整度天生比設備故障低 ——
    判準必須跟著事故種類走，不能拿同一把尺量兩種東西，否則 Demo 會在正確的行為上判自己失敗。
    """
    run = _run(scenario_id=scenario_id)
    assert run.ok, run.failures()
    assert len(run.acts) == 8
    assert run.data_loss_pct == 0.0


def test_injection_point_is_fixed_by_the_scenario():
    """「固定的異常注入腳本」——注入時點來自情境定義，不是每次隨機。"""
    scenario = get_scenario("bearing-degradation")
    assert {i.start_tick for i in scenario.injections} == {2}
    run = _run()
    detect = next(r for r in run.acts if r.act.act_id == "A2")
    assert detect.facts["detect.injected_at_min"] == 2.0
    assert detect.facts["detect.latency_min"] > 0


# ======================================================================================
# 3. 確定性
# ======================================================================================
def test_two_identical_runs_produce_the_same_decision_fingerprint():
    """同一指令跑兩次，決策結果必須完全一致。"""
    first, second = _run(), _run()
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint_hash == second.fingerprint_hash


def test_pacing_does_not_change_a_single_decision():
    """``--speed`` 只影響「印完一段停多久」，不准影響任何決策。"""
    fast = _run(speed="fast")
    paced = _run(speed=0.002)      # 每段停 duration × 0.002 秒，總計不到半秒
    assert fast.fingerprint == paced.fingerprint
    assert paced.wall_s >= paced.compute_s


def test_speed_names_resolve_and_garbage_is_rejected():
    assert resolve_speed("live") == 1.0
    assert resolve_speed("fast") == 0.0
    assert resolve_speed("0.5") == 0.5
    assert resolve_speed(2) == 2.0
    assert set(SPEEDS) == {"live", "rehearsal", "fast"}
    with pytest.raises(ValueError):
        resolve_speed("很快")


def test_seed_is_fixed_by_the_stage_not_by_the_environment(monkeypatch):
    """決賽只有一次機會，種子不吃環境變數。"""
    monkeypatch.setenv("FG_SEED", "12345")
    assert stage_settings().seed == 20260809


def test_unknown_scenario_fails_loudly():
    with pytest.raises(KeyError):
        StageDirector(scenario_id="沒有這個情境")


# ======================================================================================
# 4. 最壞情況：無金鑰 ＋ 廠區對外鏈路中斷
# ======================================================================================
def test_worst_case_offline_run_completes_and_passes_every_check():
    """文件 §5.1「Demo 可靠性原則」：現場網路中斷時仍能完成核心閉環。"""
    run = _run(offline=True)

    assert run.ok, run.failures()
    assert [r.act.act_id for r in run.acts] == list(ACT_IDS)
    assert run.data_loss_pct == 0.0
    assert run.llm_mode == MODE_EDGE_AUTONOMOUS
    assert run.link_mode == "down"
    assert run.settings["require_approval"] is True

    # 降級必須留下痕跡，而且敘述真的走了邊緣敘述器。
    names = {c.name for c in run.checks if c.passed}
    assert "斷網降級有留下稽核紀錄" in names
    assert "敘述全部走邊緣確定性敘述器" in names


def test_offline_failover_time_is_measured():
    """文件 §5.3 的「離線備援時間」：斷網到閉環完成的實際耗時。"""
    run = _run(offline=True)
    assert run.offline_failover_s is not None
    assert 0.0 < run.offline_failover_s <= run.compute_s


def test_link_outage_does_not_change_any_decision():
    """比「跑得完」更強：斷網下走的是同一條路，不是另一條比較差的路。"""
    online = _run()
    offline = _run(offline=True)
    assert online.fingerprint_hash == offline.fingerprint_hash


def test_stage_restores_the_link_state_afterwards():
    """舞台演完要把鏈路狀態放回去，否則下一場 Demo 會莫名其妙在斷網模式。"""
    assert is_cloud_up()
    _run(offline=True)
    assert is_cloud_up()

    with cloud_link_down():
        _run(offline=True)
        assert not is_cloud_up()


def test_online_run_does_not_leave_the_link_down():
    _run()
    assert is_cloud_up()


# ======================================================================================
# 5. 可靠性 harness 本身
# ======================================================================================
def test_percentile_interpolates_and_survives_a_single_sample():
    assert percentile([], 50) == 0.0
    assert percentile([1.0], 95) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)
    assert percentile([1.0, 2.0, 3.0, 4.0], 0) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0


def _report(flags: list[bool], seconds: list[float] | None = None, losses: list[float] | None = None):
    """用注入的結果組一份報表，測統計本身（不需要真的讓 Demo 失敗）。"""
    seconds = seconds or [1.0] * len(flags)
    losses = losses or [0.0] * len(flags)
    report = ReliabilityReport(scenario_id="s", runs=len(flags), offline=False)
    for index, (ok, secs, loss) in enumerate(zip(flags, seconds, losses), start=1):
        report.outcomes.append(
            RunOutcome(
                index=index,
                ok=ok,
                seconds=secs,
                fingerprint_hash="same" if ok else "drifted",
                data_loss_pct=loss,
                offline_failover_s=None,
                failures=[] if ok else ["驗證未通過"],
            )
        )
    return report


def test_longest_streak_counts_consecutive_successes_not_total():
    report = _report([True, True, False, True, True, True, False, True])
    assert report.successes == 6
    assert report.failures == 2
    assert report.longest_streak == 3           # 不是 6
    assert report.success_rate_pct == pytest.approx(75.0)
    assert report.failure_rate_pct == pytest.approx(25.0)
    assert not report.decisions_consistent      # 指紋分歧就是不一致
    assert report.failure_reasons() == ["#3: 驗證未通過", "#7: 驗證未通過"]


def test_streak_is_zero_when_the_very_first_run_fails():
    assert _report([False]).longest_streak == 0
    assert _report([False, False]).success_rate_pct == 0.0


def test_duration_statistics():
    report = _report([True] * 4, seconds=[0.1, 0.2, 0.3, 0.9])
    assert report.p50_s == pytest.approx(0.25)
    assert report.max_s == pytest.approx(0.9)
    assert report.mean_s == pytest.approx(0.375)
    assert report.p95_s <= report.max_s


def test_data_loss_rate_is_reported():
    report = _report([True, True], losses=[0.0, 3.5])
    assert report.data_loss_pct_max == pytest.approx(3.5)
    assert report.data_loss_pct_mean == pytest.approx(1.75)


def test_empty_report_is_not_consistent_by_accident():
    """沒跑過就宣稱「決策一致」是最糟糕的假陽性。"""
    report = ReliabilityReport(scenario_id="s", runs=0, offline=False)
    assert not report.decisions_consistent
    assert report.success_rate_pct == 0.0
    assert report.longest_streak == 0


def test_reliability_harness_runs_the_real_closed_loop():
    """harness 跑的是真的閉環，不是模擬跑；連續三次必須全成功且指紋一致。"""
    seen: list[int] = []
    report = run_reliability(runs=3, on_run=lambda o: seen.append(o.index))

    assert seen == [1, 2, 3]
    assert report.runs == 3 == len(report.outcomes)
    assert report.successes == 3
    assert report.longest_streak == 3
    assert report.success_rate_pct == 100.0
    assert report.decisions_consistent
    assert report.data_loss_pct_max == 0.0
    assert report.max_s > 0.0
    assert report.offline_failover_p95_s is None      # 沒開離線就不該有這個數字


def test_offline_reliability_reports_failover_times():
    report = run_reliability(runs=2, offline=True)
    assert report.successes == 2
    assert report.offline_failover_p95_s is not None
    assert report.offline_failover_max_s >= report.offline_failover_p95_s
    assert report.decisions_consistent


def test_reliability_report_serialises_for_the_proposal():
    report = run_reliability(runs=2)
    data = report.to_dict()
    for key in ("longest_streak", "success_rate_pct", "seconds", "decisions_consistent", "data_loss_pct_max"):
        assert key in data
    assert set(data["seconds"]) == {"p50", "p95", "max", "mean"}
    json.dumps(data, ensure_ascii=False)


def test_reliability_rejects_zero_runs():
    with pytest.raises(ValueError):
        run_reliability(runs=0)


# ======================================================================================
# 6. CLI 接線
# ======================================================================================
def test_cli_exposes_stage_and_stage_check():
    parser = build_parser()
    args = parser.parse_args(["stage", "--speed", "fast", "--offline", "--no-audit"])
    assert args.speed == "fast" and args.offline and args.no_audit
    assert args.scenario == "bearing-degradation"

    args = parser.parse_args(["stage-check", "--runs", "30"])
    assert args.runs == 30 and not args.offline


def test_cli_stage_can_print_the_script_without_running_it(capsys):
    parser = build_parser()
    args = parser.parse_args(["stage", "--script", "--json"])
    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert [a["screen"] for a in data["acts"]] == list(DOC_SCREENS)


def test_cli_stage_returns_zero_on_a_successful_demo():
    parser = build_parser()
    args = parser.parse_args(["stage", "--speed", "fast", "--no-audit", "--json"])
    assert args.func(args) == 0
