"""Impact Agent —— 判斷事故對業務與 SLA 的實際衝擊。

網路指標劣化不等於臨床衝擊。這個 Agent 把「哪條鏈路壞了」翻譯成
「哪些醫療服務受影響、影響多深、還剩多少時間可以決策」。
"""

from __future__ import annotations

from typing import Any

from ..domain import NetworkSnapshot
from ..llm import compact_json
from ..twin.engine import DigitalTwin
from .base import Agent, AgentResult


class ImpactAgent(Agent):
    name = "Impact Agent"
    stage = "assess"
    system_prompt = (
        "你是醫院資訊室與電信服務供應商之間的服務影響分析師。"
        "你會收到一份業務衝擊清單，每項包含業務名稱、臨床用途、SLA 違反原因。"
        "請用繁體中文寫 2-3 句話說明這次事故對院方的實際影響，"
        "並指出最優先必須恢復的業務。只能引用清單中的事實。"
        '輸出 JSON：{"business_impact": "2-3 句影響說明", '
        '"restore_first": ["最該優先恢復的 service_id"]}'
    )

    def run(
        self, twin: DigitalTwin, baseline: NetworkSnapshot, current: NetworkSnapshot
    ) -> AgentResult:
        impacted: list[dict[str, Any]] = []
        for sid, now in current.services.items():
            was_ok = baseline.services[sid].slo_met
            if was_ok and now.slo_met:
                continue
            svc = twin.services[sid]
            impacted.append({
                "service_id": sid,
                "name": svc.name,
                "priority": svc.priority,
                "critical": svc.is_critical,
                "clinical_note": svc.clinical_note,
                "reachable": now.reachable,
                "violations": now.violations,
                "admitted_mbps": round(now.admitted_mbps, 1),
            })

        impacted.sort(key=lambda x: x["priority"])
        critical_hit = [i for i in impacted if i["critical"]]

        facts = {
            "impacted_count": len(impacted),
            "critical_impacted": [i["service_id"] for i in critical_hit],
            "impacted": impacted,
            "slo_compliance_before_pct": round(baseline.slo_compliance_pct, 1),
            "slo_compliance_now_pct": round(current.slo_compliance_pct, 1),
            "critical_availability_now_pct": round(current.critical_availability_pct, 1),
        }

        def fallback() -> dict[str, Any]:
            if not impacted:
                return {"business_impact": "尚無業務受到影響。", "restore_first": []}
            names = "、".join(i["name"] for i in impacted[:4])
            crit = "、".join(i["name"] for i in critical_hit) or "無"
            return {
                "business_impact": (
                    f"受影響業務共 {len(impacted)} 項：{names}。"
                    f"其中生命關鍵業務為 {crit}；"
                    f"整體 SLA 達成率自 {baseline.slo_compliance_pct:.0f}% "
                    f"降至 {current.slo_compliance_pct:.0f}%。"
                ),
                "restore_first": [i["service_id"] for i in impacted[:2]],
            }

        out = self._ask(f"業務衝擊清單：{compact_json(impacted)}", fallback)
        restore_first = out.get("restore_first") or []
        # LLM 可能給出不存在的 id；用引擎事實過濾，避免污染下游
        restore_first = [s for s in restore_first if s in twin.services]

        return AgentResult(
            agent=self.name,
            stage=self.stage,
            facts=facts,
            narrative=out.get("business_impact", ""),
            llm_mode=out.get("_llm", "offline"),
            extras={"restore_first": restore_first},
        )
