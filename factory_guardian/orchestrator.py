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
from .agents.diagnosis import NO_FAULT_ID, DiagnosisAgent
from .agents.maintenance import MaintenanceAgent
from .agents.monitoring import WINDOW, MonitoringAgent
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
from .optimizer import RankingResult, explain_ranking, explain_robustness, rank_plans
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
    # 這一輪用的診斷，以及它在孿生體上的起訖時間。
    # 重試會重新診斷，所以「初次診斷」和「最終診斷」可能不是同一個答案 ——
    # 那個差別正是重規劃的價值，沒有這兩個欄位就量不出來。
    diagnosis: Diagnosis | None = None
    start_tick: int = 0
    end_tick: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "diagnosis_top": self.diagnosis.top.fault_id if (self.diagnosis and self.diagnosis.top) else None,
            "start_tick": self.start_tick,
            "end_tick": self.end_tick,
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
    confidence_threshold: float = 0.0   # 動設備前要求的最低診斷信心度
    diagnosis: Diagnosis | None = None
    # 偵測當下的第一次比對（還沒累積證據）。和 `diagnosis`（確認之後）分開放，
    # 因為「多看幾分鐘把答案改對了」是一個要被量出來的結果，不是一句話。
    initial_diagnosis: Diagnosis | None = None
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
    # --- 自我節制（False Positive）---
    # 告警成立、但證據不支持動設備時，閉環會停在這裡：只告警、持續監控，不改變任何設備狀態。
    abstained: bool = False
    abstain_reason: str = ""
    observation_ticks: int = 0                       # 動設備前實際觀察了幾個 tick
    degradation: dict[str, Any] = field(default_factory=dict)   # 進行式判定的依據

    @property
    def first_diagnosis(self) -> Diagnosis | None:
        """偵測當下的第一次比對；沒有記到就退回確認後的診斷。"""
        return self.initial_diagnosis or self.diagnosis

    @property
    def final_diagnosis(self) -> Diagnosis | None:
        """最後一次嘗試所依據的診斷（沒有重試時就等於初次診斷）。"""
        for attempt in reversed(self.attempts):
            if attempt.diagnosis is not None:
                return attempt.diagnosis
        return self.diagnosis

    @property
    def replans(self) -> int:
        """重新規劃的次數 = 嘗試次數 − 1。第一次不算重規劃。"""
        return max(0, len(self.attempts) - 1)

    @property
    def recovery_after_replan_min(self) -> float | None:
        """從「驗證失敗」到「驗證通過」花了多少模擬分鐘。

        量的是閉環自我修正的速度：驗證抓到問題之後，系統要多久才把事情做對。
        沒有失敗過（或失敗後再也沒通過）就是 None —— 不要用 0 假裝它很快。
        """
        failed_at: int | None = None
        for attempt in self.attempts:
            if attempt.verification is None:
                continue
            if not attempt.verification.passed and failed_at is None:
                failed_at = attempt.end_tick
            elif attempt.verification.passed and failed_at is not None:
                return float(attempt.end_tick - failed_at)
        return None

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
            "confidence_threshold": self.confidence_threshold,
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
            "abstained": self.abstained,
            "abstain_reason": self.abstain_reason,
            "observation_ticks": self.observation_ticks,
            "degradation": self.degradation,
            "replans": self.replans,
            "first_diagnosis_top": (
                self.first_diagnosis.top.fault_id
                if (self.first_diagnosis and self.first_diagnosis.top) else None
            ),
            "confirmed_diagnosis_top": (
                self.diagnosis.top.fault_id if (self.diagnosis and self.diagnosis.top) else None
            ),
            "final_diagnosis_top": (
                self.final_diagnosis.top.fault_id
                if (self.final_diagnosis and self.final_diagnosis.top) else None
            ),
            "recovery_after_replan_min": self.recovery_after_replan_min,
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
        # 動設備之前至少要觀察幾個 tick。預設等於監測視窗長度（WINDOW = 6），
        # 理由見 MonitoringAgent.degradation()：視窗裡只要還留著階躍的邊緣，
        # 「跳到一個新穩態」和「才剛開始的劣化」在數學上就是同一條線。
        observe_ticks: int = WINDOW,
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
        self.observe_ticks = max(0, min(observe_ticks, max_confirm_ticks))

        self.monitoring = MonitoringAgent(self.ctx, mode=monitoring_mode)
        self.diagnosis = DiagnosisAgent(self.ctx)
        self.production = ProductionAgent(self.ctx)
        # Safety Agent 拿 Monitoring Agent 只是為了引用 TTT（到達危險門檻的剩餘時間）
        # 當**證據**——裁決邏輯完全沒變，見 policy/engine.py 的 _ttt_evidence。
        self.safety = SafetyAgent(self.ctx, monitoring=self.monitoring)
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

            # 先讓 Monitoring Agent 觀測（更新滑動視窗），工安事件仍然優先回傳。
            # 順序這樣排是為了讓下面的 _hazard_confirmed 有視窗可用；
            # 兩者算的是同一個 snapshot，誰先算不影響結果，只影響誰先被回傳。
            events = self.monitoring.detect(snapshot, self.twin.topo)

            if watch_safety:
                hazard = self.safety.detect_hazard_event(snapshot)
                if hazard is not None and self._hazard_confirmed(hazard):
                    self._emit("detect", {"event": hazard.to_dict()})
                    return hazard

            significant = [e for e in events if e.severity.rank >= min_severity.rank]
            if significant:
                event = max(significant, key=lambda e: e.severity.rank)
                self._emit("detect", {"event": event.to_dict()})
                return event
        return None

    def _hazard_confirmed(self, hazard: AnomalyEvent) -> bool:
        """工安事件要不要當場採信？

        分兩種：

        * **人員類**（SR-01 闖入 / SR-04 煙霧 / SR-05 跌倒 / SR-08 護具）——
          影像直接看到的事實，一格畫面就該停，不做任何確認。
        * **設備讀值類**（SR-02 振動、SR-03 高溫）—— 這些和 Monitoring Agent 讀的是
          **同一支感測器**。同一個讀值不該因為換一個 Agent 去讀就跳過確認：
          一次接點抖動讓振動跳到 9 mm/s，那不是「危險工況」，那是壞掉的訊號。
          所以要求它同時通過監測端的 N-of-M 門檻確認（見 MonitoringAgent.confirmed_bands）。

        這條規則不會放寬任何工安裁決 —— `review_plan()` 與 `gate_execution()`
        完全沒有被動到。它只決定「要不要因為這一格讀值就把整個閉環叫起來」。
        """
        personnel = {"SR-01", "SR-04", "SR-05", "SR-08"}
        rules = {t.split(":")[-1] for t in hazard.triggers}
        if rules & personnel:
            return True
        snapshot = self.twin.snapshot()
        machine = snapshot.machines.get(hazard.machine_id)
        if machine is None:
            return True
        specs = self.twin.topo.machines[hazard.machine_id].signals
        critical, _ = self.monitoring.confirmed_bands(hazard.machine_id, machine, specs)
        if critical:
            return True
        if self.audit:
            self.audit.log(
                "hazard_unconfirmed", "orchestrator", machine_id=hazard.machine_id,
                rules=sorted(rules), tick=hazard.tick,
                reason="設備讀值類工安規則未通過門檻確認（單一取樣越界），不觸發閉環，持續監控。",
            )
        return False

    def confirm_diagnosis(
        self, event: AnomalyEvent, require_observation: bool = True
    ) -> tuple[Diagnosis, int, dict[str, Any], Diagnosis]:
        """動設備之前先把兩件事看清楚：證據夠不夠，以及它是不是還在惡化。

        **證據夠不夠**：早期偵測的代價是訊號還很微弱 —— 剛觸發 WARNING 時，
        指紋比對可能只有 ~0.5 的信心度，這時候就去停機或轉單是不負責任的。

        **是不是還在惡化**：這一條是為了誤報。換規格提高負載、冷機暖機過衝、換料後重啟，
        都會讓訊號跳到一個新的位置然後停在那裡；訊號的**位置**和早期劣化分不開，
        分得開的是它**還動不動**。而要回答這件事，就必須看滿一個完整的監測視窗
        （``observe_ticks``，預設 = WINDOW）：視窗裡只要還留著那個階躍的邊緣，
        「新穩態」和「才剛開始的斜坡」就是同一條線。

        這幾分鐘不是保守，是資訊還不存在。它的代價會誠實地出現在
        Mean Time To Diagnose 與產能達成率上（見 docs/factory_guardian/benchmark_notes.md），
        換到的是干擾情境下的零誤動作 —— 這個交換划不划算，數字攤在那裡讓人自己判斷。

        注意兩者都不影響 Detection Latency —— 偵測早就發生了。
        """
        machine_id = event.machine_id
        snapshot = self.twin.snapshot()
        specs = self.twin.topo.machines[machine_id].signals
        smoothed = self.monitoring.smoothed_readings(snapshot.machines[machine_id])
        diagnosis = self.diagnosis.diagnose(event, snapshot, self.twin.topo, smoothed)
        # 偵測當下的那一次比對要留下來：它和「確認之後」可能不是同一個答案，
        # 而那個差距正是這段觀察窗買到的東西。沒有這個欄位，觀察窗的價值就只能用講的。
        initial = diagnosis
        waited = 0
        while waited < self.max_confirm_ticks:
            low_confidence = diagnosis.top is not None and diagnosis.top.confidence < self.min_confidence
            needs_observation = require_observation and waited < self.observe_ticks
            if not (low_confidence or needs_observation):
                break
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
                    reason=(
                        "診斷信心度未達門檻，持續累積證據，暫不執行任何設備動作。"
                        if low_confidence
                        else "尚未觀察滿一個完整監測視窗，無法分辨『新穩態』與『進行式劣化』，暫不動設備。"
                    ),
                )
        degradation = dict(self.monitoring.degradation(self.twin.snapshot().machines[machine_id], specs))
        self._emit(
            "confirm",
            {
                "waited_ticks": waited,
                "max_confirm_ticks": self.max_confirm_ticks,
                "min_confidence": self.min_confidence,
                "observe_ticks": self.observe_ticks,
                "degradation": degradation,
                "initial_top": initial.top.fault_id if initial.top else None,
                "diagnosis": diagnosis.to_dict(),
            },
        )
        return diagnosis, waited, degradation, initial

    def handle_event(self, event: AnomalyEvent) -> LoopResult:
        """對一個異常事件跑完整閉環，驗證失敗會**從當下狀態重新規劃**再試一次。

        重試不能沿用上一輪的方案清單：第一個方案已經改變了孿生體的狀態，
        那份清單裡的投影都過期了。所以每一次嘗試都重跑
        Diagnose → Impact → Plan → Safety → Rank，這才是真正的閉環重試。
        """
        result = LoopResult(
            triggered=True, event=event, detection_tick=event.tick, confidence_threshold=self.min_confidence
        )
        machine_id = event.machine_id
        result.kpi_before = self.twin.kpi()
        tried: set[str] = set()

        for attempt_idx in range(1, self.max_attempts + 1):
            first_pass = attempt_idx == 1

            # --- Diagnose（第一輪會先確認證據充分）-------------------------------------
            if first_pass:
                # 工安事件不做「進行式」確認：人已經站在運轉中的機台旁邊，
                # 那不是一個需要多看幾分鐘才確定的趨勢。
                diagnosis, waited, degradation, initial = self.confirm_diagnosis(
                    event, require_observation=event.kind != "safety"
                )
                result.initial_diagnosis = initial
                result.confirmation_ticks = waited
                result.observation_ticks = waited
                result.degradation = degradation
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

            # --- 動設備之前的自我節制 -------------------------------------------------
            # 兩種情況下不該碰設備：診斷本身說「沒有設備故障徵兆」，
            # 或者異常已經停在一個新的穩態（不是進行式劣化）。
            # 兩者都只在**沒有現場工安風險**時成立 —— 人在危險區裡的時候，
            # 「再看看」不是一個選項。
            if first_pass and not hazards:
                abstain_reason = self._abstain_reason(diagnosis, result.degradation)
                if abstain_reason:
                    result.abstained = True
                    result.abstain_reason = abstain_reason
                    result.diagnosis = diagnosis
                    result.impact = impact
                    result.standing_hazards = hazards
                    self._emit("diagnose", {"diagnosis": diagnosis.to_dict()})
                    self._emit("impact", {"impact": impact.to_dict()})
                    # 棄權不等於不理會：告警照發、監控照跑，只是不改變任何設備狀態。
                    effect = self.twin.apply(Action(
                        ActionKind.RAISE_ALERT, machine_id, {"level": event.severity.value},
                        "證據不支持設備處置，僅告警並持續監控。",
                    ))
                    if self.audit:
                        self.audit.log(
                            "abstain", "orchestrator", machine_id=machine_id,
                            reason=abstain_reason, degradation=result.degradation,
                            top=diagnosis.top.fault_id if diagnosis.top else None,
                            confidence=round(diagnosis.top.confidence, 3) if diagnosis.top else 0.0,
                            effect=effect.to_dict(),
                            note="False Positive 的自我節制：發出告警但不執行任何設備動作。",
                        )
                    self._emit("abstain", {"reason": abstain_reason, "degradation": result.degradation})
                    break

            # --- Plan ---------------------------------------------------------------
            plans = self.production.build_plans(machine_id, snapshot, self.twin, diagnosis)

            # --- Safety -------------------------------------------------------------
            for plan in plans:
                self.safety.review_plan(plan, snapshot, machine_id)

            # --- 排名（純計算，無 LLM）------------------------------------------------
            ranking = rank_plans(plans)
            explanation = explain_ranking(ranking)
            robustness_line = explain_robustness(ranking.robustness)
            if self.audit:
                self.audit.log("rank", "optimizer", attempt=attempt_idx, **ranking.to_dict(),
                               robustness_summary=robustness_line,
                               note="加權多準則決策；Safety BLOCK 為硬限制。LLM 未參與排名。"
                                    "權重穩健性由 optimizer.robustness_scan 掃描，固定種子可重現。")

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
            attempt = self._attempt(
                attempt_idx, plan, machine_id, result.work_order, result.kpi_before, do_nothing,
                diagnosis=diagnosis,
            )
            result.attempts.append(attempt)
            if attempt.verification and attempt.verification.passed:
                break
            if self.audit:
                self.audit.log(
                    "retry", "orchestrator", attempt=attempt_idx, plan_id=plan.plan_id,
                    reason=attempt.blocked_reason or "驗證未通過，從當前狀態重新規劃。",
                )

        result.kpi_after = self.twin.kpi()
        if result.abstained:
            return result
        last = result.attempts[-1] if result.attempts else None
        if last and (not last.executed or (last.verification and not last.verification.passed)):
            result.escalated = True
            result.escalation_reason = last.blocked_reason or "所有嘗試的方案驗證均未通過，已交由人工處置。"
            if self.audit:
                self.audit.log("escalate", "orchestrator", reason=result.escalation_reason)
            self._emit("escalate", {"reason": result.escalation_reason})
        return result

    # ------------------------------------------------------------------ 自我節制
    def _abstain_reason(self, diagnosis: Diagnosis, degradation: dict[str, Any]) -> str:
        """要不要放棄這次處置？回傳理由字串（空字串 = 繼續處置）。

        兩個判準都來自 Agent 看得到的東西，沒有一個字讀到 Ground Truth：

        1. **診斷自己說沒有設備故障徵兆**（`no_equipment_fault`）——
           偏離量還在正常區間內，硬從三個故障裡挑一個最像的才是不誠實。
        2. **異常已經停在一個新的穩態**（`degradation.progressive` 為假）——
           設備劣化是進行式的；不再惡化的訊號比較可能是製程或負載改變。

        停機、降速、轉單都是有代價的動作。它們值得一個「這件事還在惡化」的前提。
        """
        top = diagnosis.top
        if top is not None and top.fault_id == NO_FAULT_ID:
            return (
                f"診斷判定無設備故障徵兆（信心度 {top.confidence:.0%}），"
                "本次僅告警並持續監控，不執行任何設備動作。"
            )
        if degradation and not degradation.get("progressive", True):
            return (
                f"觀察滿 {degradation.get('samples', 0)} 個取樣後，健康度僅變化 "
                f"{degradation.get('health_drop', 0.0):+.1f} 分、最壞趨勢 "
                f"{degradation.get('trend', 0.0):+.3f}/tick —— 訊號停在新的穩態而非持續惡化，"
                "比較可能是製程或負載改變。僅告警並持續監控，不動設備。"
            )
        return ""

    # ------------------------------------------------------------------ 單次嘗試
    def _attempt(
        self,
        attempt_idx: int,
        plan: RecoveryPlan,
        machine_id: str,
        work_order: WorkOrder | None,
        kpi_before: dict[str, float],
        do_nothing: RecoveryPlan | None = None,
        diagnosis: Diagnosis | None = None,
    ) -> LoopAttempt:
        start_tick = self.twin.tick
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
                               blocked_reason=f"未取得核准：{approval.reason}",
                               diagnosis=diagnosis, start_tick=start_tick, end_tick=self.twin.tick)

        # --- 執行前最後一道安全閘門 ---------------------------------------------------
        snapshot = self.twin.snapshot()
        allowed, reason = self.safety.gate_execution(plan.actions, snapshot, machine_id)
        if not allowed:
            return LoopAttempt(attempt_idx, plan, policy_decision, approval, executed=False,
                               blocked_reason=f"執行前安全檢查未通過：{reason}",
                               diagnosis=diagnosis, start_tick=start_tick, end_tick=self.twin.tick)

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
                           execution_effects=effects, verification=report,
                           diagnosis=diagnosis, start_tick=start_tick, end_tick=self.twin.tick)

    # ------------------------------------------------------------------ KPI
    def agent_metrics(self) -> list[dict[str, Any]]:
        return [a.metrics() for a in self.agents]


__all__ = ["Orchestrator", "LoopResult", "LoopAttempt", "auto_approve", "ApprovalCallback", "StageCallback"]
