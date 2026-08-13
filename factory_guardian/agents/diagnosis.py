"""Diagnosis Agent（規格 §4.2）。

輸入：Sensor、Error Code、Demo Equipment Manual、Maintenance History、SOP、
      設備安裝驗收記錄（As-Commissioned Baseline）。
輸出：Root Cause 候選、信心度與 Evidence。
紅線：**不得讀取 Simulator 的真實故障標籤，也不得讀取模擬器的故障參數。**

判斷流程（三步，全部可稽核）：

**第一步：以「這台機器自己的交機基準」算偏離量。**
不是拿讀值去比通用門檻，而是比 ``COMM-M-x`` 驗收記錄裡的本機基準值。
同型號的 M-A（振動基準 1.9）與 M-B（2.6）判讀標準本來就不同 ——
同樣讀到 3.6 mm/s，對 M-A 是明顯劣化，對 M-B 還在正常散布內。
這是真實預測性維護的做法，也讓「機器個體差異」成為 Agent 必須處理的問題。

**第二步：比對手冊徵兆區間 + 鑑別診斷規則。**

1. **鑑別診斷規則（0.44）** —— 手冊「關鍵鑑別點」那一段的結構化版本，
   用**訊號之間的比值**分辨徵兆重疊的故障（ΔVib/ΔTemp、ΔRPM/ΔCurrent）。
   權重最高，因為這是唯一真正有鑑別力的一項：三種故障都會溫升、
   徵兆區間大幅重疊，絕對值分不開；比值才是工程師真正的判準。

2. **手冊區間符合度（0.38）** —— 每個訊號的變化量落在 ``MAN-A-x.x`` 描述的
   區間內嗎？逐項打分再依手冊標註的訊號權重加權。手冊寫的是區間不是點值，
   且區間中心刻意偏離模擬器參數（見 ``knowledge/symptom_spec.py``）——
   Agent 拿到的是「設備商用他們的試驗機寫的經驗值」，不是這台機器的答案。

3. **歷史先驗（0.10）** —— 這台機器過去得過什麼病，近期案例權重較高。

4. **文件支持度（0.08）** —— RAG 檢索的文字相似度。權重最低：
   所有故障的手冊都提到同樣那四個訊號，純文字鑑別力有限，
   它的價值在於「產生可引用的 Evidence」，而不是決定排名。

**第三步：信心度被「訊號強度」壓抑。** 訊號還很微弱時（剛開始劣化），
即使區間比對指向某個故障，信心度也不該衝到 90%。

⚠ 為什麼不用餘弦相似度了：舊版把模擬器的 ``FAULTS[].deltas`` 除以 scale 當指紋，
但模擬器產生訊號用的是同一份 deltas，觀測向量與指紋共線 ——
餘弦對純量免疫，正確答案的餘弦恆為 1.0。那是查表，不是診斷。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..domain import (
    AnomalyEvent,
    Diagnosis,
    Evidence,
    FactorySnapshot,
    RootCauseCandidate,
)
from ..knowledge.commissioning import CommissioningRecord, commissioning_for
from ..knowledge.corpus import manual_by_ref
from ..knowledge.symptom_spec import SYMPTOM_SPECS, FaultSymptomSpec
from ..llm import SYSTEM_PROMPT
from ..twin.faults import FAULTS
from .base import Agent

W_MANUAL = 0.38        # 手冊徵兆區間符合度
W_DIFFERENTIAL = 0.44  # 鑑別診斷規則（訊號比值）—— 權重最高
W_PRIOR = 0.10         # 歷史先驗
W_DOCS = 0.08          # RAG 文字支持度
SOFTMAX_TEMPERATURE = 0.16
# 訊號強度低於這個值時，視為訊號太弱，信心度往均勻分布拉。
STRENGTH_FULL = 1.2
# 偏離量低於這個值時，判定為「沒有設備故障徵兆」。
NO_FAULT_STRENGTH = 0.45
NO_FAULT_ID = "no_equipment_fault"


@dataclass
class _Match:
    spec: FaultSymptomSpec
    manual: float
    differential: float
    prior: float
    docs: float
    combined: float
    # 逐項明細，供前端顯示「哪個訊號落在區間內、哪條鑑別規則成立」
    range_hits: list[dict[str, object]]
    diff_hits: list[dict[str, object]]


class DiagnosisAgent(Agent):
    name = "diagnosis-agent"
    role = "根因分析"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.specs: list[FaultSymptomSpec] = []

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
        self.specs = [s for fid, s in SYMPTOM_SPECS.items() if fid in FAULTS]

        readings = smoothed or {name: r.value for name, r in event.readings.items()}

        # 第一步：以「這台機器自己的交機基準」算變化量，而不是通用 nominal。
        commissioning = commissioning_for(machine_id)
        with self.tool("commissioning.baseline", f"machine={machine_id}"):
            deltas = self._baseline_deltas(machine, readings, commissioning)
        observed = self._deviation_vector(machine, readings, commissioning)
        strength = math.sqrt(sum(v * v for v in observed.values()))

        with self.timed() as timing:
            # 第二步：手冊徵兆區間比對 + 鑑別診斷規則。
            with self.tool("manual.symptom_ranges", f"faults={len(self.specs)}"):
                range_scores = {s.fault_id: self._range_score(s, deltas) for s in self.specs}
            with self.tool("manual.differential_rules", f"machine={machine_id}"):
                diff_scores = {s.fault_id: self._differential_score(s, deltas) for s in self.specs}

            with self.tool("history.machine_fault_prior", f"machine={machine_id}"):
                priors = self.ctx.kb.machine_fault_prior(machine_id, list(range_scores))

            query = self._build_query(machine, readings, observed)
            with self.tool("rag.search", f"chars={len(query)}"):
                docs = self.ctx.kb.fault_affinity(query, list(range_scores))
                retrieved = self.ctx.kb.search(query, top_k=6, machine_id=machine_id)

            matches = []
            for spec in self.specs:
                fid = spec.fault_id
                manual_score, range_hits = range_scores[fid]
                diff_score, diff_hits = diff_scores[fid]
                matches.append(
                    _Match(
                        spec=spec,
                        manual=manual_score,
                        differential=diff_score,
                        prior=priors.get(fid, 0.0),
                        docs=docs.get(fid, 0.0),
                        combined=(
                            W_MANUAL * manual_score
                            + W_DIFFERENTIAL * diff_score
                            + W_PRIOR * priors.get(fid, 0.0)
                            + W_DOCS * docs.get(fid, 0.0)
                        ),
                        range_hits=range_hits,
                        diff_hits=diff_hits,
                    )
                )
            confidences = self._confidences(matches, strength)

            candidates = [
                self._build_candidate(m, confidences[m.spec.fault_id], machine_id, readings, retrieved, commissioning)
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
            weights={
                "manual": W_MANUAL,
                "differential": W_DIFFERENTIAL,
                "prior": W_PRIOR,
                "docs": W_DOCS,
            },
            thresholds={"strength_full": STRENGTH_FULL, "no_fault": NO_FAULT_STRENGTH},
            observations=self._observations(machine, readings, observed, deltas, commissioning),
            baseline_ref=commissioning.doc_id if commissioning else "",
            summary_points=self._summary_points(event, candidates, readings, strength),
        )
        self.log(
            "diagnose",
            event_id=event.event_id,
            machine_id=machine_id,
            machine_state=machine_state,
            signal_strength=round(strength, 3),
            baseline_ref=commissioning.doc_id if commissioning else None,
            scores={
                m.spec.fault_id: {
                    "manual": round(m.manual, 3),
                    "differential": round(m.differential, 3),
                    "prior": round(m.prior, 3),
                    "docs": round(m.docs, 3),
                    "combined": round(m.combined, 3),
                    "confidence": round(confidences[m.spec.fault_id], 3),
                }
                for m in matches
            },
            top=diagnosis.top.fault_id if diagnosis.top else None,
            latency_ms=round(diagnosis.latency_ms, 1),
            note=(
                "未讀取 Simulator ground truth 或故障參數；"
                "排名由本機交機基準 + 手冊區間 + 鑑別規則決定，LLM 僅寫敘述。"
            ),
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
    def _observations(
        machine,
        readings: dict[str, float],
        observed: dict[str, float],
        deltas: dict[str, float],
        commissioning: CommissioningRecord | None,
    ) -> list[dict[str, object]]:
        """Agent 這一步「看到什麼」：觀測值、本機交機基準、相對基準的變化量。

        `nominal` 欄位刻意送「本機交機基準」而不是通用 nominal ——
        畫面上要讓評審看到判讀基準是這台機器自己的驗收值，
        以及同型號的 M-A / M-B 基準本來就不同。
        """
        rows: list[tuple[float, dict[str, object]]] = []
        for spec in machine.signals:
            value = readings.get(spec.name)
            if value is None:
                continue
            base = commissioning.baseline_of(spec.name) if commissioning else None
            reference = base.baseline if base else spec.nominal
            rows.append(
                (
                    observed.get(spec.name, 0.0),
                    {
                        "name": spec.name,
                        "unit": spec.unit,
                        "value": round(value, 2),
                        "nominal": round(reference, 2),
                        "baseline_tolerance": round(base.tolerance, 2) if base else None,
                        "delta": round(deltas.get(spec.name, 0.0), 2),
                        "deviation": round(observed.get(spec.name, 0.0), 2),
                        "band": spec.band(value).value,
                    },
                )
            )
        # 偏離量大的排前面：主導這次判斷的訊號要先被看到。
        rows.sort(key=lambda row: -abs(row[0]))
        return [row for _, row in rows]

    @staticmethod
    def _baseline_deltas(
        machine, readings: dict[str, float], commissioning: CommissioningRecord | None
    ) -> dict[str, float]:
        """相對**本機交機基準**的絕對變化量（原始單位，不正規化）。

        手冊的徵兆區間也是用原始單位寫的（「振動 +1.8~+9.2 mm/s」），
        所以這裡不做 scale 正規化 —— 兩邊必須在同一個單位空間才能比。
        沒有驗收記錄時退回通用 nominal。
        """
        out: dict[str, float] = {}
        for spec in machine.signals:
            value = readings.get(spec.name)
            if value is None:
                continue
            base = commissioning.baseline_of(spec.name) if commissioning else None
            reference = base.baseline if base else spec.nominal
            out[spec.name] = value - reference
        return out

    @staticmethod
    def _deviation_vector(
        machine, readings: dict[str, float], commissioning: CommissioningRecord | None = None
    ) -> dict[str, float]:
        """正規化偏離向量，僅用於「訊號強度」與檢索查詢排序。

        以本機驗收允收帶正規化：允收帶內算 0，超出才開始累積。
        這比用固定 scale 更貼近現場 —— 量測散布大的機器本來就需要更大的變化才算異常。
        """
        vector: dict[str, float] = {}
        for spec in machine.signals:
            value = readings.get(spec.name)
            if value is None:
                continue
            base = commissioning.baseline_of(spec.name) if commissioning else None
            if base is not None:
                signed = 1.0 if value >= base.baseline else -1.0
                vector[spec.name] = signed * base.deviation_ratio(value)
            else:
                vector[spec.name] = (value - spec.nominal) / spec.scale
        return vector

    @staticmethod
    def _range_score(
        spec: FaultSymptomSpec, deltas: dict[str, float]
    ) -> tuple[float, list[dict[str, object]]]:
        """手冊徵兆區間符合度：每個訊號的變化量落在手冊區間內嗎？

        逐項打分後依手冊標註的訊號權重加權平均。回傳明細供前端逐項顯示。
        """
        total_weight = 0.0
        acc = 0.0
        hits: list[dict[str, object]] = []
        for rng in spec.ranges:
            if rng.signal not in deltas:
                continue
            delta = deltas[rng.signal]
            score = rng.score(delta)
            acc += score * rng.weight
            total_weight += rng.weight
            hits.append(
                {
                    "signal": rng.signal,
                    "delta": round(delta, 2),
                    "low": rng.low,
                    "high": rng.high,
                    "unit": rng.unit,
                    "weight": rng.weight,
                    "score": round(score, 3),
                    "inside": rng.low <= delta <= rng.high,
                }
            )
        return (acc / total_weight if total_weight else 0.0), hits

    @staticmethod
    def _differential_score(
        spec: FaultSymptomSpec, deltas: dict[str, float]
    ) -> tuple[float, list[dict[str, object]]]:
        """鑑別診斷規則：用訊號比值分辨徵兆重疊的故障。

        分母訊號沒動時該規則不適用（回傳 None），不計入平均 ——
        不能因為「溫度沒變」就把一條溫度相關的鑑別規則算成 0 分。
        全部規則都不適用時回傳 0.5（中性），避免無資訊變成懲罰。
        """
        total_weight = 0.0
        acc = 0.0
        hits: list[dict[str, object]] = []
        for rule in spec.differentials:
            score = rule.score(deltas)
            applicable = score is not None
            if applicable:
                acc += score * rule.weight
                total_weight += rule.weight
            hits.append(
                {
                    "rule": f"Δ{rule.numerator}/Δ{rule.denominator}",
                    "expr": rule.describe(deltas),
                    "statement": rule.statement,
                    "score": round(score, 3) if applicable else None,
                    "applicable": applicable,
                }
            )
        return (acc / total_weight if total_weight else 0.5), hits

    @staticmethod
    def _confidences(matches: list[_Match], strength: float) -> dict[str, float]:
        """Softmax 轉信心度，再依訊號強度往均勻分布拉。"""
        if not matches:
            return {}
        top = max(m.combined for m in matches)
        exps = {m.spec.fault_id: math.exp((m.combined - top) / SOFTMAX_TEMPERATURE) for m in matches}
        total = sum(exps.values()) or 1.0
        sharp = {fid: value / total for fid, value in exps.items()}
        uniform = 1.0 / len(matches)
        # 訊號越弱，越往均勻分布靠 —— 早期劣化本來就不該給高信心。
        certainty = max(0.0, min(1.0, strength / STRENGTH_FULL))
        return {fid: uniform + (value - uniform) * certainty for fid, value in sharp.items()}

    def _build_query(self, machine, readings: dict[str, float], observed: dict[str, float]) -> str:
        """依偏離程度排序組出檢索查詢，讓最異常的訊號主導召回。

        措辭必須忠實於訊號的 band。早期版本只看正規化偏離量就給出「明顯上升」，
        結果一個 vibration 2.9 mm/s（低於警告門檻、band 仍是 normal）的
        典型冷卻失效，查詢裡會寫成「vibration 明顯上升」——
        那句話幾乎照抄軸承手冊的「Vibration 先明顯上升」，
        於是文字相似度把軸承劣化推到 1.0，反而壓過真正的答案。

        檢索查詢是 Agent 自己寫的問題；問錯了，召回再準也沒有意義。
        現在 normal band 一律描述為「維持正常範圍」，
        只有真正進入 warning/critical 的訊號才會被說成上升或下降。
        """
        ranked = sorted(observed.items(), key=lambda kv: -abs(kv[1]))
        parts: list[str] = []
        for name, dev in ranked:
            spec = machine.signal(name)
            value = readings.get(name, 0.0)
            band = spec.band(value).value if spec else "?"
            unit = spec.unit if spec else ""
            # band 正常 = 這個訊號沒有異常，不管正規化偏離量算出多少。
            if band == "normal" or abs(dev) < 0.1:
                parts.append(f"{name} {value:.1f}{unit} 維持正常範圍")
                continue
            direction = "上升" if dev > 0 else "下降"
            magnitude = "大幅" if abs(dev) >= 1.0 else "明顯"
            parts.append(f"{name} {value:.1f}{unit} {magnitude}{direction}（{band}）")
        return "設備徵兆：" + "；".join(parts) + "。請找出符合此徵兆組合的故障原因與鑑別診斷說明。"

    def _build_candidate(
        self,
        match: _Match,
        confidence: float,
        machine_id: str,
        readings: dict[str, float],
        retrieved,
        commissioning: CommissioningRecord | None,
    ) -> RootCauseCandidate:
        spec = match.spec
        model = FAULTS[spec.fault_id]
        evidence: list[Evidence] = []

        # 1) 交機基準證據：判讀基準是這台機器自己的驗收值
        if commissioning is not None:
            top_hit = next((h for h in match.range_hits if h["inside"]), None)
            if top_hit is not None:
                base = commissioning.baseline_of(str(top_hit["signal"]))
                if base is not None:
                    evidence.append(
                        Evidence(
                            source="commissioning",
                            reference=commissioning.doc_id,
                            statement=(
                                f"{top_hit['signal']} 相對本機交機基準 {base.baseline:g}{base.unit} "
                                f"變化 {float(top_hit['delta']):+.2f}，落在手冊區間 "
                                f"{top_hit['low']:+g}~{top_hit['high']:+g} 內。"
                            ),
                            weight=match.manual,
                        )
                    )

        # 2) 手冊區間比對：逐項符合度（排名的主要依據）
        inside = [h for h in match.range_hits if h["inside"]]
        if inside:
            summary = "、".join(
                f"{h['signal']} {float(h['delta']):+.2f}{h['unit']}" for h in inside[:3]
            )
            evidence.append(
                Evidence(
                    source="manual",
                    reference=spec.manual_ref,
                    statement=f"符合手冊徵兆區間的訊號：{summary}（符合度 {match.manual:.0%}）。",
                    weight=match.manual,
                )
            )

        # 3) 鑑別診斷規則：手冊「關鍵鑑別點」的結構化版本
        for hit in match.diff_hits:
            if hit["applicable"] and float(hit["score"] or 0.0) >= 0.5:
                evidence.append(
                    Evidence(
                        source="differential",
                        reference=spec.manual_ref,
                        statement=f"{hit['expr']}：{hit['statement']}",
                        weight=match.differential,
                    )
                )

        # 4) SOP 證據
        for ref in model.manual_refs:
            doc = manual_by_ref(ref)
            if doc is None or doc.doc_type != "sop":
                continue
            evidence.append(
                Evidence(source=doc.doc_type, reference=ref, statement=doc.title, weight=match.docs)
            )

        # 5) 檢索到的相似段落（RAG 的實際命中）
        for hit in retrieved[:2]:
            if spec.fault_id in hit.chunk.fault_ids:
                evidence.append(
                    Evidence(source=f"rag:{hit.chunk.source}", reference=hit.chunk.chunk_id,
                             statement=hit.chunk.text[:120], weight=hit.score)
                )

        # 6) 歷史案例
        for case in self.ctx.kb.cases_for_fault(spec.fault_id, machine_id, limit=2):
            evidence.append(
                Evidence(source="history", reference=case.case_id,
                         statement=f"{case.days_ago} 天前同機台曾判定為此故障：{case.symptoms}", weight=match.prior)
            )

        actions = [
            f"參照 {ref}" for ref in model.manual_refs
        ] + [f"準備零件：{'、'.join(model.typical_parts)}", f"預估維修工時 {model.repair_min:g} 分鐘（{model.required_skill}）"]

        return RootCauseCandidate(
            fault_id=spec.fault_id,
            label=spec.label,
            confidence=confidence,
            evidence=evidence,
            recommended_actions=actions,
            scores={
                "manual": match.manual,
                "differential": match.differential,
                "prior": match.prior,
                "docs": match.docs,
                "combined": match.combined,
            },
            range_hits=match.range_hits,
            diff_hits=match.diff_hits,
        )

    # ------------------------------------------------------------------ 敘述
    @staticmethod
    def _summary_points(
        event, candidates: list[RootCauseCandidate], readings: dict[str, float], strength: float
    ) -> list[dict[str, object]]:
        """敘述的結構化版本 —— 和 ``_narrate`` 的 fallback 講同一組事實。

        差別在於這裡不組句子，而是回傳 label/value/detail 三欄，
        讓前端排成條列。數字一律在這裡格式化好，前端不重算。
        """
        if not candidates:
            return [{"label": "根因研判", "value": "無法推論", "detail": "現有證據不足以指向任何候選根因。"}]

        top = candidates[0]
        points: list[dict[str, object]] = [
            {
                "label": "觸發事件",
                "value": f"{event.machine_id} / TICK {event.tick}",
                "detail": (
                    f"模擬第 {event.sim_minutes:.0f} 分鐘觸發 {event.severity.value.upper()} 異常，"
                    f"觸發條件：{'、'.join(event.triggers)}。"
                ),
            },
            {
                "label": "健康度",
                "value": f"{event.health:.0f}",
                "detail": f"訊號強度 {strength:.2f}（偏離向量長度）。",
                "tone": "alarm" if event.health < 50 else "",
            },
            {
                "label": "根因研判",
                "value": f"{top.label} {top.confidence:.0%}",
                "detail": (
                    top.evidence[0].statement if top.evidence else "依手冊徵兆區間與鑑別診斷規則比對得出。"
                ),
                "tone": "key",
            },
        ]

        others = candidates[1:3]
        if others:
            points.append(
                {
                    "label": "次要候選",
                    "value": "、".join(f"{c.label} {c.confidence:.0%}" for c in others),
                    "detail": "信心度明顯低於主判定，僅供交叉確認。",
                }
            )
        return points

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
                f"主要依據：{top.evidence[0].statement if top.evidence else '手冊徵兆區間與鑑別診斷規則比對'}",
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


__all__ = ["DiagnosisAgent", "W_MANUAL", "W_DIFFERENTIAL", "W_PRIOR", "W_DOCS"]
