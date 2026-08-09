"""Verification Agent —— Verify 階段，閉環的最後一哩。

執行後重新量測，並與「推演預測值」對照。預測與實測的落差是這套系統
最重要的可信度指標：如果孿生推演不準，整個先推演後執行的主張就站不住腳。
驗證失敗會回報 replan_required，讓 Orchestrator 重跑閉環。
"""

from __future__ import annotations

from typing import Any

from ..domain import NetworkSnapshot, RecoveryPlan
from ..llm import compact_json
from ..twin.engine import DigitalTwin
from .base import Agent, AgentResult

_LATENCY_TOLERANCE_MS = 5.0     # 預測與實測的可接受落差
_AVAILABILITY_TOLERANCE_PCT = 0.1


class VerificationAgent(Agent):
    name = "Verification Agent"
    stage = "verify"
    system_prompt = (
        "你是網路變更後的驗收工程師。你會收到變更前、預測、與變更後的三組實測指標。"
        "請用繁體中文寫 2-3 句話說明：關鍵業務是否恢復、量化改善幅度為何、"
        "以及孿生推演的預測是否準確。只能引用給定數據。"
        '輸出 JSON：{"verdict": "2-3 句驗收結論"}'
    )

    def run(
        self,
        twin: DigitalTwin,
        baseline: NetworkSnapshot,
        incident: NetworkSnapshot,
        plan: RecoveryPlan,
        actual: NetworkSnapshot,
    ) -> AgentResult:
        projected = plan.projected
        drift: list[dict[str, Any]] = []
        if projected is not None:
            for sid, act in actual.services.items():
                pred = projected.services[sid]
                if pred.latency_ms == float("inf") or act.latency_ms == float("inf"):
                    delta = 0.0 if pred.reachable == act.reachable else float("inf")
                else:
                    delta = abs(pred.latency_ms - act.latency_ms)
                if delta > _LATENCY_TOLERANCE_MS or pred.slo_met != act.slo_met:
                    drift.append({
                        "service_id": sid,
                        "predicted_latency_ms": None if pred.latency_ms == float("inf") else round(pred.latency_ms, 1),
                        "actual_latency_ms": None if act.latency_ms == float("inf") else round(act.latency_ms, 1),
                        "predicted_slo_met": pred.slo_met,
                        "actual_slo_met": act.slo_met,
                    })

        crit_recovered = actual.critical_availability_pct >= 100.0 - _AVAILABILITY_TOLERANCE_PCT
        still_failing = [
            twin.services[sid].name for sid, st in actual.services.items()
            if twin.services[sid].is_critical and not st.slo_met
        ]
        replan_required = not crit_recovered

        facts = {
            "critical_availability": {
                "baseline_pct": round(baseline.critical_availability_pct, 1),
                "incident_pct": round(incident.critical_availability_pct, 1),
                "projected_pct": round(projected.critical_availability_pct, 1) if projected else None,
                "actual_pct": round(actual.critical_availability_pct, 1),
            },
            "slo_compliance": {
                "baseline_pct": round(baseline.slo_compliance_pct, 1),
                "incident_pct": round(incident.slo_compliance_pct, 1),
                "actual_pct": round(actual.slo_compliance_pct, 1),
            },
            "monthly_cost_ntd": {
                "baseline": round(baseline.monthly_cost_ntd),
                "actual": round(actual.monthly_cost_ntd),
            },
            "critical_services_recovered": crit_recovered,
            "critical_still_failing": still_failing,
            "prediction_drift": drift,
            "prediction_accurate": not drift,
            "replan_required": replan_required,
        }

        def fallback() -> dict[str, Any]:
            status = "已完全恢復" if crit_recovered else f"仍有 {len(still_failing)} 項未恢復"
            acc = "與推演預測一致" if not drift else f"有 {len(drift)} 項與預測不符"
            return {
                "verdict": (
                    f"生命關鍵業務{status}：可用率自事故當下的 "
                    f"{incident.critical_availability_pct:.0f}% 回升至 "
                    f"{actual.critical_availability_pct:.0f}%，"
                    f"整體 SLA 達成率 {incident.slo_compliance_pct:.0f}% → "
                    f"{actual.slo_compliance_pct:.0f}%。實測結果{acc}。"
                )
            }

        out = self._ask(f"驗收資料：{compact_json(facts)}", fallback)

        return AgentResult(
            agent=self.name,
            stage=self.stage,
            facts=facts,
            narrative=out.get("verdict", ""),
            llm_mode=out.get("_llm", "offline"),
            extras={"replan_required": replan_required},
        )
