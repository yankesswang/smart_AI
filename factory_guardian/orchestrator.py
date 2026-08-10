"""Orchestrator：把六個 Agent 串成一條可執行、可核准、可驗證的閉環。

Detect → Diagnose → Impact → Plan → Safety → Approve → Execute → Verify

三條紅線由這裡強制執行：
* Agent 拿不到 Ground Truth（只透過 ``twin.snapshot()``）。
* 方案排名由 ``optimizer.rank_plans()`` 算，不是 LLM。
* 執行後一定回到真實孿生體驗證；驗證失敗就改採次佳方案重跑（Retry Loop）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .agents.base import AgentContext
from .agents.diagnosis import DiagnosisAgent
from .agents.maintenance import MaintenanceAgent
from .agents.monitoring import MonitoringAgent
from .agents.production import PLAN_HORIZON_TICKS, ProductionAgent
from .agents.safety import SafetyAgent
from .agents.verification import VerificationAgent
from .audit import AuditLog
from .domain import (
    Action,
    ActionKind,
    AnomalyEvent,
    ApprovalDecision,
    Diagnosis,
    ImpactAssessment,
    RecoveryPlan,
    Severity,
    VerificationReport,
    WorkOrder,
)
from .optimizer import RankingResult, explain_ranking, rank_plans
from .policy.engine import PolicyDecision
from .twin.engine import FactoryTwin

ApprovalCallback = Callable[[RecoveryPlan, PolicyDecision], ApprovalDecision]
StageCallback = Callable[[str, dict[str, Any]], None]


def auto_approve(plan: RecoveryPlan, decision: PolicyDecision) -> ApprovalDecision:
    """腳本化 Demo / Benchmark 用的自動核准；仍會完整寫進稽核軌跡。"""
    return ApprovalDecision(
        plan_id=plan.plan_id,
        approved=decision.allowed,
        approver="auto-approver",
        reason="自動核准模式" if decision.allowed else "動作被 Policy 禁止",
        auto=True,
    )


@dataclass
class LoopAttempt:
    """一次「核准 → 執行 → 驗證」的嘗試。驗證失敗會產生下一次嘗試。"""

    attempt: int
    plan: RecoveryPlan
    policy: PolicyDecision
    approval: ApprovalDecision
    executed: bool
    execution_effects: list[dict[str, Any]] = field(default_factory=list)
    verification: VerificationReport | None = None
    blocked_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "plan": self.plan.to_dict(),
            "policy": self.policy.to_dict(),
            "approval": self.approval.to_dict(),
            "executed": self.executed,
            "blocked_reason": self.blocked_reason,
            "execution_effects": self.execution_effects,
            "verification": self.verification.to_dict() if self.verification else None,
        }


@dataclass
class LoopResult:
    """一次完整閉環的結果。"""

    triggered: bool
    event: AnomalyEvent | None = None
    detection_tick: int | None = None
    confirmation_ticks: int = 0     # 為了累積證據而多等的 tick 數
    diagnosis: Diagnosis | None = None
    impact: ImpactAssessment | None = None
    plans: list[RecoveryPlan] = field(default_factory=list)
    ranking: RankingResult | None = None
    ranking_explanation: str = ""
    work_order: WorkOrder | None = None
    attempts: list[LoopAttempt] = field(default_factory=list)
    standing_hazards: list[dict[str, Any]] = field(default_factory=list)
    escalated: bool = False
    escalation_reason: str = ""
    kpi_before: dict[str, float] = field(default_factory=dict)
    kpi_after: dict[str, float] = field(default_factory=dict)

    @property
    def executed_plan(self) -> RecoveryPlan | None:
        """實際執行的**第一個**方案 —— 那才是這次事故的處置決策。

        重試時後續執行的多半是「維持現行處置」之類的收尾動作，
        用最後一個當標題會讓報表看起來像是系統做了個奇怪的決定。
        """
        for attempt in self.attempts:
            if attempt.executed:
                return attempt.plan
        return None

    @property
    def executed_plan_ids(self) -> list[str]:
        return [a.plan.plan_id for a in self.attempts if a.executed]

    @property
    def verified(self) -> bool:
        last = self.attempts[-1] if self.attempts else None
        return bool(last and last.verification and last.verification.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "detection_tick": self.detection_tick,
            "confirmation_ticks": self.confirmation_ticks,
            "event": self.event.to_dict() if self.event else None,
            "diagnosis": self.diagnosis.to_dict() if self.diagnosis else None,
            "impact": self.impact.to_dict() if self.impact else None,
            "plans": [p.to_dict() for p in self.plans],
            "ranking": self.ranking.to_dict() if self.ranking else None,
            "ranking_explanation": self.ranking_explanation,
            "work_order": self.work_order.to_dict() if self.work_order else None,
            "attempts": [a.to_dict() for a in self.attempts],
            "standing_hazards": self.standing_hazards,
            "escalated": self.escalated,
            "escalation_reason": self.escalation_reason,
            "executed_plan_id": self.executed_plan.plan_id if self.executed_plan else None,
            "executed_plan_ids": self.executed_plan_ids,
            "verified": self.verified,
            "kpi_before": {k: round(v, 2) for k, v in self.kpi_before.items()},
            "kpi_after": {k: round(v, 2) for k, v in self.kpi_after.items()},
        }


class Orchestrator:
    """協調流程與重試（規格 §5 表格中 Orchestrator 的職責）。"""

    def __init__(
        self,
        twin: FactoryTwin,
        ctx: AgentContext | None = None,
        approval: ApprovalCallback | None = None,
        monitoring_mode: str = "full",
        max_attempts: int = 2,
        # 與 ProductionAgent.PLAN_HORIZON_TICKS 對齊，預測與量測才在同一個時間尺度上。
        settle_ticks: int = PLAN_HORIZON_TICKS,
        on_stage: StageCallback | None = None,
        min_confidence: float = 0.65,
        max_confirm_ticks: int = 10,
    ) -> None:
        self.twin = twin
        self.ctx = ctx or AgentContext()
        self.approval = approval or auto_approve
        self.max_attempts = max_attempts
        self.settle_ticks = settle_ticks
        self.on_stage = on_stage
        # 動設備之前要求的最低診斷信心度；不到就繼續觀察（見 confirm_diagnosis）。
        self.min_confidence = min_confidence
        self.max_confirm_ticks = max_confirm_ticks

        self.monitoring = MonitoringAgent(self.ctx, mode=monitoring_mode)
        self.diagnosis = DiagnosisAgent(self.ctx)
        self.production = ProductionAgent(self.ctx)
        self.safety = SafetyAgent(self.ctx)
        self.maintenance = MaintenanceAgent(self.ctx)
        self.verification = VerificationAgent(self.ctx)

    @property
    def audit(self) -> AuditLog | None:
        return self.ctx.audit

    @property
    def agents(self) -> list:
        return [self.monitoring, self.diagnosis, self.production, self.safety, self.maintenance, self.verification]

    def _emit(self, stage: str, payload: dict[str, Any]) -> None:
        if self.on_stage:
            self.on_stage(stage, payload)

    # ------------------------------------------------------------------ 主流程
    def run_until_event(
        self,
        max_ticks: int,
        min_severity: Severity = Severity.WARNING,
        watch_safety: bool = True,
    ) -> AnomalyEvent | None:
        """推進孿生體直到偵測到夠嚴重的異常。

        兩個偵測器並行：Monitoring Agent 看感測器，Safety Agent 看影像與環境。
        工安事件優先 —— 人員在運轉中的危險區裡，不需要等設備先壞。
        """
        for _ in range(max_ticks):
            snapshot = self.twin.step()
            self._emit("tick", {"snapshot": snapshot.to_dict()})

            if watch_safety:
                hazard = self.safety.detect_hazard_event(snapshot)
                if hazard is not None:
                    self._emit("detect", {"event": hazard.to_dict()})
                    return hazard

            events = self.monitoring.detect(snapshot, self.twin.topo)
            significant = [e for e in events if e.severity.rank >= min_severity.rank]
            if significant:
                event = max(significant, key=lambda e: e.severity.rank)
                self._emit("detect", {"event": event.to_dict()})
                return event
        return None

    def confirm_diagnosis(self, event: AnomalyEvent) -> tuple[Diagnosis, int]:
        """證據不足時先繼續觀察，不要急著動設備。

        早期偵測的代價是訊號還很微弱：剛觸發 WARNING 時，指紋比對可能只有 ~0.5 的信心度，
        這時候就去停機或轉單是不負責任的。所以這裡會持續累積證據並重新診斷，
        直到信心度達標或等待上限用完（用完就帶著當下的信心度往下走，並記錄在稽核軌跡）。

        注意這不會影響 Detection Latency —— 偵測早就發生了；
        它影響的是 Mean Time To Diagnose，那正是規格 §10 要量的東西。
        """
        machine_id = event.machine_id
        snapshot = self.twin.snapshot()
        smoothed = self.monitoring.smoothed_readings(snapshot.machines[machine_id])
        diagnosis = self.diagnosis.diagnose(event, snapshot, self.twin.topo, smoothed)
        waited = 0
        while (
            diagnosis.top is not None
            and diagnosis.top.confidence < self.min_confidence
            and waited < self.max_confirm_ticks
        ):
            waited += 1
            snapshot = self.twin.step()
            self._emit("tick", {"snapshot": snapshot.to_dict()})
            self.monitoring.detect(snapshot, self.twin.topo)
            smoothed = self.monitoring.smoothed_readings(snapshot.machines[machine_id])
            diagnosis = self.diagnosis.diagnose(event, snapshot, self.twin.topo, smoothed)
            if self.audit:
                self.audit.log(
                    "observe", "orchestrator",
                    waited_ticks=waited,
                    machine_id=machine_id,
                    confidence=round(diagnosis.top.confidence, 3) if diagnosis.top else 0.0,
                    threshold=self.min_confidence,
                    reason="診斷信心度未達門檻，持續累積證據，暫不執行任何設備動作。",
                )
        self._emit("confirm", {"waited_ticks": waited, "diagnosis": diagnosis.to_dict()})
        return diagnosis, waited

    def handle_event(self, event: AnomalyEvent) -> LoopResult:
        """對一個異常事件跑完整閉環，驗證失敗會**從當下狀態重新規劃**再試一次。

        重試不能沿用上一輪的方案清單：第一個方案已經改變了孿生體的狀態，
        那份清單裡的投影都過期了。所以每一次嘗試都重跑
        Diagnose → Impact → Plan → Safety → Rank，這才是真正的閉環重試。
        """
        result = LoopResult(triggered=True, event=event, detection_tick=event.tick)
        machine_id = event.machine_id
        result.kpi_before = self.twin.kpi()
        tried: set[str] = set()

        for attempt_idx in range(1, self.max_attempts + 1):
            first_pass = attempt_idx == 1

            # --- Diagnose（第一輪會先確認證據充分）-------------------------------------
            if first_pass:
                diagnosis, waited = self.confirm_diagnosis(event)
                result.confirmation_ticks = waited
            else:
                snapshot = self.twin.snapshot()
                smoothed = self.monitoring.smoothed_readings(snapshot.machines[machine_id])
                diagnosis = self.diagnosis.diagnose(event, snapshot, self.twin.topo, smoothed)
            snapshot = self.twin.snapshot()

            # --- Impact -------------------------------------------------------------
            impact = self.production.assess_impact(machine_id, snapshot, self.twin)

            # --- 現場工安狀態（與方案無關的事實）--------------------------------------
            self.safety.perceive(snapshot)
            hazards = [f.to_dict() for f in self.safety.standing_hazards(snapshot, machine_id)]

            # --- Plan ---------------------------------------------------------------
            plans = self.production.build_plans(machine_id, snapshot, self.twin, diagnosis)

            # --- Safety -------------------------------------------------------------
            for plan in plans:
                self.safety.review_plan(plan, snapshot, machine_id)

            # --- 排名（純計算，無 LLM）------------------------------------------------
            ranking = rank_plans(plans)
            explanation = explain_ranking(ranking)
            if self.audit:
                self.audit.log("rank", "optimizer", attempt=attempt_idx, **ranking.to_dict(),
                               note="加權多準則決策；Safety BLOCK 為硬限制。LLM 未參與排名。")

            if first_pass:
                result.diagnosis = diagnosis
                result.impact = impact
                result.plans = plans
                result.ranking = ranking
                result.ranking_explanation = explanation
                result.standing_hazards = hazards
                self._emit("diagnose", {"diagnosis": diagnosis.to_dict()})
                self._emit("impact", {"impact": impact.to_dict()})
                self._emit("plan", {"plans": [p.to_dict() for p in plans]})
                self._emit("safety", {"reviews": [p.safety.to_dict() for p in plans if p.safety]})
                self._emit("rank", {"ranking": ranking.to_dict(), "explanation": explanation})

                # 工單只開一次（低風險動作，依政策可自動）
                max_delay = max((i.delay_min for i in impact.affected_orders), default=0.0)
                result.work_order = self.maintenance.create_work_order(event, diagnosis, snapshot, max_delay)
                self._emit("work_order", {"work_order": result.work_order.to_dict()})
            else:
                self._emit("replan", {"attempt": attempt_idx, "ranking": ranking.to_dict(), "explanation": explanation})

            # 「什麼都不做」的反事實基準，供 Verification 判斷介入是否真的有價值
            do_nothing = next((p for p in plans if p.plan_id == "PLAN-A"), None)

            candidates = [p for p in ranking.ranked if p.feasible and p.plan_id not in tried]
            if not candidates:
                result.escalated = True
                result.escalation_reason = (
                    "所有方案都被 Safety Agent 阻擋，需人工重新規劃。"
                    if not tried
                    else "已無其他可行方案可嘗試，交由人工處置。"
                )
                if self.audit:
                    self.audit.log("escalate", "orchestrator", attempt=attempt_idx, reason=result.escalation_reason,
                                   blocked_plans=[p.plan_id for p in ranking.ranked if not p.feasible])
                self._emit("escalate", {"reason": result.escalation_reason})
                break

            plan = candidates[0]
            tried.add(plan.plan_id)
            attempt = self._attempt(attempt_idx, plan, machine_id, result.work_order, result.kpi_before, do_nothing)
            result.attempts.append(attempt)
            if attempt.verification and attempt.verification.passed:
                break
            if self.audit:
                self.audit.log(
                    "retry", "orchestrator", attempt=attempt_idx, plan_id=plan.plan_id,
                    reason=attempt.blocked_reason or "驗證未通過，從當前狀態重新規劃。",
                )

        result.kpi_after = self.twin.kpi()
        last = result.attempts[-1] if result.attempts else None
        if last and (not last.executed or (last.verification and not last.verification.passed)):
            result.escalated = True
            result.escalation_reason = last.blocked_reason or "所有嘗試的方案驗證均未通過，已交由人工處置。"
            if self.audit:
                self.audit.log("escalate", "orchestrator", reason=result.escalation_reason)
            self._emit("escalate", {"reason": result.escalation_reason})
        return result

    # ------------------------------------------------------------------ 單次嘗試
    def _attempt(
        self,
        attempt_idx: int,
        plan: RecoveryPlan,
        machine_id: str,
        work_order: WorkOrder | None,
        kpi_before: dict[str, float],
        do_nothing: RecoveryPlan | None = None,
    ) -> LoopAttempt:
        policy_decision = self.ctx.policy.evaluate_actions([a.kind for a in plan.actions])
        if self.audit:
            self.audit.log("policy", "policy-engine", plan_id=plan.plan_id, **policy_decision.to_dict())
        self._emit("policy", {"plan_id": plan.plan_id, "decision": policy_decision.to_dict()})

        # --- Approve ---------------------------------------------------------------
        if not policy_decision.allowed:
            approval = ApprovalDecision(plan.plan_id, False, "policy-engine", "動作被 Policy 禁止", auto=True)
        elif policy_decision.requires_approval:
            approval = self.approval(plan, policy_decision)
        else:
            approval = ApprovalDecision(plan.plan_id, True, "policy-engine", "低風險動作，依政策自動放行", auto=True)
        if self.audit:
            self.audit.log("approval", approval.approver, attempt=attempt_idx, **approval.to_dict())
        self._emit("approve", {"approval": approval.to_dict()})

        if not approval.approved:
            return LoopAttempt(attempt_idx, plan, policy_decision, approval, executed=False,
                               blocked_reason=f"未取得核准：{approval.reason}")

        # --- 執行前最後一道安全閘門 ---------------------------------------------------
        snapshot = self.twin.snapshot()
        allowed, reason = self.safety.gate_execution(plan.actions, snapshot, machine_id)
        if not allowed:
            return LoopAttempt(attempt_idx, plan, policy_decision, approval, executed=False,
                               blocked_reason=f"執行前安全檢查未通過：{reason}")

        # --- Execute（真的改變孿生體狀態）---------------------------------------------
        effects: list[dict[str, Any]] = []
        for action in plan.actions:
            params = dict(action.params)
            if action.kind is ActionKind.CREATE_WORK_ORDER and work_order:
                params["work_order_id"] = work_order.work_order_id
            effect = self.twin.apply(Action(action.kind, action.target, params, action.rationale))
            effects.append(effect.to_dict())
            if self.audit:
                self.audit.log("execute", "orchestrator", attempt=attempt_idx, plan_id=plan.plan_id,
                               action=action.describe(), **effect.to_dict())
        if work_order:
            work_order.status = "dispatched"
        self._emit("execute", {"plan_id": plan.plan_id, "effects": effects})

        # --- Verify（在真實孿生體上）-------------------------------------------------
        report = self.verification.verify(
            plan, kpi_before, self.twin, machine_id, settle_ticks=self.settle_ticks, do_nothing=do_nothing
        )
        self._emit("verify", {"report": report.to_dict()})
        return LoopAttempt(attempt_idx, plan, policy_decision, approval, executed=True,
                           execution_effects=effects, verification=report)

    # ------------------------------------------------------------------ KPI
    def agent_metrics(self) -> list[dict[str, Any]]:
        return [a.metrics() for a in self.agents]


__all__ = ["Orchestrator", "LoopResult", "LoopAttempt", "auto_approve", "ApprovalCallback", "StageCallback"]
