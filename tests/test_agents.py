"""Agent 行為：偵測、診斷、影響、工單。"""

from __future__ import annotations

import pytest

from factory_guardian.agents.diagnosis import NO_FAULT_ID, DiagnosisAgent
from factory_guardian.agents.maintenance import MaintenanceAgent
from factory_guardian.agents.monitoring import MonitoringAgent
from factory_guardian.agents.production import ProductionAgent
from factory_guardian.domain import Severity
from factory_guardian.knowledge.retriever import KnowledgeBase
from factory_guardian.twin.engine import FactoryTwin
from factory_guardian.twin.scenarios import get_scenario


def _run_until_event(twin: FactoryTwin, agent: MonitoringAgent, max_ticks: int = 40):
    for _ in range(max_ticks):
        snapshot = twin.step()
        events = agent.detect(snapshot, twin.topo)
        significant = [e for e in events if e.severity.rank >= Severity.WARNING.rank]
        if significant:
            return significant[0], snapshot
    return None, twin.snapshot()


# --------------------------------------------------------------------------------- 監測
def test_monitoring_stays_quiet_on_a_healthy_factory(ctx):
    twin = FactoryTwin(seed=99)
    agent = MonitoringAgent(ctx)
    for _ in range(60):
        agent.detect(twin.step(), twin.topo)
    assert agent.events == [], "健康工廠不該產生任何告警（False Positive KPI）"


def test_monitoring_detects_degradation(ctx):
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    agent = MonitoringAgent(ctx)
    event, _ = _run_until_event(twin, agent)
    assert event is not None
    assert event.machine_id == "M-A"
    assert event.triggers


def test_monitoring_estimates_health_from_noisy_sensors_only(ctx):
    """Agent 不能讀模擬器的真實健康度，它自己算的值會有小幅落差。"""
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    agent = MonitoringAgent(ctx)
    for _ in range(16):
        agent.detect(twin.step(), twin.topo)
    snapshot = twin.snapshot()
    machine = snapshot.machines["M-A"]
    estimate = agent.health_estimate(machine, twin.topo.machines["M-A"].signals)
    assert estimate < 90
    assert abs(estimate - machine.health) < 15


def test_threshold_only_mode_detects_later_than_full_mode(ctx):
    """Baseline A 沒有趨勢與健康度判斷，只能等訊號真的踩線。"""
    full_twin = FactoryTwin(seed=99)
    full_twin.schedule(get_scenario("bearing-degradation").injections)
    full_event, _ = _run_until_event(full_twin, MonitoringAgent(ctx, mode="full"))

    thr_twin = FactoryTwin(seed=99)
    thr_twin.schedule(get_scenario("bearing-degradation").injections)
    thr_event, _ = _run_until_event(thr_twin, MonitoringAgent(ctx, mode="threshold_only"))

    assert full_event is not None and thr_event is not None
    assert full_event.tick <= thr_event.tick


# --------------------------------------------------------------------------------- 診斷
@pytest.mark.parametrize(
    "scenario_id,expected",
    [
        ("bearing-degradation", "bearing_degradation"),
        ("cooling-failure", "cooling_failure"),
        ("motor-overload", "motor_overload"),
    ],
)
def test_diagnosis_identifies_the_true_root_cause(ctx, scenario_id, expected):
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario(scenario_id).injections)
    monitor = MonitoringAgent(ctx)
    diagnoser = DiagnosisAgent(ctx)

    event, snapshot = _run_until_event(twin, monitor)
    assert event is not None
    # 累積一點證據再判（等同 Orchestrator 的 confirm 階段）
    for _ in range(4):
        snapshot = twin.step()
        monitor.detect(snapshot, twin.topo)
    smoothed = monitor.smoothed_readings(snapshot.machines["M-A"])
    diagnosis = diagnoser.diagnose(event, snapshot, twin.topo, smoothed)

    assert diagnosis.top is not None
    assert diagnosis.top.fault_id == expected
    assert diagnosis.top.confidence > 0.55
    assert diagnosis.top.evidence, "任何結論都必須附證據"
    assert diagnosis.narrative


def test_diagnosis_never_reads_ground_truth(ctx, monkeypatch):
    """把 ground_truth 換成會爆炸的東西：診斷過程碰它就會失敗。"""
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    monitor, diagnoser = MonitoringAgent(ctx), DiagnosisAgent(ctx)
    event, snapshot = _run_until_event(twin, monitor)

    def explode(self):
        raise AssertionError("Agent 讀取了 Simulator 的 Ground Truth")

    monkeypatch.setattr(type(twin), "ground_truth", property(explode))
    diagnosis = diagnoser.diagnose(event, snapshot, twin.topo, monitor.smoothed_readings(snapshot.machines["M-A"]))
    assert diagnosis.top is not None


def test_confidence_grows_as_evidence_accumulates(ctx):
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    monitor, diagnoser = MonitoringAgent(ctx), DiagnosisAgent(ctx)
    event, snapshot = _run_until_event(twin, monitor)
    early = diagnoser.diagnose(event, snapshot, twin.topo, monitor.smoothed_readings(snapshot.machines["M-A"]))
    for _ in range(8):
        snapshot = twin.step()
        monitor.detect(snapshot, twin.topo)
    later = diagnoser.diagnose(event, snapshot, twin.topo, monitor.smoothed_readings(snapshot.machines["M-A"]))
    assert later.top.confidence > early.top.confidence


def test_diagnosis_reports_no_fault_when_sensors_are_clean(ctx):
    """工安事件觸發時，誠實的答案是「沒有設備故障」，不是硬挑一個最像的。"""
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("hazard-zone").injections)
    monitor, diagnoser = MonitoringAgent(ctx), DiagnosisAgent(ctx)
    snapshot = twin.run(6)
    monitor.detect(snapshot, twin.topo)
    from factory_guardian.agents.safety import SafetyAgent

    event = SafetyAgent(ctx).detect_hazard_event(snapshot)
    assert event is not None and event.kind == "safety"
    diagnosis = diagnoser.diagnose(event, snapshot, twin.topo, monitor.smoothed_readings(snapshot.machines["M-A"]))
    assert diagnosis.top.fault_id == NO_FAULT_ID


def test_history_prior_favours_faults_this_machine_actually_had():
    kb = KnowledgeBase()
    prior = kb.machine_fault_prior("M-A", ["bearing_degradation", "cooling_failure", "motor_overload"])
    assert abs(sum(prior.values()) - 1.0) < 1e-6
    assert prior["bearing_degradation"] > prior["motor_overload"]


# --------------------------------------------------------------------------------- 影響 / 方案
def test_impact_finds_dependent_orders_through_the_knowledge_graph(ctx):
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(14)
    impact = ProductionAgent(ctx).assess_impact("M-A", twin.snapshot(), twin)
    assert "M-C" in impact.downstream_machines
    assert any(o.order_id == "ORD-A001" for o in impact.affected_orders)
    assert impact.narrative


def test_plans_are_state_aware(ctx):
    """機台已經在維修中，就不該再提出「降速運轉」這種動作。"""
    from factory_guardian.domain import Action, ActionKind

    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(14)
    twin.apply(Action(ActionKind.STOP_MACHINE, "M-A"))
    twin.apply(Action(ActionKind.START_MAINTENANCE, "M-A", {"duration_min": 40}))

    plans = ProductionAgent(ctx).build_plans("M-A", twin.snapshot(), twin, None)
    kinds = {a.kind for p in plans for a in p.actions}
    assert ActionKind.DERATE_MACHINE not in kinds
    assert ActionKind.STOP_MACHINE not in kinds
    assert plans and plans[0].plan_id == "PLAN-HOLD"


def test_plan_projections_come_from_simulation(ctx):
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(14)
    monitor, diagnoser = MonitoringAgent(ctx), DiagnosisAgent(ctx)
    snapshot = twin.snapshot()
    monitor.detect(snapshot, twin.topo)
    plans = ProductionAgent(ctx).build_plans("M-A", snapshot, twin, None)

    by_id = {p.plan_id: p for p in plans}
    assert {"PLAN-A", "PLAN-C", "PLAN-D"} <= set(by_id)
    # 什麼都不做 → 設備繼續劣化；轉單並維修 → 設備被保住
    assert by_id["PLAN-A"].projection.min_health < by_id["PLAN-D"].projection.min_health
    # 轉單到備援機台的產能一定優於整條線停下來
    assert by_id["PLAN-D"].projection.production_pct > by_id["PLAN-C"].projection.production_pct
    # 乾跑不會動到真實孿生體
    assert twin.snapshot().machines["M-A"].state.value == "running"


# --------------------------------------------------------------------------------- 工單
def test_work_order_is_complete_and_cites_sop(ctx):
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    monitor, diagnoser = MonitoringAgent(ctx), DiagnosisAgent(ctx)
    event, snapshot = _run_until_event(twin, monitor)
    for _ in range(6):
        snapshot = twin.step()
        monitor.detect(snapshot, twin.topo)
    diagnosis = diagnoser.diagnose(event, snapshot, twin.topo, monitor.smoothed_readings(snapshot.machines["M-A"]))

    work_order = MaintenanceAgent(ctx).create_work_order(event, diagnosis, snapshot, order_delay_min=40)
    assert work_order.completeness() == 100.0
    assert work_order.suggested_parts
    assert "SOP-MT-07" in work_order.sop_refs
    assert any("LOTO" in p for p in work_order.safety_precautions)
    assert work_order.priority in {"P1", "P2", "P3"}
    assert work_order.estimated_repair_min > 0


# ======================================================================================
# 診斷可信度的護欄：徵兆知識必須來自手冊，不能來自模擬器參數
# ======================================================================================
def test_manual_symptom_ranges_are_decoupled_from_simulator_deltas():
    """手冊區間中心必須明顯偏離模擬器的故障參數。

    這是整個診斷可信度的根：舊版把 ``FAULTS[].deltas`` 除以 scale 當成故障指紋，
    但模擬器產生訊號用的就是同一份 deltas —— 觀測向量與指紋共線，
    餘弦對純量免疫，正確答案的餘弦恆為 1.0。那是查表，不是診斷。

    手冊是設備商用他們的試驗機寫的，跟你廠裡這台不會剛好一樣。
    這個測試確保沒有人「順手」把兩者對齊回去。
    """
    from factory_guardian.knowledge.symptom_spec import SYMPTOM_SPECS
    from factory_guardian.twin.faults import FAULTS

    for fault_id, spec in SYMPTOM_SPECS.items():
        deltas = FAULTS[fault_id].deltas
        for rng in spec.ranges:
            simulated = deltas.get(rng.signal, 0.0)
            if abs(simulated) < 0.6:
                continue  # 訊號本來就幾乎不動，比例偏差沒有意義
            drift = abs(rng.center - simulated) / abs(simulated)
            assert drift >= 0.10, (
                f"{fault_id}.{rng.signal} 手冊中心 {rng.center} 與模擬器 {simulated} 過於接近"
                "（偏差 <10%）—— 這會讓診斷退化成查表"
            )


def test_diagnosis_never_imports_simulator_fault_parameters():
    """Diagnosis Agent 不得讀取 deltas / fault_progress / ground_truth。"""
    import inspect
    from factory_guardian.agents.diagnosis import DiagnosisAgent

    source = inspect.getsource(DiagnosisAgent)
    for forbidden in (".deltas", "fault_progress", "ground_truth", "rt.fault"):
        assert forbidden not in source, f"診斷程式碼碰到了模擬器內部狀態：{forbidden}"


def test_same_reading_is_judged_differently_on_machines_with_different_baselines():
    """同一個振動讀值，在交機基準不同的兩台機器上必須得到不同的判讀。

    M-A 基準 1.9 mm/s、M-B 基準 2.6 mm/s（同型號，安裝條件不同）。
    這是「以本機基準判讀」真的有生效的證明 —— 如果兩台算出一樣的偏離量，
    代表 Agent 還在用通用門檻，機器個體差異被忽略了。
    """
    from factory_guardian.knowledge.commissioning import commissioning_for

    a, b = commissioning_for("M-A"), commissioning_for("M-B")
    assert a is not None and b is not None
    va, vb = a.baseline_of("vibration"), b.baseline_of("vibration")
    assert va.baseline != vb.baseline, "同型號機台的交機基準應該不同"

    reading = 3.6
    dev_a, dev_b = va.deviation_ratio(reading), vb.deviation_ratio(reading)
    assert dev_a > dev_b, (
        f"振動 {reading} 對基準較低的 M-A 應該更異常"
        f"（M-A {dev_a:.2f} vs M-B {dev_b:.2f}）"
    )


def test_diagnosis_exposes_auditable_range_and_rule_hits(ctx):
    """診斷結果必須帶著逐項明細，讓評審能核對每個數字對應手冊哪一行。"""
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("cooling-failure").injections)
    monitor, diagnoser = MonitoringAgent(ctx), DiagnosisAgent(ctx)
    event, snapshot = _run_until_event(twin, monitor)
    for _ in range(4):
        snapshot = twin.step()
        monitor.detect(snapshot, twin.topo)
    diagnosis = diagnoser.diagnose(
        event, snapshot, twin.topo, monitor.smoothed_readings(snapshot.machines["M-A"])
    )

    assert diagnosis.baseline_ref == "COMM-M-A-2023"
    top = diagnosis.top
    assert top.range_hits, "必須逐項列出每個訊號與手冊區間的比對結果"
    assert any(h["inside"] for h in top.range_hits)
    assert top.diff_hits, "必須列出鑑別診斷規則的比值與判定"
    assert any(e.source == "commissioning" for e in top.evidence), "證據必須引用交機驗收記錄"
    # 排名的四個分項都要在，前端才畫得出算式
    assert {"manual", "differential", "prior", "docs", "combined"} <= set(top.scores)
