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


def test_time_to_threshold_is_none_on_a_healthy_machine(ctx):
    """健康機台不能被量測雜訊外推出一個假的踩線時間。

    這是 TTT 的 False Positive 防線：只要健康的機台會冒出「12 分鐘後踩線」，
    這個數字在現場就一文不值 —— 沒有人會相信第二次。
    """
    twin = FactoryTwin(seed=99)
    agent = MonitoringAgent(ctx)
    for _ in range(60):
        snapshot = twin.step()
        agent.detect(snapshot, twin.topo)
        for mid, machine in snapshot.machines.items():
            estimate = agent.time_to_threshold(machine, twin.topo.machines[mid].signals)
            assert estimate.minutes is None, f"{mid} 在健康狀態下不該有 TTT（得到 {estimate.minutes}）"


def test_time_to_threshold_shrinks_as_the_machine_degrades(ctx):
    """劣化中的機台：TTT 必須隨著 tick 一路變小，最後歸零（已經踩線）。"""
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    agent = MonitoringAgent(ctx)
    specs = twin.topo.machines["M-A"].signals

    series: list[tuple[int, float, str]] = []
    for _ in range(20):
        snapshot = twin.step()
        agent.detect(snapshot, twin.topo)
        estimate = agent.time_to_threshold(snapshot.machines["M-A"], specs)
        if estimate.minutes is not None:
            series.append((snapshot.tick, estimate.minutes, estimate.runtime))

    assert len(series) >= 5, "劣化情境應該要有連續數個 tick 算得出 TTT"
    minutes = [m for _, m, _ in series]
    assert minutes == sorted(minutes, reverse=True), f"TTT 必須隨 tick 遞減：{minutes}"
    assert minutes[0] > 0 and minutes[-1] == 0.0, "最後應該已經踩線（TTT = 0）"
    # runtime 一定要標示是誰算的：時序模型或線性外推，不能含糊。
    runtimes = {rt for _, _, rt in series}
    assert runtimes <= {"linear-extrapolation"} | {rt for rt in runtimes if rt.startswith("forecast:")}
    assert all(rt for _, _, rt in series)
    assert any(rt.startswith("forecast:") for _, _, rt in series), "接近踩線時應該呼叫得到預測模組"


def test_anomaly_event_carries_time_to_threshold(ctx):
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    agent = MonitoringAgent(ctx)
    event, _ = _run_until_event(twin, agent)
    assert event is not None
    assert "time_to_threshold_min" in event.to_dict()
    if event.time_to_threshold_min is not None:
        assert event.time_to_threshold_detail["runtime"]


def test_failure_risk_rises_when_the_threshold_is_close(ctx):
    """TTT 進了 failure_risk：同一個健康度下，快踩線的機台風險必須比較高。"""
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    agent = MonitoringAgent(ctx)
    specs = twin.topo.machines["M-A"].signals
    snapshot = twin.snapshot()
    for _ in range(8):
        snapshot = twin.step()
        agent.detect(snapshot, twin.topo)

    machine = snapshot.machines["M-A"]
    with_ttt = agent.failure_risk(machine, specs)
    agent.last_ttt.clear()
    agent._ttt_cache.clear()
    agent.forecast_service = None
    baseline_agent = MonitoringAgent(ctx, mode="threshold_only")
    baseline_agent.windows = agent.windows
    baseline_agent.history = agent.history
    without_ttt = baseline_agent.failure_risk(machine, specs)
    assert with_ttt >= without_ttt


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


def test_plan_families_search_their_parameters(ctx):
    """四個方案不再是固定模板：每個家族都掃描過一小組參數，掃描結果留在 plan.variants。"""
    from factory_guardian.agents.production import DERATE_DEFAULT, DERATE_GRID, MAINTENANCE_DELAY_GRID_MIN

    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(8)
    snapshot = twin.snapshot()
    monitor = MonitoringAgent(ctx)
    monitor.detect(snapshot, twin.topo)
    plans = {p.plan_id: p for p in ProductionAgent(ctx).build_plans("M-A", snapshot, twin, None)}

    plan_b = plans["PLAN-B"]
    assert [v["params"]["derate_factor"] for v in plan_b.variants] == list(DERATE_GRID)
    selected = [v for v in plan_b.variants if v["selected"]]
    assert len(selected) == 1
    # 孿生體的降速工作點現在真的跟著 factor 走（見 twin.engine._act_derate），
    # 所以掃描是有鑑別力的：被選中的那一個必須來自網格，而且變體之間的分數不該全部相同。
    assert selected[0]["params"]["derate_factor"] in DERATE_GRID
    assert DERATE_DEFAULT in DERATE_GRID
    scores = {round(v["score"], 6) for v in plan_b.variants}
    assert len(scores) > 1, "降速幅度不同卻得到同樣的分數，代表 factor 沒有真的被套用"

    plan_c = plans["PLAN-C"]
    assert [v["params"]["maintenance_delay_min"] for v in plan_c.variants] == list(MAINTENANCE_DELAY_GRID_MIN)
    chosen_c = next(v for v in plan_c.variants if v["selected"])
    assert not chosen_c["blocked"] and chosen_c["executable"]

    # 每個家族只有一個變體進排名
    for plan in plans.values():
        assert sum(1 for v in plan.variants if v["selected"]) <= 1


def test_blocked_variants_never_represent_their_family(ctx):
    """搜尋必須走 Safety 預測規則：延後停機會讓振動峰值衝進危險區，那種變體不能出線。

    這裡要帶著診斷跑：信念模型裡沒有故障，乾跑就不會劣化，也就測不到這道閘門 ——
    那正是「規劃跑在診斷信念模型上」這個設計的意義。
    """
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("bearing-degradation").injections)
    monitor, diagnoser = MonitoringAgent(ctx), DiagnosisAgent(ctx)
    event, snapshot = _run_until_event(twin, monitor)
    for _ in range(4):
        snapshot = twin.step()
        monitor.detect(snapshot, twin.topo)
    diagnosis = diagnoser.diagnose(
        event, snapshot, twin.topo, monitor.smoothed_readings(snapshot.machines["M-A"])
    )
    plans = {p.plan_id: p for p in ProductionAgent(ctx).build_plans("M-A", snapshot, twin, diagnosis)}

    delayed = [v for v in plans["PLAN-C"].variants if v["params"]["maintenance_delay_min"] > 0]
    assert delayed, "PLAN-C 應該掃描過延後停機的變體"
    assert all(v["blocked"] for v in delayed), "延後停機讓振動峰值超過危險門檻，應該被 Safety 預測規則擋下"
    assert all(not v["selected"] for v in delayed)
    # 被擋下的變體仍然要留在紀錄裡：它就是「為什麼不延後」的證據。
    assert all(v["peak_vibration"] > plans["PLAN-C"].projection.peak_vibration for v in delayed)


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


# --------------------------------------------------------------------------------- 多原型指紋
class TestMultiPrototypeFingerprint:
    """一個故障對多個指紋方向（`docs/factory_guardian/external_validation.md` §8.2 的改進）。

    這一組測試守的是兩件事，而且第二件比第一件重要：

    1. 多原型真的會在該生效的時候生效（失載方向的觀測要命中 `under_load`）。
    2. **只有主原型的故障，行為逐位元不變**。多原型是新增能力，不是改寫既有行為 ——
       Demo 上所有既有數字都綁在主原型上，動到它們就等於讓提案書裡的數字失效。
    """

    @staticmethod
    def _signatures():
        from factory_guardian.twin.faults import fault_signatures

        return {
            s.fault_id: s
            for s in fault_signatures(
                {"temperature": 10.0, "vibration": 3.0, "current": 2.0, "rpm_pct": 10.0}
            )
        }

    def test_primary_prototype_is_always_index_zero(self):
        """索引 0 永遠是手冊主徵兆。稽核輸出的 `prototype` 欄位靠這條規則才解讀得了。"""
        from factory_guardian.domain import PRIMARY_PROTOTYPE

        for sig in self._signatures().values():
            assert sig.prototypes[0].name == PRIMARY_PROTOTYPE
            assert sig.prototypes[0].profile == sig.profile

    def test_every_alternate_prototype_carries_a_rationale(self):
        """多一個原型就是多一個可以誤命中的方向。沒有手冊理由的原型是在替方法開後門。"""
        for sig in self._signatures().values():
            for prototype in sig.alt_prototypes:
                assert prototype.rationale.strip(), f"{sig.fault_id}.{prototype.name} 缺少理由"

    def test_single_prototype_faults_are_unchanged(self):
        """只有主原型時，多原型比對必須等於原本的單一餘弦 —— 逐位元。"""
        sig = self._signatures()["bearing_degradation"]
        assert len(sig.prototypes) == 1
        observed = {"vibration": 2.1, "temperature": 1.4, "current": 0.8, "rpm_pct": -0.5}
        index, name, cos = DiagnosisAgent._match_prototype(observed, sig)
        assert (index, name) == (0, "primary")
        assert cos == DiagnosisAgent._cosine(observed, sig.profile)

    def test_opposite_direction_hits_the_alternate_prototype(self):
        """失載（電流下降、轉速衝高）必須命中 `under_load`，而不是被判成「不像馬達故障」。"""
        sig = self._signatures()["motor_overload"]
        under_load = {"temperature": -0.4, "vibration": 0.4, "current": -2.0, "rpm_pct": 0.6}
        index, name, cos = DiagnosisAgent._match_prototype(under_load, sig)
        assert name == "under_load" and index == 1
        # 單一原型下這個方向的餘弦是負的 —— 也就是整個故障會被排除掉。
        assert DiagnosisAgent._cosine(under_load, sig.profile) < 0.0
        assert cos > 0.9

    def test_overload_still_hits_the_primary_prototype(self):
        """反向確認：真正的過載不能被替代方向搶走解釋權。"""
        sig = self._signatures()["motor_overload"]
        overload = {"temperature": 1.3, "vibration": 0.5, "current": 3.0, "rpm_pct": -1.8}
        index, name, _ = DiagnosisAgent._match_prototype(overload, sig)
        assert (index, name) == (0, "primary")

    def test_demo_scenarios_still_hit_the_primary_prototype(self, ctx):
        """Demo 三條主線情境都必須由主原型拿下，否則畫面上的說明會換一個故事。"""
        for scenario_id in ("bearing-degradation", "cooling-failure", "motor-overload"):
            twin = FactoryTwin(seed=99)
            twin.schedule(get_scenario(scenario_id).injections)
            monitor, diagnoser = MonitoringAgent(ctx), DiagnosisAgent(ctx)
            event, snapshot = _run_until_event(twin, monitor)
            for _ in range(4):
                snapshot = twin.step()
                monitor.detect(snapshot, twin.topo)
            diagnosis = diagnoser.diagnose(
                event, snapshot, twin.topo, monitor.smoothed_readings(snapshot.machines["M-A"])
            )
            assert diagnosis.top.scores["prototype"] == 0.0, scenario_id

    def test_scores_stay_numeric_and_serialisable(self, ctx):
        """`scores` 的值一律是數字：`stage/director.py` 會用 `f"{v:.3f}"` 逐項格式化，
        塞字串進去會讓 Demo 導播稿在執行時炸掉。"""
        twin = FactoryTwin(seed=99)
        twin.schedule(get_scenario("bearing-degradation").injections)
        monitor, diagnoser = MonitoringAgent(ctx), DiagnosisAgent(ctx)
        event, snapshot = _run_until_event(twin, monitor)
        diagnosis = diagnoser.diagnose(
            event, snapshot, twin.topo, monitor.smoothed_readings(snapshot.machines["M-A"])
        )
        for candidate in diagnosis.candidates:
            for key, value in candidate.scores.items():
                assert isinstance(value, (int, float)), f"{key} 不是數字"
                assert f"{value:.3f}"
        assert diagnosis.to_dict()["candidates"][0]["scores"]["prototype"] == 0.0


# --------------------------------------------------------------------------------- 判別式接手層
class TestDiscriminativeReranker:
    """判別式模型接手排序（`docs/factory_guardian/external_validation.md` §8.3）。

    最重要的一條是 `test_default_is_bit_identical`：這一層預設不存在，
    存在但權重為 0 時也不得改變任何一個數字。否則「加了一層新東西」就會
    默默改掉提案書、Dashboard 與稽核軌跡上所有既有的信心度。
    """

    @staticmethod
    def _diagnose(ctx, reranker=None, scenario_id="bearing-degradation"):
        twin = FactoryTwin(seed=99)
        twin.schedule(get_scenario(scenario_id).injections)
        monitor = MonitoringAgent(ctx)
        diagnoser = DiagnosisAgent(ctx, reranker=reranker)
        event, snapshot = _run_until_event(twin, monitor)
        for _ in range(4):
            snapshot = twin.step()
            monitor.detect(snapshot, twin.topo)
        return diagnoser.diagnose(
            event, snapshot, twin.topo, monitor.smoothed_readings(snapshot.machines["M-A"])
        )

    def test_default_is_bit_identical(self, ctx):
        """沒掛、或掛了但權重 0 —— 兩種情況都必須與原本逐位元相同。"""
        from factory_guardian.agents.diagnosis import DiscriminativeReranker

        base = self._diagnose(ctx)
        zero = self._diagnose(ctx, DiscriminativeReranker(weight=0.0))
        for a, b in zip(base.candidates, zero.candidates):
            assert a.fault_id == b.fault_id
            assert a.confidence == b.confidence
            assert a.scores["combined"] == b.scores["combined"]
        assert base.weights["rerank"] == 0.0
        assert zero.weights["rerank"] == 0.0

    def test_disabled_state_is_recorded_not_silent(self, ctx):
        """降級一定要留下理由。靜默跳過等於騙人。"""
        diagnosis = self._diagnose(ctx)
        assert diagnosis.reranker["enabled"] is False
        assert diagnosis.reranker["reason"], "沒啟用也必須說明為什麼"

    def test_weights_still_add_up(self, ctx):
        """Dashboard 的「合計 = 各項相加」在多了一項之後仍要成立。"""
        from factory_guardian.agents.diagnosis import DiscriminativeReranker

        for reranker in (None, DiscriminativeReranker(weight=0.12)):
            diagnosis = self._diagnose(ctx, reranker)
            weights = diagnosis.weights
            for candidate in diagnosis.candidates:
                scores = candidate.scores
                if "cosine" not in scores:
                    continue
                expected = (
                    weights["signature"] * max(0.0, scores["cosine"])
                    + weights["prior"] * scores["prior"]
                    + weights["docs"] * scores["docs"]
                    + weights["rerank"] * scores["rerank"]
                )
                assert scores["combined"] == pytest.approx(expected, abs=1e-9)

    def test_probabilities_are_a_distribution_when_enabled(self, ctx):
        from factory_guardian.agents.diagnosis import DiscriminativeReranker

        diagnosis = self._diagnose(ctx, DiscriminativeReranker(weight=0.12))
        if not diagnosis.reranker["enabled"]:
            pytest.skip("本環境沒有 scikit-learn，判別式接手層已正確降級")
        probabilities = diagnosis.reranker["probabilities"]
        assert abs(sum(probabilities.values()) - 1.0) < 1e-6
        assert diagnosis.reranker["cases"] >= diagnosis.reranker["min_cases"]

    def test_machines_without_enough_history_stay_disabled(self, ctx):
        """M-C（包裝機）只有 3 筆標註案例 —— 這正是冷啟動，判別式模型不該參與。"""
        from factory_guardian.agents.diagnosis import (
            MIN_LABELLED_CASES,
            DiscriminativeReranker,
        )
        from factory_guardian.knowledge.corpus import MAINTENANCE_HISTORY

        twin = FactoryTwin(seed=99)
        machine = twin.topo.machines["M-C"]
        state = DiscriminativeReranker(weight=0.12).probabilities(
            machine, "M-C", {s.name: s.nominal for s in machine.signals}, ["bearing_degradation"]
        )
        cases = sum(1 for c in MAINTENANCE_HISTORY if c.machine_id == "M-C" and c.readings)
        assert cases < MIN_LABELLED_CASES
        assert state["enabled"] is False
        assert "M-C" in str(state["reason"]) or "scikit-learn" in str(state["reason"])

    def test_training_data_is_labelled_synthetic(self):
        """訓練資料是合成維修歷史。這件事必須是程式裡宣告出來的事實。"""
        from factory_guardian.knowledge.corpus import MAINTENANCE_HISTORY

        with_readings = [c for c in MAINTENANCE_HISTORY if c.readings]
        assert with_readings, "沒有任何案例帶讀值，判別式接手層永遠訓練不起來"
        assert all(c.readings_synthetic for c in with_readings)
        assert all(c.synthetic for c in with_readings)

    def test_readings_agree_with_the_symptom_text(self):
        """`symptoms` 裡寫出來的數字要和 `readings` 對得上 —— 文字與數值不可以互相打臉。"""
        import re

        from factory_guardian.knowledge.corpus import MAINTENANCE_HISTORY

        checked = 0
        for case in MAINTENANCE_HISTORY:
            for pattern, signal in (
                (r"振動[^0-9]{0,4}([0-9.]+)\s*mm/s", "vibration"),
                (r"電流[^0-9]{0,4}([0-9.]+)\s*A", "current"),
            ):
                match = re.search(pattern, case.symptoms)
                if match and signal in case.readings:
                    assert case.readings[signal] == pytest.approx(float(match.group(1))), case.case_id
                    checked += 1
        assert checked >= 10, "抽樣核對的筆數太少，這個測試等於沒守住東西"
