"""Industrial Safety Guardian（規格 §4.4）。

職責：VLM / CV（PPE、Hazard Zone、跌倒）＋環境感測（高溫、煙霧）＋ Safety Policy，
在高風險條件下阻止「為了產量繼續運轉」的方案。

這個 Agent 是**政策閘門**，不是建議者：它的 BLOCK 是硬限制，
Optimizer 會直接把該方案標成不可行，LLM 也無權推翻。
"""

from __future__ import annotations

from ..domain import (
    Action,
    ActionKind,
    AnomalyEvent,
    CameraObservation,
    Evidence,
    FactorySnapshot,
    MachineState,
    RecoveryPlan,
    SafetyFinding,
    SafetyReview,
    SafetyVerdictKind,
    Severity,
)
from ..policy.engine import SafetyContext
from .base import Agent
from .vision import VisionBackend, SimulatedVLM

# 會讓目標機台停止產出的動作；沒有這些動作代表方案讓機台繼續跑。
_STOPPERS = {ActionKind.STOP_MACHINE, ActionKind.START_MAINTENANCE}


class SafetyAgent(Agent):
    name = "safety-agent"
    role = "風險與政策檢查"

    def __init__(self, ctx=None, vision: VisionBackend | None = None) -> None:
        super().__init__(ctx)
        self.vision = vision or SimulatedVLM()
        self.hazard_seq = 0

    # ------------------------------------------------------------------ 視覺與環境
    def perceive(self, snapshot: FactorySnapshot) -> list[CameraObservation]:
        """把 Camera 影像交給 VLM 後端解讀。MVP 用模擬後端，介面可換成真實 VLM。"""
        with self.tool("vlm.analyze", f"cameras={len(snapshot.cameras)}"):
            observations = self.vision.analyze(snapshot)
        for obs in observations:
            if obs.person_in_hazard_zone or not obs.ppe_compliant or obs.smoke_detected or obs.fall_detected:
                self.log(
                    "vision_alert",
                    camera_id=obs.camera_id,
                    zone_id=obs.zone_id,
                    caption=obs.caption,
                    person_in_hazard_zone=obs.person_in_hazard_zone,
                    ppe_compliant=obs.ppe_compliant,
                    smoke_detected=obs.smoke_detected,
                    confidence=obs.confidence,
                )
        return observations

    def detect_hazard_event(self, snapshot: FactorySnapshot) -> AnomalyEvent | None:
        """工安事件也要能觸發閉環。

        Monitoring Agent 只看感測器；人員闖入危險區這種事故不會讓溫度或振動動一下，
        但它是最需要立刻反應的事件之一。所以 Safety Agent 自己也是一個偵測器。
        """
        self.perceive(snapshot)
        for machine_id, machine in snapshot.machines.items():
            if machine.state not in (MachineState.RUNNING, MachineState.DERATED):
                continue
            findings = self.standing_hazards(snapshot, machine_id)
            blocking = [f for f in findings if f.verdict is SafetyVerdictKind.BLOCK]
            if not blocking:
                continue
            self.hazard_seq += 1
            event = AnomalyEvent(
                event_id=f"SAF-{self.hazard_seq:03d}",
                machine_id=machine_id,
                tick=snapshot.tick,
                sim_minutes=snapshot.sim_minutes,
                severity=Severity.CRITICAL,
                triggers=[f"safety:{f.rule_id}" for f in blocking],
                health=machine.health,
                readings=dict(machine.readings),
                detector=self.name,
                kind="safety",
            )
            self.log(
                "detect", event_id=event.event_id, machine_id=machine_id, tick=snapshot.tick,
                severity=event.severity.value, triggers=event.triggers, kind="safety",
                messages=[f.message for f in blocking],
            )
            return event
        return None

    def standing_hazards(self, snapshot: FactorySnapshot, machine_id: str) -> list[SafetyFinding]:
        """與方案無關的現場風險（不論選哪個方案都成立的事實）。"""
        ctx = SafetyContext(
            snapshot=snapshot,
            machine_id=machine_id,
            action_kinds=set(),
            keeps_machine_running=snapshot.machines[machine_id].state
            in (MachineState.RUNNING, MachineState.DERATED),
            keeps_full_speed=snapshot.machines[machine_id].state is MachineState.RUNNING,
        )
        return self.ctx.policy.evaluate_safety(ctx)

    # ------------------------------------------------------------------ 方案審查
    def review_plan(self, plan: RecoveryPlan, snapshot: FactorySnapshot, machine_id: str) -> SafetyReview:
        kinds = {a.kind for a in plan.actions}
        keeps_running = not (kinds & _STOPPERS)
        keeps_full_speed = keeps_running and ActionKind.DERATE_MACHINE not in kinds

        # 轉單方案：目標機台雖然停了，但接手的機台開始承擔全負載 —— 也要檢查它。
        with self.timed():
            ctx = SafetyContext(
                snapshot=snapshot,
                machine_id=machine_id,
                action_kinds=kinds,
                keeps_machine_running=keeps_running,
                keeps_full_speed=keeps_full_speed,
                plan_id=plan.plan_id,
                projection=plan.projection,
            )
            with self.tool("policy.evaluate_safety", f"plan={plan.plan_id}"):
                findings = self.ctx.policy.evaluate_safety(ctx)

            for action in plan.actions:
                if action.kind is ActionKind.TRANSFER_ORDER:
                    target = action.params.get("to_machine")
                    if target and target in snapshot.machines:
                        findings.extend(self._review_receiving_machine(snapshot, target))

            verdict = self.ctx.policy.combine(findings)

        required_sop = sorted({e.reference.split("#")[0] for f in findings for e in f.evidence if e.source in ("sop", "policy")})
        review = SafetyReview(plan_id=plan.plan_id, verdict=verdict, findings=findings, required_sop=required_sop)
        plan.safety = review
        self.log(
            "safety",
            plan_id=plan.plan_id,
            machine_id=machine_id,
            verdict=verdict.value,
            keeps_machine_running=keeps_running,
            keeps_full_speed=keeps_full_speed,
            findings=[f.to_dict() for f in findings],
            required_sop=required_sop,
        )
        return review

    def _review_receiving_machine(self, snapshot: FactorySnapshot, machine_id: str) -> list[SafetyFinding]:
        """接手訂單的機台自己也不能是不安全的。"""
        machine = snapshot.machines[machine_id]
        findings: list[SafetyFinding] = []
        vib = machine.value("vibration")
        temp = machine.value("temperature")
        if vib is not None and vib > 7.0:
            findings.append(SafetyFinding(
                rule_id="SR-10",
                verdict=SafetyVerdictKind.BLOCK,
                message=f"接手機台 {machine_id} 振動 {vib:.2f} mm/s 已超過危險門檻，不可承接滿載訂單。",
                evidence=[Evidence("sensor", f"{machine_id}.vibration", f"vibration={vib:.2f} mm/s")],
            ))
        elif vib is not None and vib > 4.0:
            findings.append(SafetyFinding(
                rule_id="SR-11",
                verdict=SafetyVerdictKind.APPROVAL_REQUIRED,
                message=f"接手機台 {machine_id} 振動 {vib:.2f} mm/s 已進入警告區間，滿載運轉需生產主管確認。",
                evidence=[Evidence("sensor", f"{machine_id}.vibration", f"vibration={vib:.2f} mm/s")],
            ))
        if temp is not None and temp > 80.0:
            findings.append(SafetyFinding(
                rule_id="SR-12",
                verdict=SafetyVerdictKind.BLOCK,
                message=f"接手機台 {machine_id} 溫度 {temp:.1f}°C 已超過危險門檻，不可承接滿載訂單。",
                evidence=[Evidence("sensor", f"{machine_id}.temperature", f"temperature={temp:.1f}°C")],
            ))
        return findings

    # ------------------------------------------------------------------ 執行前最終檢查
    def gate_execution(self, actions: list[Action], snapshot: FactorySnapshot, machine_id: str) -> tuple[bool, str]:
        """執行前的最後一道閘門：情況可能在核准後又變糟了。"""
        kinds = {a.kind for a in actions}
        if ActionKind.SAFETY_OVERRIDE in kinds:
            return False, "方案包含 Safety Override，系統禁止執行。"
        keeps_running = not (kinds & _STOPPERS)
        ctx = SafetyContext(
            snapshot=snapshot,
            machine_id=machine_id,
            action_kinds=kinds,
            keeps_machine_running=keeps_running,
            keeps_full_speed=keeps_running and ActionKind.DERATE_MACHINE not in kinds,
        )
        findings = self.ctx.policy.evaluate_safety(ctx)
        blocking = [f for f in findings if f.verdict is SafetyVerdictKind.BLOCK]
        if blocking:
            reason = "；".join(f.message for f in blocking)
            self.log("safety_gate", machine_id=machine_id, allowed=False, reason=reason)
            return False, reason
        self.log("safety_gate", machine_id=machine_id, allowed=True, reason="通過執行前安全檢查")
        return True, "通過執行前安全檢查"


__all__ = ["SafetyAgent"]
