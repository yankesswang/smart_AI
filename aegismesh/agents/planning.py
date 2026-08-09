"""Planning Agent —— Plan ＋ Simulate 兩階段。

重點：候選計畫由最佳化器產生、由孿生推演驗證，排名由確定性評分決定。
LLM 只在「已經算完、已經排好」之後，替最佳方案寫一段可讀的決策理由。
即使 LLM 完全離線，Planning Agent 仍然會產出同一份計畫。
"""

from __future__ import annotations

from typing import Any

from ..domain import NetworkSnapshot, RecoveryPlan
from ..llm import compact_json
from ..optimizer import STRATEGY_LABELS, generate_plans, score_plan
from ..twin.engine import DigitalTwin
from .base import PLAIN_LANGUAGE, Agent, AgentResult


class PlanningAgent(Agent):
    name = "Planning Agent"
    stage = "plan+simulate"
    system_prompt = (
        "你要向醫院管理層說明系統挑出來的救援方案。"
        "電腦已用演算法算出數個備案，並在網路模擬環境中跑過各自的預期結果"
        "（這些數字是算出來的，你不可以更改）。"
        "請用繁體中文寫 2-3 句話，說明為什麼推薦的方案勝出，以及它付出的代價"
        "（多花多少錢、哪些服務被降速或暫停）。只能引用給定的數據。"
        + PLAIN_LANGUAGE +
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
                "strategy_label": STRATEGY_LABELS.get(p.strategy, p.strategy),
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
                    f"推薦「{b['strategy_label']}」方案：救命服務正常率可回到 "
                    f"{b['critical_availability_pct']:.0f}%、整體服務達標率 "
                    f"{b['slo_compliance_pct']:.0f}%，綜合評分 {b['score']} 為三個方案中最高。"
                ),
                "tradeoff": (
                    f"每月通訊費用估計 NT${b['monthly_cost_ntd']:,}，需暫停的服務：{suspended}。"
                ),
            }

        out = self._ask(
            f"災害當下：救命服務正常率 {incident.critical_availability_pct:.0f}%、"
            f"整體服務達標率 {incident.slo_compliance_pct:.0f}%、"
            f"每月費用 NT${incident.monthly_cost_ntd:,.0f}\n"
            f"三個方案的模擬結果（已依評分排序，請用 strategy_label 稱呼方案）："
            f"{compact_json(digest)}\n"
            f"推薦方案：{STRATEGY_LABELS.get(best.strategy, best.strategy)}",
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
