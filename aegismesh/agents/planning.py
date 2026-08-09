"""Planning Agent —— Plan ＋ Simulate 兩階段。

重點：候選計畫由最佳化器產生、由孿生推演驗證，排名由確定性評分決定。
LLM 只在「已經算完、已經排好」之後，替最佳方案寫一段可讀的決策理由。
即使 LLM 完全離線，Planning Agent 仍然會產出同一份計畫。
"""

from __future__ import annotations

from typing import Any

from ..domain import NetworkSnapshot, RecoveryPlan
from ..llm import compact_json
from ..optimizer import generate_plans, score_plan
from ..twin.engine import DigitalTwin
from .base import Agent, AgentResult


class PlanningAgent(Agent):
    name = "Planning Agent"
    stage = "plan+simulate"
    system_prompt = (
        "你是電信網路的復原策略分析師。系統已用最佳化演算法產生數個候選計畫，"
        "並在數位孿生中推演出各自的預測成效（這些數字是算出來的，不可更改）。"
        "請用繁體中文寫 2-3 句話，說明為什麼推薦的計畫勝出，以及它付出的代價"
        "（成本、被犧牲的業務）。只能引用給定的數據。"
        '輸出 JSON：{"rationale": "2-3 句推薦理由", "tradeoff": "一句話說明代價"}'
    )

    def run(
        self, twin: DigitalTwin, incident: NetworkSnapshot
    ) -> tuple[AgentResult, list[RecoveryPlan]]:
        plans = generate_plans(twin)
        for plan in plans:
            plan.score = score_plan(incident, plan.projected, plan)
        plans.sort(key=lambda p: p.score, reverse=True)

        if not plans:
            return (
                AgentResult(self.name, self.stage,
                            {"plan_count": 0}, "無可行復原路徑，需人工介入。"),
                [],
            )

        best = plans[0]
        digest = [
            {
                "id": p.id,
                "strategy": p.strategy,
                "actions": len(p.actions),
                "critical_availability_pct": round(p.projected.critical_availability_pct, 1),
                "slo_compliance_pct": round(p.projected.slo_compliance_pct, 1),
                "monthly_cost_ntd": round(p.projected.monthly_cost_ntd),
                "score": round(p.score, 1),
                "suspended": [
                    twin.services[sid].name
                    for sid, st in p.projected.services.items() if st.admitted_mbps <= 0
                ],
            }
            for p in plans
        ]

        def fallback() -> dict[str, Any]:
            b = digest[0]
            suspended = "、".join(b["suspended"]) or "無"
            return {
                "rationale": (
                    f"推薦 {best.strategy}：關鍵業務可用率可回到 "
                    f"{b['critical_availability_pct']:.0f}%、整體 SLA 達成率 "
                    f"{b['slo_compliance_pct']:.0f}%，綜合評分 {b['score']} 為候選中最高。"
                ),
                "tradeoff": (
                    f"月成本估計 NT${b['monthly_cost_ntd']:,}，需暫停的業務：{suspended}。"
                ),
            }

        out = self._ask(
            f"事故當下：關鍵可用率 {incident.critical_availability_pct:.0f}%、"
            f"SLA 達成率 {incident.slo_compliance_pct:.0f}%、"
            f"月成本 NT${incident.monthly_cost_ntd:,.0f}\n"
            f"候選計畫推演結果（已依評分排序）：{compact_json(digest)}\n"
            f"推薦計畫：{best.id}",
            fallback,
        )
        best.llm_rationale = out.get("rationale", "")

        return (
            AgentResult(
                agent=self.name,
                stage=self.stage,
                facts={"plan_count": len(plans), "candidates": digest, "recommended": best.id},
                narrative=f"{out.get('rationale', '')} {out.get('tradeoff', '')}".strip(),
                llm_mode=out.get("_llm", "offline"),
            ),
            plans,
        )
