"""Edge / Cloud 部署分層與斷網降級。

這一份測試裡最重要的一條是
:func:`test_closed_loop_completes_and_verifies_with_cloud_link_down` ——
它證明「廠區對外鏈路中斷時，完整 Agent 閉環仍然跑得完並通過驗證」。
提案書裡關於中華電信 MEC 的所有論述都靠它站著；沒有它，那些論述只是形容詞。

第二重要的是 :func:`test_cloud_link_down_does_not_change_any_control_decision`：
斷網不只是「跑得完」，而是「跑出**完全相同**的決策」。降級只發生在敘述文字上。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory_guardian.config import REPO_ROOT, Settings, get_settings
from factory_guardian.deployment import (
    CAMERA_SCALES,
    CHT_CAPABILITIES,
    COMPONENTS,
    DATA_EGRESS_POLICY,
    NETWORK_ZONES,
    OfflineImpact,
    Tier,
    bandwidth_budget,
    cloud_link,
    cloud_link_down,
    components_by_tier,
    critical_path_components,
    critical_path_is_edge_only,
    deployment_report,
    is_cloud_up,
    latency_budget,
    measure_edge_decision_latency,
    reset_cloud_link,
    set_cloud_link,
)
from factory_guardian.deployment.budget import (
    SAFETY_LATENCY_TARGET_MS,
    VIDEO_BITS_PER_PIXEL,
    VIDEO_FPS,
    VIDEO_HEIGHT,
    VIDEO_OVERHEAD_FACTOR,
    VIDEO_WIDTH,
)
from factory_guardian.domain import Severity
from factory_guardian.llm import MODE_EDGE_AUTONOMOUS, LLMClient
from factory_guardian.orchestrator import Orchestrator
from factory_guardian.twin.engine import FactoryTwin
from factory_guardian.twin.scenarios import get_scenario


def _orchestrator(ctx, scenario_id: str = "bearing-degradation") -> Orchestrator:
    twin = FactoryTwin(seed=20260809)
    twin.schedule(get_scenario(scenario_id).injections)
    return Orchestrator(twin=twin, ctx=ctx)


def _decision_fingerprint(result) -> dict:
    """把一次閉環壓成「所有真正重要的決策」——不含任何敘述文字與計時。"""
    return {
        "fault": result.diagnosis.top.fault_id,
        "confidence": round(result.diagnosis.top.confidence, 6),
        "affected_orders": sorted(i.order_id for i in result.impact.affected_orders),
        "plans": [p.plan_id for p in result.plans],
        "safety": {p.plan_id: (p.safety.verdict.value if p.safety else None) for p in result.plans},
        "feasible": {p.plan_id: p.feasible for p in result.plans},
        "executed": result.executed_plan_ids,
        "verified": result.verified,
        "escalated": result.escalated,
        "kpi_after": {k: round(v, 4) for k, v in result.kpi_after.items()},
    }


# ======================================================================================
# 分層表：不變式
# ======================================================================================
def test_no_cloud_component_sits_on_the_control_critical_path():
    """整份部署設計的不變式。

    只要有人把 LLM、雲端知識庫或任何雲端服務放進
    Detect → Diagnose → Safety → Approve → Execute → Verify 的路徑，這條就會紅。
    """
    assert critical_path_is_edge_only()
    offenders = [c.component_id for c in critical_path_components() if c.tier is not Tier.EDGE]
    assert offenders == [], f"這些雲端元件跑進了控制關鍵路徑：{offenders}"


def test_every_cloud_component_declares_a_degradation_path():
    """雲端元件斷網時只能降級或延後，不允許「直接失效」。"""
    for component in components_by_tier(Tier.CLOUD):
        assert component.offline_impact in (OfflineImpact.DEGRADED, OfflineImpact.DEFERRED)
        assert component.offline_behavior.strip()


def test_every_edge_component_is_unaffected_by_a_link_outage():
    for component in components_by_tier(Tier.EDGE):
        assert component.offline_impact is OfflineImpact.NONE


def test_every_component_points_at_code_that_actually_exists():
    """分層表不能是架構圖名詞。每一列都要指得到 repo 裡真的存在的檔案或目錄。"""
    for component in COMPONENTS:
        primary = component.module.split("（")[0].split("、")[0].strip()
        assert (REPO_ROOT / primary).exists(), f"{component.component_id} 指向不存在的 {primary}"


def test_edge_tier_carries_the_whole_closed_loop():
    edge_ids = {c.component_id for c in components_by_tier(Tier.EDGE)}
    # 閉環的八個階段各自的負責元件都必須在邊緣。
    for required in (
        "monitoring-agent", "diagnosis-fingerprint", "production-agent",
        "safety-agent", "policy-engine", "digital-twin", "verification-agent", "audit-log",
    ):
        assert required in edge_ids


def test_egress_policy_keeps_video_waveforms_and_control_commands_in_the_plant():
    policy = {r.data_class: r for r in DATA_EGRESS_POLICY}
    assert not policy["原始攝影機影格"].allowed
    assert not policy["原始感測波形（加速度計 / 電流）"].allowed
    assert not policy["控制指令（stop_machine / derate / start_maintenance）"].allowed
    # 出得去的必須是決策與匯總，不是原始資料。
    assert policy["稽核軌跡 JSONL（事件、裁決、核准）"].allowed
    assert policy["模型與門檻更新（下行）"].allowed
    for rule in DATA_EGRESS_POLICY:
        assert rule.reason.strip() and rule.control.strip()


def test_network_zones_expose_exactly_one_egress_point():
    dmz = [z for z in NETWORK_ZONES if z.level == "L3.5"]
    assert len(dmz) == 1
    assert "唯一" in dmz[0].egress


def test_cht_capability_claims_are_status_tagged():
    """不准把「規劃中」寫成「已完成」。虛報比缺項更容易在追問時崩掉。"""
    allowed = {"demonstrated", "interface-ready", "requirement-derived", "roadmap"}
    for capability in CHT_CAPABILITIES:
        assert capability.status in allowed
        assert capability.evidence.strip()
    # MEC 是唯一敢寫 demonstrated 的一項，因為它有下面那條測試撐著。
    mec = next(c for c in CHT_CAPABILITIES if "MEC" in c.capability)
    assert mec.status == "demonstrated"
    positioning = next(c for c in CHT_CAPABILITIES if "定位" in c.capability)
    assert positioning.status == "roadmap"


# ======================================================================================
# 預算：數字必須是算出來的
# ======================================================================================
def test_video_bandwidth_is_derived_from_stated_assumptions():
    budget = bandwidth_budget("pilot-line")
    video = budget["streams"][0]
    expected = VIDEO_WIDTH * VIDEO_HEIGHT * VIDEO_FPS * VIDEO_BITS_PER_PIXEL * VIDEO_OVERHEAD_FACTOR / 1e6
    assert video["per_unit_mbps"] == pytest.approx(expected, rel=1e-3)
    assert video["mbps"] == pytest.approx(expected * 4, rel=1e-3)
    assert budget["scale"]["cameras"] == 4


def test_bandwidth_budget_scales_with_camera_count():
    mvp = bandwidth_budget("mvp")["total_uplink_mbps"]
    plant = bandwidth_budget("plant")["total_uplink_mbps"]
    assert plant > mvp * 20
    assert {s.scale_id for s in CAMERA_SCALES} == {"mvp", "pilot-line", "plant"}


def test_sensor_telemetry_is_negligible_next_to_video():
    """論點本身：真正吃頻寬的是影像，感測資料幾乎不算什麼。"""
    budget = bandwidth_budget("pilot-line")
    video, sensors = budget["streams"][0]["mbps"], budget["streams"][1]["mbps"]
    assert sensors < video / 100


def test_edge_preprocessing_gain_is_quantified():
    gain = bandwidth_budget()["edge_preprocessing_gain"]
    assert gain["raw_waveform_mbps"] > gain["processed_mbps"] * 50
    assert gain["reduction_factor"] > 50


def test_unknown_scale_is_rejected():
    with pytest.raises(ValueError):
        bandwidth_budget("no-such-scale")


def test_every_budget_number_declares_its_basis():
    """四種來源之外不准有第五種。"""
    allowed = {"measured", "live-measured", "vendor-typical", "assumption", "measured + assumption"}
    budget = bandwidth_budget()
    for stream in budget["streams"]:
        assert stream["basis"] in allowed
        assert stream["formula"].strip()
    assert budget["monthly_egress"]["basis"] in allowed
    assert len(budget["assumptions"]) >= 4
    for segment in latency_budget()["segments"]:
        assert segment["basis"] in allowed
        assert segment["note"].strip()


def test_safety_latency_budget_fits_inside_the_target():
    budget = latency_budget()
    assert budget["total_ms"] == pytest.approx(sum(s["ms"] for s in budget["segments"]), abs=0.1)
    assert budget["within_target"]
    assert budget["total_ms"] < SAFETY_LATENCY_TARGET_MS
    # 預算涵蓋範圍必須說清楚：這條鏈路不含人工核准。
    assert "人工核准" in budget["scope_note"]


def test_edge_decision_latency_is_measured_live_not_recorded():
    measured = measure_edge_decision_latency(iterations=50)
    assert measured["iterations"] >= 50
    assert measured["safety_gate_p95_ms"] >= 0.0
    assert measured["policy_eval_p95_ms"] >= 0.0
    # 實測值放進預算後，那兩段的 basis 要改標 live-measured。
    budget = latency_budget(measured)
    bases = {s["segment_id"]: s["basis"] for s in budget["segments"]}
    assert bases["safety-rules"] == "live-measured"
    assert bases["policy-engine"] == "live-measured"


def test_availability_math_explains_why_safety_stays_at_the_edge():
    paths = latency_budget()["availability"]["paths"]
    edge, cloud = paths[0], paths[1]
    assert edge["segments"] < cloud["segments"]
    assert edge["availability_pct"] > cloud["availability_pct"]
    assert edge["downtime_min_per_month"] < cloud["downtime_min_per_month"]


# ======================================================================================
# 鏈路狀態
# ======================================================================================
def test_cloud_link_defaults_to_up():
    assert is_cloud_up()
    assert cloud_link().to_dict()["mode"] == "cloud-assisted"


def test_cloud_link_can_be_switched_and_restored():
    set_cloud_link(False, reason="演練", actor="judge")
    state = cloud_link()
    assert not state.up and state.actor == "judge" and state.transitions == 1
    assert state.to_dict()["mode"] == "edge-autonomous"
    set_cloud_link(True, reason="恢復")
    assert is_cloud_up()


def test_cloud_link_context_manager_restores_previous_state():
    assert is_cloud_up()
    with cloud_link_down():
        assert not is_cloud_up()
    assert is_cloud_up()


def test_boot_state_follows_the_environment(monkeypatch):
    monkeypatch.setenv("FG_CLOUD_LINK", "0")
    get_settings(refresh=True)
    assert not is_cloud_up()
    monkeypatch.setenv("FG_CLOUD_LINK", "1")
    get_settings(refresh=True)
    assert is_cloud_up()


def test_settings_describe_reports_the_effective_narration_mode():
    settings = Settings(openai_api_key="sk-test", audit_dir=Path("/tmp"))
    assert settings.describe()["llm_mode"].startswith("openai:")
    with cloud_link_down():
        described = settings.describe()
    # 有金鑰也一樣：鏈路斷了就不能宣稱敘述是雲端 LLM 產生的。
    assert described["llm_mode"] == MODE_EDGE_AUTONOMOUS
    assert described["cloud_link"] == "down"


def test_reset_returns_to_the_configured_default():
    set_cloud_link(False, reason="演練")
    assert not is_cloud_up()
    reset_cloud_link()
    assert is_cloud_up()


# ======================================================================================
# 斷網降級：這一節是整份提案的落地論據
# ======================================================================================
def test_closed_loop_completes_and_verifies_with_cloud_link_down(ctx):
    """**廠區對外鏈路中斷時，完整 Agent 閉環仍然跑得完並通過驗證。**

    這正是 MEC 之所以是 MEC 的定義：偵測 → 診斷 → 安全阻擋 → 核准 → 執行 → 驗證
    全部在廠內完成，一個位元組都不需要離開工廠。
    """
    with cloud_link_down():
        orch = _orchestrator(ctx)
        event = orch.run_until_event(60, min_severity=Severity.WARNING)
        assert event is not None

        result = orch.handle_event(event)

        # 閉環的每一站都要真的發生，不是「沒有例外」而已。
        assert result.diagnosis and result.diagnosis.top.fault_id == "bearing_degradation"
        assert result.impact and result.impact.affected_orders
        assert len(result.plans) >= 3
        assert result.ranking and result.ranking.recommended
        assert result.work_order and result.work_order.completeness() > 80
        assert result.executed_plan is not None
        assert result.verified
        assert not result.escalated

        # 安全裁決在斷網下照樣擋得住「維持全速運轉」。
        plan_a = next(p for p in result.plans if p.plan_id == "PLAN-A")
        assert plan_a.safety.blocked
        assert result.executed_plan.plan_id != "PLAN-A"

        # 敘述確實走了邊緣敘述器，而且內容不是空的。
        assert result.diagnosis.narrative.strip()
        assert ctx.llm.mode == MODE_EDGE_AUTONOMOUS


def test_cloud_link_down_does_not_change_any_control_decision(ctx):
    """降級只發生在文字上：所有決策、排名、安全裁決與 KPI 完全相同。

    這比「跑得完」更強 —— 它排除了「斷網時系統其實走了另一條比較差的路」這個質疑。
    """
    orch_up = _orchestrator(ctx)
    result_up = orch_up.handle_event(orch_up.run_until_event(60, min_severity=Severity.WARNING))

    with cloud_link_down():
        orch_down = _orchestrator(ctx)
        result_down = orch_down.handle_event(orch_down.run_until_event(60, min_severity=Severity.WARNING))

    assert _decision_fingerprint(result_up) == _decision_fingerprint(result_down)


def test_degradation_is_written_into_the_audit_trail(ctx):
    with cloud_link_down():
        orch = _orchestrator(ctx)
        orch.handle_event(orch.run_until_event(60, min_severity=Severity.WARNING))

    degrades = ctx.audit.by_stage("degrade")
    assert degrades, "斷網降級必須留下稽核紀錄，不能靜悄悄發生"
    detail = degrades[0].detail
    assert detail["capability"] == "llm_narrative"
    assert detail["tier"] == "cloud"
    assert detail["control_loop_impact"] == "none"

    # 每一筆 llm_call 都要標明當時的鏈路狀態。
    calls = ctx.audit.by_stage("llm_call")
    assert calls and all(c.detail["cloud_link"] == "down" for c in calls)
    assert all(c.detail["mode"] == MODE_EDGE_AUTONOMOUS for c in calls)


def test_link_transition_is_audited(ctx):
    set_cloud_link(False, reason="評審現場演練", actor="judge", audit=ctx.audit)
    records = ctx.audit.by_stage("cloud_link")
    assert len(records) == 1
    assert records[0].detail["link"] == "down"
    assert records[0].actor == "judge"

    # 沒有實際變化就不該灌稽核紀錄。
    set_cloud_link(False, reason="重複", actor="judge", audit=ctx.audit)
    assert len(ctx.audit.by_stage("cloud_link")) == 1

    set_cloud_link(True, reason="恢復", actor="judge", audit=ctx.audit)
    assert len(ctx.audit.by_stage("cloud_link")) == 2


def test_offline_llm_guard_does_not_block_the_edge_fallback(settings, tmp_path):
    """``FG_ALLOW_OFFLINE_LLM=0`` 的用途是防止「以為在打真 LLM 其實沒有」。

    它不該在鏈路真的斷掉時把工廠的閉環一起停下來 —— 那是把設定旗標當成安全機制。
    """
    strict = Settings(
        audit_dir=tmp_path / "runs", openai_api_key=None, allow_offline_llm=False, require_approval=False
    )
    client = LLMClient(settings=strict)
    with pytest.raises(RuntimeError):
        client.narrate("s", "u", lambda: "邊緣敘述")     # 鏈路正常 + 沒金鑰 → 照舊擋下

    with cloud_link_down():
        response = client.narrate("s", "u", lambda: "邊緣敘述")
    assert response.text == "邊緣敘述"
    assert response.mode == MODE_EDGE_AUTONOMOUS


# ======================================================================================
# 報告組裝
# ======================================================================================
def test_deployment_report_is_self_consistent():
    report = deployment_report(measure_live=False)
    assert report["link"]["status"] == "up"
    assert not report["degradation"]["active"]
    assert report["tiers"]["summary"]["critical_path_is_edge_only"]
    assert report["tiers"]["summary"]["edge_count"] > report["tiers"]["summary"]["cloud_count"]
    assert report["latency_budget"]["within_target"]
    assert report["bandwidth_budget"]["total_uplink_mbps"] > 0


def test_deployment_report_lists_what_degrades_when_the_link_drops():
    with cloud_link_down():
        report = deployment_report(measure_live=False)
    assert report["degradation"]["active"]
    assert report["degradation"]["control_loop_impact"] == "none"
    cloud_ids = {c.component_id for c in components_by_tier(Tier.CLOUD)}
    edge_ids = {c.component_id for c in components_by_tier(Tier.EDGE)}
    assert set(report["degradation"]["degraded_components"]) == cloud_ids
    assert set(report["degradation"]["unaffected_components"]) == edge_ids
