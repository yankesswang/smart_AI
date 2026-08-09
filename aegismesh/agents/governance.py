"""Policy Agent —— Approve 階段的守門人。

這個 Agent 沒有裁量權：decision 完全由 policies.yaml 的規則引擎決定。
LLM 的角色是把政策命中結果整理成給值班主管看的核准說明 ——
它不能把 deny 講成 allow，因為 decision 欄位根本不經過它。
"""

from __future__ import annotations

from typing import Any

from ..domain import NetworkSnapshot, RecoveryPlan
from ..llm import compact_json
from ..optimizer import STRATEGY_LABELS
from ..policy.engine import ALLOW, DENY, PolicyEngine
from ..twin.engine import DigitalTwin
from .base import PLAIN_LANGUAGE, Agent, AgentResult


class PolicyAgent(Agent):
    name = "Policy Agent"
    stage = "govern"
    system_prompt = (
        "你負責向值班主管說明這次網路變更的審查結果。"
        "規則引擎已對方案做出判定（可直接執行 / 需主管核准 / 不准執行），"
        "這個判定是最終結果，你不能更改，也不可以把「不准執行」講成可以執行。"
        "請用繁體中文寫 1-2 句話說明：為什麼是這個結果、以及主管按下核准前該確認什麼。"
        + PLAIN_LANGUAGE +
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
                    "strategy_label": STRATEGY_LABELS.get(p.strategy, p.strategy),
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
                return {"briefing": "三個方案都違反了管理規則，已全數擋下，必須由人工介入處理。"}
            picked = STRATEGY_LABELS.get(selected.strategy, selected.strategy)
            if selected.policy_decision == ALLOW:
                return {"briefing": f"「{picked}」方案沒有踩到任何管理規則，可以直接執行。"}
            reasons = "；".join(selected.policy_findings) or "觸發了需要核准的規則"
            return {
                "briefing": (
                    f"「{picked}」方案風險評分 {selected.risk_score:.0f} 分，"
                    f"依規定須由值班主管核准：{reasons}"
                )
            }

        out = self._ask(
            f"審查結果（請用 strategy_label 稱呼方案）：{compact_json(facts['evaluations'])}\n"
            f"選定方案：{STRATEGY_LABELS.get(selected.strategy, '無') if selected else '無'}",
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
