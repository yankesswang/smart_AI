"""Episode 執行與 KPI 量測（規格 §10）。

一個 Episode = 在一個情境上，用某一種模式跑完整段時間，然後量 KPI。

三種模式對應規格 §10.1 的對照組：

* ``baseline-a``：只有固定 Threshold 告警，不做跨資料診斷，也不採取任何行動。
* ``baseline-b``：偵測後直接停機，不做生產影響分析與替代排程。
* ``guardian``：跨 Machine / Production / Safety 的完整閉環。

三種模式跑在**相同 seed、相同情境、相同總時長**的孿生體上，所以 KPI 可以直接比較。
Ground Truth 只在這裡用來評分，不會進入任何 Agent 的輸入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .agents.base import AgentContext
from .audit import AuditLog
from .config import Settings, get_settings
from .domain import Action, ActionKind, Scenario, Severity
from .orchestrator import ApprovalCallback, LoopResult, Orchestrator, StageCallback, auto_approve
from .policy.engine import PolicyEngine
from .twin.engine import FactoryTwin
from .twin.topology import UNIT_MARGIN_NTD

MODES = ("baseline-a", "baseline-b", "guardian")


@dataclass
class EpisodeKPI:
    """規格 §10 的五類 KPI，全部由實際執行量出來。"""

    # Diagnosis
    detected: bool = False
    detection_latency_min: float | None = None
    diagnosis_correct: bool | None = None
    diagnosis_confidence: float | None = None
    false_positive_events: int = 0

    # Maintenance
    time_to_diagnose_min: float | None = None
    work_order_completeness_pct: float | None = None

    # Production
    production_attainment_pct: float = 0.0
    production_loss_units: float = 0.0
    production_loss_ntd: float = 0.0
    max_order_delay_min: float = 0.0
    late_orders: int = 0
    recovery_min: float | None = None

    # 永續（能源／碳排）—— 全部由孿生體逐 tick 積分量出來，不是事後估算。
    energy_kwh: float = 0.0
    energy_waste_kwh: float = 0.0
    energy_waste_ntd: float = 0.0
    co2e_kg: float = 0.0
    co2e_waste_kg: float = 0.0
    energy_intensity_kwh_per_unit: float = 0.0

    # Safety
    unsafe_plans_generated: int = 0
    unsafe_plans_blocked: int = 0
    hazard_detected: bool = False
    safety_violations_executed: int = 0
    hazard_exposure_min: float = 0.0

    # Agent / 設備
    plans_considered: int = 0
    attempts: int = 0
    verification_passed: bool | None = None
    human_interventions: int = 0
    decision_latency_ms: float = 0.0
    tool_calls: int = 0
    tool_success_pct: float = 100.0
    machine_health_final: float = 100.0
    secondary_damage: bool = False

    @property
    def unsafe_block_rate_pct(self) -> float:
        if self.unsafe_plans_generated == 0:
            return 100.0
        return 100.0 * self.unsafe_plans_blocked / self.unsafe_plans_generated

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items()}
        data["unsafe_block_rate_pct"] = round(self.unsafe_block_rate_pct, 1)
        for key, value in list(data.items()):
            if isinstance(value, float):
                data[key] = round(value, 2)
        return data


@dataclass
class EpisodeResult:
    scenario_id: str
    mode: str
    horizon_ticks: int
    kpi: EpisodeKPI
    loop: LoopResult | None
    ground_truth: dict[str, str]
    final_kpi: dict[str, float]
    agent_metrics: list[dict[str, Any]] = field(default_factory=list)
    audit_path: str | None = None
    run_id: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    events_seen: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "mode": self.mode,
            "horizon_ticks": self.horizon_ticks,
            "run_id": self.run_id,
            "ground_truth": self.ground_truth,
            "kpi": self.kpi.to_dict(),
            "final_kpi": {k: round(v, 2) for k, v in self.final_kpi.items()},
            "agent_metrics": self.agent_metrics,
            "audit_path": self.audit_path,
            "settings": self.settings,
            "events_seen": self.events_seen,
            "loop": self.loop.to_dict() if self.loop else None,
        }


def run_episode(
    scenario: Scenario,
    mode: str = "guardian",
    settings: Settings | None = None,
    approval: ApprovalCallback | None = None,
    on_stage: StageCallback | None = None,
    audit: AuditLog | None = None,
    persist_audit: bool = True,
    require_approval: bool | None = None,
) -> EpisodeResult:
    """跑完一個 Episode 並回傳 KPI。"""
    if mode not in MODES:
        raise ValueError(f"未知模式 {mode}；可用：{', '.join(MODES)}")

    settings = settings or get_settings()
    audit = audit or AuditLog(settings=settings, persist=persist_audit)
    needs_approval = settings.require_approval if require_approval is None else require_approval
    ctx = AgentContext(
        settings=settings,
        audit=audit,
        policy=PolicyEngine(require_approval=needs_approval),
    )

    twin = FactoryTwin(seed=settings.seed, tick_minutes=settings.tick_seconds / 60.0)
    twin.schedule(scenario.injections)
    injection_tick = min((i.start_tick for i in scenario.injections), default=0)

    audit.log(
        "episode_start",
        "orchestrator",
        scenario=scenario.scenario_id,
        mode=mode,
        horizon_ticks=scenario.horizon_ticks,
        settings=settings.describe(),
        data_disclaimer="所有 Sensor / Orders / Manual / Maintenance History 皆為合成資料。",
    )

    monitoring_mode = "threshold_only" if mode == "baseline-a" else "full"
    orch = Orchestrator(
        twin=twin,
        ctx=ctx,
        approval=approval or auto_approve,
        monitoring_mode=monitoring_mode,
        max_attempts=1 if mode == "baseline-b" else 2,
        # settle_ticks 不覆寫：預設值已與 ProductionAgent.PLAN_HORIZON_TICKS 對齊，
        # 預測與量測必須在同一個時間尺度上比較。
        on_stage=on_stage,
    )

    kpi = EpisodeKPI()
    loop: LoopResult | None = None
    faulty_machines = {m for m in scenario.ground_truth if m != "SAFETY"}

    # --- Detect ---------------------------------------------------------------------
    # Baseline A 的定義就是「只有固定 Threshold 告警」，所以它沒有影像/工安偵測器。
    # 給它 VLM 會讓對照組失去意義 —— 那已經不是 Baseline A 了。
    event = orch.run_until_event(
        scenario.horizon_ticks,
        min_severity=Severity.WARNING,
        watch_safety=mode != "baseline-a",
    )
    kpi.false_positive_events = sum(
        1 for e in _all_events(orch) if e.machine_id not in faulty_machines
    )
    if event is not None:
        kpi.detected = True
        kpi.detection_latency_min = (event.tick - injection_tick) * twin.tick_minutes

        if mode == "baseline-a":
            # Baseline A：只告警，什麼都不做。
            twin.apply(Action(ActionKind.RAISE_ALERT, event.machine_id, {"level": event.severity.value},
                              "Baseline A：固定門檻告警"))
            audit.log("baseline_action", "baseline-a", machine_id=event.machine_id,
                      note="只發出告警，不做診斷、不做影響分析、不採取任何設備行動。")
        elif mode == "baseline-b":
            # Baseline B：偵測後直接停機，不做影響分析與替代排程。
            for action in (
                Action(ActionKind.STOP_MACHINE, event.machine_id, {}, "Baseline B：偵測即停機"),
                Action(ActionKind.START_MAINTENANCE, event.machine_id, {}, "Baseline B：直接進場維修"),
            ):
                effect = twin.apply(action)
                audit.log("baseline_action", "baseline-b", action=action.describe(), **effect.to_dict())
            kpi.human_interventions = 1
        else:
            loop = orch.handle_event(event)
            _fill_guardian_kpi(kpi, loop, scenario, twin)

    # --- 跑完剩下的時間，讓三種模式的總時長一致 ----------------------------------------
    # 三組必須跑滿同樣的 tick 數，KPI 才可比。閉環若吃掉超過 horizon 的時間，
    # 代表情境視野設得太短，會明確記錄下來而不是默默讓比較失真。
    remaining = scenario.horizon_ticks - twin.tick
    if remaining > 0:
        twin.run(remaining)
    elif remaining < 0:
        audit.log(
            "horizon_overrun", "orchestrator", mode=mode, scenario=scenario.scenario_id,
            horizon_ticks=scenario.horizon_ticks, actual_ticks=twin.tick,
            note="閉環耗時超過情境視野，跨模式 KPI 比較將不對等，請加大 horizon_ticks。",
        )

    _fill_common_kpi(kpi, twin, scenario, orch)
    final_kpi = twin.kpi()
    debug = twin.debug_state()
    kpi.secondary_damage = any(
        e.get("type") == "secondary_damage" for e in twin.event_log
    )
    kpi.hazard_exposure_min = twin.hazard_exposure_min
    kpi.machine_health_final = min(
        (m["health"] for mid, m in debug["machines"].items() if mid in faulty_machines),
        default=100.0,
    )

    audit.log(
        "episode_end",
        "orchestrator",
        scenario=scenario.scenario_id,
        mode=mode,
        kpi=kpi.to_dict(),
        final_kpi={k: round(v, 2) for k, v in final_kpi.items()},
        ground_truth=scenario.ground_truth,
        note="Ground Truth 僅用於評分，未曾進入任何 Agent 的輸入。",
    )

    return EpisodeResult(
        scenario_id=scenario.scenario_id,
        mode=mode,
        horizon_ticks=scenario.horizon_ticks,
        kpi=kpi,
        loop=loop,
        ground_truth=dict(scenario.ground_truth),
        final_kpi=final_kpi,
        agent_metrics=orch.agent_metrics(),
        audit_path=str(audit.path) if audit.path else None,
        run_id=audit.run_id,
        settings=settings.describe(),
        events_seen=len(_all_events(orch)),
    )


# --------------------------------------------------------------------------------------
# KPI 組裝
# --------------------------------------------------------------------------------------
def _all_events(orch: Orchestrator) -> list:
    """Monitoring Agent 至今觸發過的所有事件（含被忽略的低嚴重度事件）。"""
    return orch.monitoring.events


def _fill_guardian_kpi(kpi: EpisodeKPI, loop: LoopResult, scenario: Scenario, twin: FactoryTwin) -> None:
    truth = next((v for k, v in scenario.ground_truth.items() if k != "SAFETY"), None)
    if loop.diagnosis and loop.diagnosis.top:
        kpi.diagnosis_confidence = loop.diagnosis.top.confidence
        if truth is not None:
            kpi.diagnosis_correct = loop.diagnosis.top.fault_id == truth
        # Mean Time To Diagnose = 從故障注入到「診斷可據以行動」為止，
        # 含為了累積證據而刻意等待的時間。
        kpi.time_to_diagnose_min = (
            (kpi.detection_latency_min or 0.0) + loop.confirmation_ticks * twin.tick_minutes
        )
    if loop.work_order:
        kpi.work_order_completeness_pct = loop.work_order.completeness()

    kpi.plans_considered = len(loop.plans)
    kpi.attempts = len(loop.attempts)
    kpi.human_interventions = sum(1 for a in loop.attempts if not a.approval.auto) + (1 if loop.escalated else 0)

    blocked = [p for p in loop.plans if p.safety and p.safety.blocked]
    kpi.unsafe_plans_generated = len(blocked)
    kpi.unsafe_plans_blocked = sum(1 for p in blocked if not p.feasible)
    kpi.hazard_detected = any(h.get("rule_id") in ("SR-01", "SR-04", "SR-05", "SR-08") for h in loop.standing_hazards)
    executed = loop.executed_plan
    kpi.safety_violations_executed = 1 if (executed and executed.safety and executed.safety.blocked) else 0

    last = loop.attempts[-1] if loop.attempts else None
    if last and last.verification:
        kpi.verification_passed = last.verification.passed
    if executed:
        kpi.recovery_min = executed.projection.recovery_min


def _fill_common_kpi(kpi: EpisodeKPI, twin: FactoryTwin, scenario: Scenario, orch: Orchestrator) -> None:
    horizon_min = scenario.horizon_ticks * twin.tick_minutes
    nominal_units = twin.nominal_output_uph * horizon_min / 60.0
    produced = twin.completed_units_total
    kpi.production_attainment_pct = 100.0 * produced / nominal_units if nominal_units > 0 else 0.0
    kpi.production_loss_units = max(0.0, nominal_units - produced)
    kpi.production_loss_ntd = kpi.production_loss_units * UNIT_MARGIN_NTD

    # 永續：孿生體在整段 episode 中逐 tick 累積的能源帳，這裡只是讀出來。
    energy = twin.energy_kpi()
    kpi.energy_kwh = energy["energy_kwh"]
    kpi.energy_waste_kwh = energy["energy_waste_kwh"]
    kpi.energy_waste_ntd = energy["energy_waste_ntd"]
    kpi.co2e_kg = energy["co2e_kg"]
    kpi.co2e_waste_kg = energy["co2e_waste_kg"]
    kpi.energy_intensity_kwh_per_unit = energy["energy_intensity_kwh_per_unit"]

    delays = []
    for order in twin.orders.values():
        if order.done:
            continue
        finish = twin.estimate_finish_min(order.order_id)
        delays.append(max(0.0, finish - order.due_in_min))
    kpi.max_order_delay_min = max(delays, default=0.0)
    kpi.late_orders = sum(1 for d in delays if d > 0)

    metrics = orch.agent_metrics()
    kpi.tool_calls = sum(m["tool_calls"] for m in metrics)
    ok_weighted = sum(m["tool_calls"] * m["tool_success_pct"] for m in metrics)
    kpi.tool_success_pct = ok_weighted / kpi.tool_calls if kpi.tool_calls else 100.0
    total_decisions = sum(m["decisions"] for m in metrics)
    kpi.decision_latency_ms = (
        sum(m["decisions"] * m["avg_decision_latency_ms"] for m in metrics) / total_decisions
        if total_decisions
        else 0.0
    )


__all__ = ["EpisodeKPI", "EpisodeResult", "run_episode", "MODES"]
