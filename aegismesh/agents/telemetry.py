"""Telemetry Agent —— Observe 階段。

偵測鏈路中斷、延遲上升、丟包與壅塞，並判定事故嚴重度。
異常判定完全由門檻與量測值決定，LLM 只負責把它講成人話。
"""

from __future__ import annotations

from typing import Any

from ..domain import NetworkSnapshot, Severity
from ..llm import compact_json
from ..twin.engine import DigitalTwin
from .base import PLAIN_LANGUAGE, Agent, AgentResult

_UTIL_WARN_PCT = 75.0
_LATENCY_RATIO_WARN = 2.0   # 延遲超過基礎值的倍數


class TelemetryAgent(Agent):
    name = "Telemetry Agent"
    stage = "observe"
    system_prompt = (
        "你是醫院網路的監控人員，正在向不具電信背景的院方主管與評審報告狀況。"
        "你會收到一份系統量測出的異常清單。"
        "請只根據清單內容，用繁體中文寫一段 2-3 句的狀況播報，"
        "說明發生什麼事、哪一段網路出問題、以及現在最該擔心什麼。"
        "不要杜撰清單中沒有的數據。線路一律使用清單中 link_name 的中文名稱。"
        + PLAIN_LANGUAGE +
        '輸出 JSON：{"headline": "一句話標題", "narrative": "2-3 句敘述"}'
    )

    def run(
        self, twin: DigitalTwin, baseline: NetworkSnapshot, current: NetworkSnapshot
    ) -> AgentResult:
        anomalies: list[dict[str, Any]] = []

        for lid, link in twin.links.items():
            now = current.links[lid]
            base = baseline.links[lid]
            # 代號留給稽核，名稱留給人與 LLM
            name = twin.link_label(lid)

            if now.state == "down":
                anomalies.append({
                    "link": lid, "link_name": name, "type": "link_down",
                    "severity": Severity.CRITICAL.value,
                    "detail": f"{name}完全中斷（原本 {link.capacity_mbps:.0f} Mbps 全部歸零）",
                })
                continue

            if link.netem_extra_latency_ms > 0 and now.latency_ms > base.latency_ms * _LATENCY_RATIO_WARN:
                anomalies.append({
                    "link": lid, "link_name": name, "type": "latency_spike",
                    "severity": Severity.WARNING.value,
                    "detail": f"{name}的反應時間由 {base.latency_ms:.1f} 毫秒變慢到 {now.latency_ms:.1f} 毫秒",
                })
            if link.netem_capacity_factor < 1.0:
                anomalies.append({
                    "link": lid, "link_name": name, "type": "capacity_loss",
                    "severity": Severity.WARNING.value,
                    "detail": f"{name}可用速度只剩 {link.netem_capacity_factor:.0%}"
                              f"（{now.capacity_mbps:.0f} Mbps）",
                })
            if now.utilization_pct >= _UTIL_WARN_PCT:
                anomalies.append({
                    "link": lid, "link_name": name, "type": "congestion",
                    "severity": Severity.WARNING.value,
                    "detail": f"{name}已用掉 {now.utilization_pct:.0f}% 的容量，開始塞車",
                })
            if now.loss_pct > max(0.1, base.loss_pct * 3):
                anomalies.append({
                    "link": lid, "link_name": name, "type": "packet_loss",
                    "severity": Severity.WARNING.value,
                    "detail": f"{name}的資料遺失率達 {now.loss_pct:.2f}%（平常 {base.loss_pct:.2f}%）",
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
                return {"headline": "網路狀態正常", "narrative": "所有線路與醫療服務都在正常範圍內。"}
            heads = "；".join(a["detail"] for a in anomalies[:3]) or "多項指標變差"
            return {
                "headline": f"偵測到 {len(anomalies)} 項網路異常",
                "narrative": (
                    f"{heads}。目前有 {len(unreachable)} 項醫療服務完全斷線，"
                    f"救命服務正常率 {current.critical_availability_pct:.0f}%。"
                ),
            }

        out = self._ask(
            f"異常清單（請直接使用其中的 link_name 中文名稱）：{compact_json(anomalies)}\n"
            f"完全斷線的醫療服務：{[twin.services[s].name for s in unreachable]}\n"
            f"救命服務正常率：{current.critical_availability_pct:.0f}%",
            fallback,
        )

        return AgentResult(
            agent=self.name,
            stage=self.stage,
            facts=facts,
            narrative=f"{out.get('headline', '')}｜{out.get('narrative', '')}".strip("｜"),
            llm_mode=out.get("_llm", "offline"),
        )
