"""Orchestrator —— Observe → Plan → Simulate → Approve → Execute → Verify 閉環。

它是唯一有權變更網路狀態的元件，而且只能執行「通過政策、且（必要時）經人工核准」
的計畫。每一步都寫進雜湊鏈稽核軌跡；驗證失敗會自動重跑閉環。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .agents import (
    ImpactAgent,
    PlanningAgent,
    PolicyAgent,
    TelemetryAgent,
    VerificationAgent,
)
from .agents.base import Agent, AgentResult
from .audit import AuditLog
from .config import SETTINGS, Settings
from .domain import NetworkSnapshot, RecoveryPlan, Scenario
from .llm import LLMClient
from .policy.engine import ALLOW, DENY
from .twin.engine import DigitalTwin

ApprovalFn = Callable[[RecoveryPlan], bool]


def auto_approve(_: RecoveryPlan) -> bool:
    """無人值守模式。真實部署絕不該用它 —— 這裡只服務自動化測試。"""
    return True


def auto_reject(_: RecoveryPlan) -> bool:
    return False


@dataclass
class LoopResult:
    scenario: Scenario
    baseline: NetworkSnapshot
    incident: NetworkSnapshot
    final: NetworkSnapshot
    plans: list[RecoveryPlan]
    executed_plan: RecoveryPlan | None
    agent_results: list[AgentResult] = field(default_factory=list)
    approved: bool = False
    rounds: int = 1
    halted_reason: str | None = None
    audit_path: Path | None = None
    llm_mode: str = "offline"

    @property
    def succeeded(self) -> bool:
        return self.final.critical_availability_pct >= 100.0 - 1e-6

    def summary(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.id,
            "rounds": self.rounds,
            "approved": self.approved,
            "executed_plan": self.executed_plan.id if self.executed_plan else None,
            "halted_reason": self.halted_reason,
            "llm_mode": self.llm_mode,
            "critical_availability_pct": {
                "baseline": round(self.baseline.critical_availability_pct, 1),
                "incident": round(self.incident.critical_availability_pct, 1),
                "final": round(self.final.critical_availability_pct, 1),
            },
            "slo_compliance_pct": {
                "baseline": round(self.baseline.slo_compliance_pct, 1),
                "incident": round(self.incident.slo_compliance_pct, 1),
                "final": round(self.final.slo_compliance_pct, 1),
            },
            "monthly_cost_ntd": {
                "baseline": round(self.baseline.monthly_cost_ntd),
                "final": round(self.final.monthly_cost_ntd),
            },
            "succeeded": self.succeeded,
        }


class Orchestrator:
    def __init__(
        self,
        twin: DigitalTwin | None = None,
        settings: Settings | None = None,
        audit_log: AuditLog | None = None,
    ) -> None:
        self.settings = settings or SETTINGS
        self.twin = twin or DigitalTwin()
        self.llm = LLMClient(self.settings)

        if audit_log is None:
            # 秒級時間戳會讓同一秒內的兩次獨立事故寫進同一個檔案，
            # 查核人員會讀到一條從未發生過的連續時間線 —— 加上唯一碼避免。
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            audit_log = AuditLog(
                self.settings.audit_dir / f"audit-{stamp}-{uuid4().hex[:6]}.jsonl"
            )
        self.audit = audit_log

        self.telemetry = TelemetryAgent(self.llm)
        self.impact = ImpactAgent(self.llm)
        self.planning = PlanningAgent(self.llm)
        self.governance = PolicyAgent(self.llm)
        self.verification = VerificationAgent(self.llm)

    # ------------------------------------------------------------------ 閉環

    def run(
        self,
        scenario: Scenario,
        approval_fn: ApprovalFn | None = None,
        max_rounds: int = 2,
        on_stage: Callable[[str, Any], None] | None = None,
    ) -> LoopResult:
        emit = on_stage or (lambda *_: None)
        approve = approval_fn or auto_approve

        def announce(agent: Agent) -> None:
            """先讓 UI 知道哪個 Agent 開跑。

            每個 Agent 的敘述都要等一次 LLM 往返（實測 1.5–2.5s），而
            `emit("agent", ...)` 是在那之後才發的。中間沒有任何回饋的話，
            使用者按下「執行閉環」會先看到好幾秒的空白，畫面像當掉。
            """
            emit("agent_start", {"agent": agent.name, "stage": agent.stage})

        # ---- 事故前基準
        self.twin.clear_faults()
        self.twin.reset_routing()
        baseline = self.twin.evaluate("baseline")
        self.audit.record("baseline", "DigitalTwin", baseline.to_dict())
        emit("baseline", baseline)

        # ---- 注入故障（等同真實環境的 tc/netem 與斷纜）
        self.twin.apply_faults(scenario.faults)
        self.twin.tick += 1
        incident = self.twin.evaluate("incident")
        self.audit.record("fault_injection", "Scenario", {
            "scenario": scenario.id,
            "faults": [
                {
                    "link": f.link_id,
                    "state": f.state.value,
                    "netem": f.netem_command(
                        capacity_mbps=self.twin.links[f.link_id].capacity_mbps
                    ),
                    "description": f.description,
                }
                for f in scenario.faults
            ],
            "snapshot": incident.to_dict(),
        })
        emit("incident", incident)

        results: list[AgentResult] = []
        executed: RecoveryPlan | None = None
        approved = False
        halted: str | None = None
        all_plans: list[RecoveryPlan] = []
        final = incident
        round_no = 0

        for round_no in range(1, max_rounds + 1):
            current = self.twin.evaluate(f"round-{round_no}")

            # ---- Observe
            announce(self.telemetry)
            r = self.telemetry.run(self.twin, baseline, current)
            results.append(r); self.audit.record(r.stage, r.agent, r.to_dict()); emit("agent", r)

            # ---- Assess
            announce(self.impact)
            r = self.impact.run(self.twin, baseline, current)
            results.append(r); self.audit.record(r.stage, r.agent, r.to_dict()); emit("agent", r)

            # ---- Plan + Simulate
            announce(self.planning)
            r, plans = self.planning.run(self.twin, current)
            all_plans = plans
            results.append(r); self.audit.record(r.stage, r.agent, r.to_dict()); emit("agent", r)
            if not plans:
                halted = "最佳化器找不到任何可行復原路徑"
                break

            # ---- Govern（政策裁決，Agent 無裁量權）
            announce(self.governance)
            r, selected = self.governance.run(self.twin, plans, current)
            results.append(r); self.audit.record(r.stage, r.agent, r.to_dict())
            # 政策裁決寫回計畫後才發布，UI 拿到的表格才是完整的
            emit("plans", {"plans": plans, "selected": selected})
            emit("agent", r)
            if selected is None:
                halted = "所有候選計畫皆被治理政策拒絕"
                break

            # ---- Approve（人工閘門）
            needs_human = self.settings.require_approval and selected.policy_decision != ALLOW
            approved = approve(selected) if needs_human else True
            self.audit.record("approve", "Human" if needs_human else "PolicyEngine", {
                "plan_id": selected.id,
                "policy_decision": selected.policy_decision,
                "risk_score": round(selected.risk_score, 1),
                "findings": selected.policy_findings,
                "human_gate": needs_human,
                "approved": approved,
            })
            emit("approval", {"plan": selected, "approved": approved, "human_gate": needs_human})
            if not approved:
                halted = "人工核准遭拒，網路維持事故當下狀態"
                break

            # ---- Execute（唯一真正變更網路的一步）
            applied = self.twin.apply_plan(selected)
            self.twin.tick += 1
            executed = selected
            self.audit.record("execute", "Orchestrator", {
                "plan_id": selected.id, "applied_actions": applied,
            })
            emit("execute", {"plan": selected, "applied": applied})

            # ---- Verify
            final = self.twin.evaluate(f"verified-round-{round_no}")
            announce(self.verification)
            r = self.verification.run(self.twin, baseline, incident, selected, final)
            results.append(r); self.audit.record(r.stage, r.agent, r.to_dict()); emit("agent", r)

            if not r.extras.get("replan_required"):
                break
            if round_no >= max_rounds:
                halted = f"連續 {max_rounds} 輪仍未讓關鍵業務全數恢復，已升級人工處理"
                emit("replan_exhausted", {"rounds": round_no})
                break
            emit("replan", {"round": round_no})

        result = LoopResult(
            scenario=scenario,
            baseline=baseline,
            incident=incident,
            final=final,
            plans=all_plans,
            executed_plan=executed,
            agent_results=results,
            approved=approved,
            rounds=max(round_no, 1),
            halted_reason=halted,
            audit_path=self.audit.path,
            llm_mode=self.llm.mode,
        )
        self.audit.record("loop_complete", "Orchestrator", result.summary())
        return result

    # ------------------------------------------------------------------ 稽核

    def verify_audit(self) -> tuple[bool, str]:
        return self.audit.verify()


__all__ = ["Orchestrator", "LoopResult", "auto_approve", "auto_reject", "ALLOW", "DENY"]
