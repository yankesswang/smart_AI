"""Telemetry Agent —— Observe 階段。

偵測鏈路中斷、延遲上升、丟包與壅塞，並判定事故嚴重度。
異常判定完全由門檻與量測值決定，LLM 只負責把它講成人話。
"""

from __future__ import annotations

from typing import Any

from ..domain import NetworkSnapshot, Severity
from ..llm import compact_json
from ..twin.engine import DigitalTwin
from .base import Agent, AgentResult

_UTIL_WARN_PCT = 75.0
_LATENCY_RATIO_WARN = 2.0   # 延遲超過基礎值的倍數


class TelemetryAgent(Agent):
    name = "Telemetry Agent"
    stage = "observe"
    system_prompt = (
        "你是電信網路維運中心（NOC）的一線監控分析師。"
        "你會收到一份由網路數位孿生量測出的異常清單。"
        "請只根據清單內容，用繁體中文寫一段 2-3 句的事故播報，"
        "說明發生什麼、哪一段網路、以及當下最該注意的風險。"
        "不要杜撰清單中沒有的數據。"
        '輸出 JSON：{"headline": "一句話標題", "narrative": "2-3 句敘述"}'
    )

    def run(
        self, twin: DigitalTwin, baseline: NetworkSnapshot, current: NetworkSnapshot
    ) -> AgentResult:
        anomalies: list[dict[str, Any]] = []

        for lid, link in twin.links.items():
            now = current.links[lid]
            base = baseline.links[lid]

            if now.state == "down":
                anomalies.append({
                    "link": lid, "type": "link_down", "severity": Severity.CRITICAL.value,
                    "detail": f"{link.kind.value} 鏈路中斷（容量 {link.capacity_mbps:.0f} Mbps 歸零）",
                })
                continue

            if link.netem_extra_latency_ms > 0 and now.latency_ms > base.latency_ms * _LATENCY_RATIO_WARN:
                anomalies.append({
                    "link": lid, "type": "latency_spike", "severity": Severity.WARNING.value,
                    "detail": f"延遲由 {base.latency_ms:.1f}ms 升至 {now.latency_ms:.1f}ms",
                })
            if link.netem_capacity_factor < 1.0:
                anomalies.append({
                    "link": lid, "type": "capacity_loss", "severity": Severity.WARNING.value,
                    "detail": f"可用容量降至 {link.netem_capacity_factor:.0%}"
                              f"（{now.capacity_mbps:.0f} Mbps）",
                })
            if now.utilization_pct >= _UTIL_WARN_PCT:
                anomalies.append({
                    "link": lid, "type": "congestion", "severity": Severity.WARNING.value,
                    "detail": f"使用率 {now.utilization_pct:.0f}%，已進入壅塞區間",
                })
            if now.loss_pct > max(0.1, base.loss_pct * 3):
                anomalies.append({
                    "link": lid, "type": "packet_loss", "severity": Severity.WARNING.value,
                    "detail": f"丟包率 {now.loss_pct:.2f}%（基準 {base.loss_pct:.2f}%）",
                })

        unreachable = [sid for sid, s in current.services.items() if not s.reachable]
        severity = (
            Severity.CRITICAL if (unreachable or any(a["type"] == "link_down" for a in anomalies))
            else Severity.WARNING if anomalies
            else Severity.OK
        )

        facts = {
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
            "severity": severity.value,
            "unreachable_services": unreachable,
            "critical_availability_pct": round(current.critical_availability_pct, 1),
        }

        def fallback() -> dict[str, Any]:
            if severity is Severity.OK:
                return {"headline": "網路狀態正常", "narrative": "所有鏈路與業務均在 SLO 範圍內。"}
            heads = "；".join(a["detail"] for a in anomalies[:3]) or "多項指標劣化"
            return {
                "headline": f"偵測到 {len(anomalies)} 項網路異常",
                "narrative": (
                    f"{heads}。目前有 {len(unreachable)} 項業務失去連線，"
                    f"關鍵業務可用率 {current.critical_availability_pct:.0f}%。"
                ),
            }

        out = self._ask(
            f"異常清單：{compact_json(anomalies)}\n"
            f"失聯業務：{unreachable}\n"
            f"關鍵業務可用率：{current.critical_availability_pct:.0f}%",
            fallback,
        )

        return AgentResult(
            agent=self.name,
            stage=self.stage,
            facts=facts,
            narrative=f"{out.get('headline', '')}｜{out.get('narrative', '')}".strip("｜"),
            llm_mode=out.get("_llm", "offline"),
        )
