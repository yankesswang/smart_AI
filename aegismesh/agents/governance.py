"""Policy Agent —— Approve 階段的守門人。

這個 Agent 沒有裁量權：decision 完全由 policies.yaml 的規則引擎決定。
LLM 的角色是把政策命中結果整理成給值班主管看的核准說明 ——
它不能把 deny 講成 allow，因為 decision 欄位根本不經過它。
"""

from __future__ import annotations

from typing import Any

from ..domain import NetworkSnapshot, RecoveryPlan
from ..llm import compact_json
from ..policy.engine import ALLOW, DENY, PolicyEngine
from ..twin.engine import DigitalTwin
from .base import Agent, AgentResult


class PolicyAgent(Agent):
    name = "Policy Agent"
    stage = "govern"
    system_prompt = (
        "你是電信維運的變更管理與法遵稽核人員。"
        "政策引擎已對復原計畫做出裁決（allow / require_approval / deny），"
        "這個裁決是最終結果，你不能更改。"
        "請用繁體中文寫 1-2 句話向值班主管說明：為什麼是這個裁決、"
        "以及核准前應該確認什麼。"
        '輸出 JSON：{"briefing": "1-2 句核准說明"}'
    )

    def __init__(self, llm, policy_engine: PolicyEngine | None = None) -> None:
        super().__init__(llm)
        self.policy = policy_engine or PolicyEngine()

    def run(
        self, twin: DigitalTwin, plans: list[RecoveryPlan], incident: NetworkSnapshot
    ) -> tuple[AgentResult, RecoveryPlan | None]:
        for plan in plans:
            self.policy.apply_to(twin, plan, incident)

        # 只有沒被 deny 的計畫可進入核准流程；同分時取評分最高者
        eligible = [p for p in plans if p.policy_decision != DENY]
        selected = max(eligible, key=lambda p: p.score) if eligible else None

        facts = {
            "policy_version": self.policy.version,
            "rule_count": len(self.policy.rules),
            "evaluations": [
                {
                    "plan_id": p.id,
                    "decision": p.policy_decision,
                    "risk_score": round(p.risk_score, 1),
                    "findings": p.policy_findings,
                }
                for p in plans
            ],
            "selected_plan": selected.id if selected else None,
            "requires_human_approval": bool(selected and selected.policy_decision != ALLOW),
        }

        def fallback() -> dict[str, Any]:
            if selected is None:
                return {"briefing": "所有候選計畫皆違反治理政策，已全數擋下，須人工介入處理。"}
            if selected.policy_decision == ALLOW:
                return {"briefing": f"計畫 {selected.id} 未觸發任何治理規則，可自動執行。"}
            reasons = "；".join(selected.policy_findings) or "觸發需核准規則"
            return {
                "briefing": (
                    f"計畫 {selected.id} 風險評分 {selected.risk_score:.0f}，"
                    f"須人工核准：{reasons}"
                )
            }

        out = self._ask(
            f"政策裁決結果：{compact_json(facts['evaluations'])}\n"
            f"選定計畫：{selected.id if selected else '無'}",
            fallback,
        )

        return (
            AgentResult(
                agent=self.name,
                stage=self.stage,
                facts=facts,
                narrative=out.get("briefing", ""),
                llm_mode=out.get("_llm", "offline"),
            ),
            selected,
        )
