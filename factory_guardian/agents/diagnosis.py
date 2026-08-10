"""Diagnosis Agent（規格 §4.2）。

輸入：Sensor、Error Code、Demo Equipment Manual、Maintenance History、SOP。
輸出：Root Cause 候選、信心度與 Evidence。
紅線：**不得讀取 Simulator 的真實故障標籤**。

排名怎麼算出來的（三個訊號，權重寫死在程式碼裡，可稽核）：

1. **感測器指紋餘弦相似度（0.75）** —— 主要判準。
   把觀測到的偏離量以各訊號的 scale 正規化成一個向量，和手冊描述的故障指紋比對。
   這是數字比對，比文字相似度可靠得多：軸承劣化的振動主導、冷卻失效的溫度單獨異常、
   馬達過載的電流上升 + 轉速下降，在向量空間裡是分得開的。

2. **歷史先驗（0.15）** —— 這台機器過去得過什麼病，近期案例權重較高。

3. **文件支持度（0.10）** —— RAG 檢索到的手冊/SOP/歷史案例對各候選的支持程度。
   權重刻意壓低：所有故障的手冊都會提到同樣那四個訊號，純文字相似度鑑別力有限，
   它的價值在於「產生可引用的 Evidence」，而不是決定排名。

信心度另外會被「訊號強度」壓抑：訊號還很微弱時（剛開始劣化），
即使指紋比對指向某個故障，信心度也不該衝到 90%。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..domain import (
    AnomalyEvent,
    Diagnosis,
    Evidence,
    FactorySnapshot,
    FaultSignature,
    RootCauseCandidate,
)
from ..knowledge.corpus import manual_by_ref
from ..llm import SYSTEM_PROMPT
from ..twin.faults import FAULTS, fault_signatures
from .base import Agent

W_SIGNATURE = 0.75
W_PRIOR = 0.15
W_DOCS = 0.10
SOFTMAX_TEMPERATURE = 0.16
# 觀測向量長度低於這個值時，視為訊號太弱，信心度往均勻分布拉。
STRENGTH_FULL = 1.2
# 偏離量低於這個值時，判定為「沒有設備故障徵兆」。
NO_FAULT_STRENGTH = 0.45
NO_FAULT_ID = "no_equipment_fault"


@dataclass
class _Match:
    signature: FaultSignature
    cosine: float
    prior: float
    docs: float
    combined: float


class DiagnosisAgent(Agent):
    name = "diagnosis-agent"
    role = "根因分析"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.signatures: list[FaultSignature] = []

    # ------------------------------------------------------------------ 主流程
    def diagnose(
        self,
        event: AnomalyEvent,
        snapshot: FactorySnapshot,
        topology,
        smoothed: dict[str, float] | None = None,
    ) -> Diagnosis:
        machine_id = event.machine_id
        machine = topology.machines[machine_id]
        machine_state = snapshot.machines[machine_id].state.value
        scales = {spec.name: spec.scale for spec in machine.signals}
        self.signatures = [s for s in fault_signatures(scales) if s.fault_id in FAULTS]

        readings = smoothed or {name: r.value for name, r in event.readings.items()}
        observed = self._deviation_vector(machine, readings)
        strength = math.sqrt(sum(v * v for v in observed.values()))

        with self.timed() as timing:
            with self.tool("sensor.deviation_vector", f"machine={machine_id}"):
                cosines = {sig.fault_id: self._cosine(observed, sig.profile) for sig in self.signatures}

            with self.tool("history.machine_fault_prior", f"machine={machine_id}"):
                priors = self.ctx.kb.machine_fault_prior(machine_id, list(cosines))

            query = self._build_query(machine, readings, observed)
            with self.tool("rag.search", f"chars={len(query)}"):
                docs = self.ctx.kb.fault_affinity(query, list(cosines))
                retrieved = self.ctx.kb.search(query, top_k=6, machine_id=machine_id)

            matches = [
                _Match(
                    signature=sig,
                    cosine=cosines[sig.fault_id],
                    prior=priors.get(sig.fault_id, 0.0),
                    docs=docs.get(sig.fault_id, 0.0),
                    combined=(
                        W_SIGNATURE * max(0.0, cosines[sig.fault_id])
                        + W_PRIOR * priors.get(sig.fault_id, 0.0)
                        + W_DOCS * docs.get(sig.fault_id, 0.0)
                    ),
                )
                for sig in self.signatures
            ]
            confidences = self._confidences(matches, strength)

            candidates = [
                self._build_candidate(m, confidences[m.signature.fault_id], machine_id, readings, retrieved)
                for m in sorted(matches, key=lambda m: -m.combined)
            ]
            # 訊號都還在正常範圍時，最誠實的答案是「這台機器沒有故障徵兆」，
            # 而不是硬從三個故障裡挑一個最像的。工安事件觸發的閉環會走到這裡。
            no_fault = self._no_fault_candidate(strength, event)
            if no_fault is not None and (not candidates or no_fault.confidence > candidates[0].confidence):
                candidates.insert(0, no_fault)

        narrative = self._narrate(event, candidates, readings, strength)
        diagnosis = Diagnosis(
            machine_id=machine_id,
            candidates=candidates,
            narrative=narrative,
            latency_ms=timing.get("latency_ms", 0.0),
            llm_mode=self.ctx.llm.mode if self.ctx.llm else "offline",
            signal_strength=strength,
            weights={"signature": W_SIGNATURE, "prior": W_PRIOR, "docs": W_DOCS},
            thresholds={"strength_full": STRENGTH_FULL, "no_fault": NO_FAULT_STRENGTH},
            observations=self._observations(machine, readings, observed),
        )
        self.log(
            "diagnose",
            event_id=event.event_id,
            machine_id=machine_id,
            machine_state=machine_state,
            signal_strength=round(strength, 3),
            scores={
                m.signature.fault_id: {
                    "cosine": round(m.cosine, 3),
                    "prior": round(m.prior, 3),
                    "docs": round(m.docs, 3),
                    "combined": round(m.combined, 3),
                    "confidence": round(confidences[m.signature.fault_id], 3),
                }
                for m in matches
            },
            top=diagnosis.top.fault_id if diagnosis.top else None,
            latency_ms=round(diagnosis.latency_ms, 1),
            note="未讀取 Simulator ground truth；排名由數值比對決定，LLM 僅寫敘述。",
        )
        return diagnosis

    # ------------------------------------------------------------------ 計算
    @staticmethod
    def _no_fault_candidate(strength: float, event) -> RootCauseCandidate | None:
        """訊號強度低於門檻時，產生「無設備故障徵兆」候選。"""
        if strength >= NO_FAULT_STRENGTH:
            return None
        confidence = max(0.0, min(1.0, 1.0 - strength / NO_FAULT_STRENGTH))
        safety_triggered = getattr(event, "kind", "equipment") == "safety"
        evidence = [
            Evidence(
                source="sensor",
                reference=f"{event.machine_id}.deviation",
                statement=f"四項訊號的正規化偏離量僅 {strength:.2f}，全部落在正常區間內。",
                weight=confidence,
            )
        ]
        if safety_triggered:
            evidence.append(
                Evidence(source="vision", reference="CAM", statement="本次閉環由工安事件觸發，而非設備感測器異常。")
            )
        return RootCauseCandidate(
            fault_id=NO_FAULT_ID,
            label="無設備故障徵兆（工安/操作事件）",
            confidence=confidence,
            evidence=evidence,
            recommended_actions=[
                "本事件不需要設備維修；請依 SOP-SF-01 處理現場工安風險。",
                "確認人員撤離危險區並完成 LOTO 後才可復機。",
            ],
        )

    @staticmethod
    def _observations(machine, readings: dict[str, float], observed: dict[str, float]) -> list[dict[str, object]]:
        """Agent 這一步「看到什麼」：每個訊號的觀測值、正常值與正規化偏離量。

        偏離量是排名的唯一輸入（指紋比對比的就是這個向量），
        所以它必須跟著診斷結果一起送到前端，否則畫面只能顯示結論、無法顯示依據。
        """
        rows: list[tuple[float, dict[str, object]]] = []
        for spec in machine.signals:
            value = readings.get(spec.name)
            if value is None:
                continue
            deviation = observed.get(spec.name, 0.0)
            rows.append(
                (
                    deviation,
                    {
                        "name": spec.name,
                        "unit": spec.unit,
                        "value": round(value, 2),
                        "nominal": round(spec.nominal, 2),
                        "deviation": round(deviation, 2),
                        "band": spec.band(value).value,
                    },
                )
            )
        # 偏離量大的排前面：主導這次判斷的訊號要先被看到。
        rows.sort(key=lambda row: -abs(row[0]))
        return [row for _, row in rows]

    @staticmethod
    def _deviation_vector(machine, readings: dict[str, float]) -> dict[str, float]:
        """觀測偏離向量：(觀測值 − 正常值) / scale。"""
        vector: dict[str, float] = {}
        for spec in machine.signals:
            value = readings.get(spec.name)
            if value is None:
                continue
            vector[spec.name] = (value - spec.nominal) / spec.scale
        return vector

    @staticmethod
    def _cosine(observed: dict[str, float], profile: dict[str, float]) -> float:
        keys = set(observed) | set(profile)
        dot = sum(observed.get(k, 0.0) * profile.get(k, 0.0) for k in keys)
        n1 = math.sqrt(sum(observed.get(k, 0.0) ** 2 for k in keys))
        n2 = math.sqrt(sum(profile.get(k, 0.0) ** 2 for k in keys))
        if n1 < 1e-9 or n2 < 1e-9:
            return 0.0
        return dot / (n1 * n2)

    @staticmethod
    def _confidences(matches: list[_Match], strength: float) -> dict[str, float]:
        """Softmax 轉信心度，再依訊號強度往均勻分布拉。"""
        if not matches:
            return {}
        top = max(m.combined for m in matches)
        exps = {m.signature.fault_id: math.exp((m.combined - top) / SOFTMAX_TEMPERATURE) for m in matches}
        total = sum(exps.values()) or 1.0
        sharp = {fid: value / total for fid, value in exps.items()}
        uniform = 1.0 / len(matches)
        # 訊號越弱，越往均勻分布靠 —— 早期劣化本來就不該給高信心。
        certainty = max(0.0, min(1.0, strength / STRENGTH_FULL))
        return {fid: uniform + (value - uniform) * certainty for fid, value in sharp.items()}

    def _build_query(self, machine, readings: dict[str, float], observed: dict[str, float]) -> str:
        """依偏離程度排序組出檢索查詢，讓最異常的訊號主導召回。"""
        ranked = sorted(observed.items(), key=lambda kv: -abs(kv[1]))
        parts: list[str] = []
        for name, dev in ranked:
            spec = machine.signal(name)
            value = readings.get(name, 0.0)
            band = spec.band(value).value if spec else "?"
            direction = "上升" if dev > 0 else "下降"
            magnitude = "大幅" if abs(dev) >= 1.0 else ("明顯" if abs(dev) >= 0.5 else "小幅")
            if abs(dev) < 0.1:
                parts.append(f"{name} {value:.1f}{spec.unit if spec else ''} 維持正常")
            else:
                parts.append(f"{name} {value:.1f}{spec.unit if spec else ''} {magnitude}{direction}（{band}）")
        return "設備徵兆：" + "；".join(parts) + "。請找出符合此徵兆組合的故障原因與鑑別診斷說明。"

    def _build_candidate(
        self,
        match: _Match,
        confidence: float,
        machine_id: str,
        readings: dict[str, float],
        retrieved,
    ) -> RootCauseCandidate:
        sig = match.signature
        evidence: list[Evidence] = []

        # 1) 感測器證據：貢獻最大的兩個訊號
        contributions = sorted(sig.profile.items(), key=lambda kv: -abs(kv[1]))[:2]
        for name, _ in contributions:
            if name in readings:
                evidence.append(
                    Evidence(
                        source="sensor",
                        reference=f"{machine_id}.{name}",
                        statement=f"{name} 觀測值 {readings[name]:.2f}，符合此故障指紋的主要方向。",
                        weight=abs(match.cosine),
                    )
                )

        # 2) 手冊/SOP 證據
        for ref in sig.manual_refs:
            doc = manual_by_ref(ref)
            if doc is None:
                continue
            first_line = next((p for p in doc.body.split("\n") if "鑑別" in p or "徵兆" in p), doc.body.split("\n")[0])
            evidence.append(Evidence(source=doc.doc_type, reference=ref, statement=first_line.strip(), weight=match.docs))

        # 3) 檢索到的相似段落（RAG 的實際命中）
        for hit in retrieved[:2]:
            if sig.fault_id in hit.chunk.fault_ids:
                evidence.append(
                    Evidence(source=f"rag:{hit.chunk.source}", reference=hit.chunk.chunk_id,
                             statement=hit.chunk.text[:120], weight=hit.score)
                )

        # 4) 歷史案例
        for case in self.ctx.kb.cases_for_fault(sig.fault_id, machine_id, limit=2):
            evidence.append(
                Evidence(source="history", reference=case.case_id,
                         statement=f"{case.days_ago} 天前同機台曾判定為此故障：{case.symptoms}", weight=match.prior)
            )

        model = FAULTS[sig.fault_id]
        actions = [
            f"參照 {ref}" for ref in sig.manual_refs
        ] + [f"準備零件：{'、'.join(model.typical_parts)}", f"預估維修工時 {model.repair_min:g} 分鐘（{model.required_skill}）"]

        return RootCauseCandidate(
            fault_id=sig.fault_id,
            label=sig.label,
            confidence=confidence,
            evidence=evidence,
            recommended_actions=actions,
            scores={
                "cosine": match.cosine,
                "prior": match.prior,
                "docs": match.docs,
                "combined": match.combined,
            },
        )

    # ------------------------------------------------------------------ 敘述
    def _narrate(self, event, candidates: list[RootCauseCandidate], readings: dict[str, float], strength: float) -> str:
        def fallback() -> str:
            if not candidates:
                return "無法從現有證據推論根因。"
            top = candidates[0]
            others = "；".join(f"{c.label} {c.confidence:.0%}" for c in candidates[1:3])
            signal_txt = "、".join(f"{k} {v:.2f}" for k, v in readings.items())
            lines = [
                f"{event.machine_id} 於第 {event.tick} tick（模擬第 {event.sim_minutes:.0f} 分鐘）觸發 "
                f"{event.severity.value.upper()} 異常，觸發條件：{'、'.join(event.triggers)}。",
                f"目前觀測值：{signal_txt}；Agent 估計健康度 {event.health:.0f}。",
                f"根因研判：{top.label}，信心度 {top.confidence:.0%}（訊號強度 {strength:.2f}）。",
                f"主要依據：{top.evidence[0].statement if top.evidence else '感測器指紋比對'}",
            ]
            if others:
                lines.append(f"次要候選：{others}。")
            return "\n".join(lines)

        if self.ctx.llm is None:
            return fallback()
        payload = {
            "machine": event.machine_id,
            "tick": event.tick,
            "severity": event.severity.value,
            "triggers": event.triggers,
            "readings": {k: round(v, 2) for k, v in readings.items()},
            "health_estimate": round(event.health, 1),
            "candidates": [
                {"label": c.label, "confidence": round(c.confidence, 3),
                 "evidence": [e.statement for e in c.evidence[:3]]}
                for c in candidates
            ],
        }
        user = (
            "以下是規則引擎已經算好的診斷結果，請用 4~6 行繁體中文寫成工程師看得懂的根因說明。"
            "必須說明為什麼是這個根因、以及為什麼不是其他候選。不要更動信心度或排名。\n\n"
            f"{payload}"
        )
        response = self.ctx.llm.narrate(SYSTEM_PROMPT, user, fallback, actor=self.name)
        return response.text


__all__ = ["DiagnosisAgent", "W_SIGNATURE", "W_PRIOR", "W_DOCS"]
