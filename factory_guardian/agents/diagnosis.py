"""Diagnosis Agent（規格 §4.2）。

輸入：Sensor、Error Code、Demo Equipment Manual、Maintenance History、SOP。
輸出：Root Cause 候選、信心度與 Evidence。
紅線：**不得讀取 Simulator 的真實故障標籤**。

排名怎麼算出來的（三項加權，權重寫死在程式碼裡，可稽核）：

1. **指紋餘弦相似度（0.75）** —— 主要判準。
   把觀測到的偏離量以各訊號的 scale 正規化成一個向量，和手冊描述的故障指紋比對。
   這是數字比對，比文字相似度可靠得多：軸承劣化的振動主導、冷卻失效的溫度單獨異常、
   馬達過載的電流上升 + 轉速下降，在向量空間裡是分得開的。
   這一項本身又由**感測器指紋**與**聲學指紋**融合而成，見下方「聲音怎麼進來的」。

2. **歷史先驗（0.15）** —— 這台機器過去得過什麼病，近期案例權重較高。

3. **文件支持度（0.10）** —— RAG 檢索到的手冊/SOP/歷史案例對各候選的支持程度。
   權重刻意壓低：所有故障的手冊都會提到同樣那四個訊號，純文字相似度鑑別力有限，
   它的價值在於「產生可引用的 Evidence」，而不是決定排名。

信心度另外會被「訊號強度」壓抑：訊號還很微弱時（剛開始劣化），
即使指紋比對指向某個故障，信心度也不該衝到 90%。

---

## 聲音怎麼進來的（規格 §4.4 的第四個模態）

聲音**不是**第四個加權項，而是併進第 1 項裡：

    cos_fused = (1 − share) × cos_sensor + share × cos_acoustic
    share     = W_ACOUSTIC_SHARE × gate            （gate ∈ [0, 1]）

有效權重因此是 **0.75 × 0.20 = 0.15**（聲音）與 **0.75 × 0.80 = 0.60**（感測器），
歷史先驗與文件支持度完全不動。

### 為什麼是併進去，而不是新開一個加權項

一個新的加權項會把 0.75 / 0.15 / 0.10 這組已經被 Dashboard、稽核軌跡與提案書
引用的數字全部改掉，也會讓「合計 = 各項相加」在畫面上對不起來。融合進第 1 項則是
**加法上封閉**的：`cos_fused` 仍是一個 [−1, 1] 的餘弦值，
`0.75 × cos + 0.15 × prior + 0.10 × docs` 這條式子一個字都不用改，
而拆解明細（`scores.cosine_sensor` / `scores.cosine_acoustic`）照樣可以被稽核。

順帶一個實際的好處：`signal_strength` 仍然只由四個感測器訊號算出，
所以「訊號強度閘」與 Orchestrator 的 `confirm_diagnosis` 門檻語意完全不變。

### 為什麼是 0.20，不是更高

因為在這個 Demo 上，聲音是**推導出來的**，不是量到的。
Digital Twin 的聲學指標由既有的振動／轉速／電流物理算出（見 `acoustics/synthetic.py`），
所以它**補強**證據，不**新增**獨立證據。讓一個推導量的權重逼近真正量到的訊號，
會製造出「兩個獨立來源互相印證」的假象 —— 那正是這個專案最不該做的事。

0.20 的下限則來自它真的補上了一塊鑑別力：冷卻失效與馬達過載**都會推高溫度**，
感測器指紋在這裡分得比較辛苦；但前者是「冷卻泵停了 → 少一個純音」、
後者是「負載上升 → 多出線頻諧波」，兩者的聲學 profile 餘弦是 **−0.89**（幾乎相反）。
權重要大到足以在這種情況下翻轉排名，又要小到不會蓋過真正量到的訊號。

### gate：訊號弱的時候聲音直接棄權

`gate` 由聲學偏離向量的長度決定，低於 `ACOUSTIC_STRENGTH_FULL` 就按比例縮小。
沒有麥克風、或聲音還在正常範圍時 `share = 0`，整條路徑退化成原本的純感測器判斷 ——
**逐位元相同**。這條性質有測試守著（`test_acoustics.py`），它保證多裝一支麥克風
不會在「聲音根本沒說話」的情況下動到任何既有結論。

> ⚠️ Demo 的聲學觀測是**合成**的。偵測器本身的有效性另以外部真實工業錄音驗證
> （DCASE2020 Task2 / MIMII pump，AUC 0.903 / pAUC 0.785），
> 兩者沒有任何資料流往來 —— 見 `docs/acoustic_validation.md`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..acoustics.signatures import (
    INDICATOR_NAMES,
    INDICATOR_UNITS,
    NOMINAL_INDICATORS,
    acoustic_signatures,
)
from ..acoustics.signatures import cosine as acoustic_cosine
from ..acoustics.signatures import deviation_vector as acoustic_deviation
from ..acoustics.signatures import strength as acoustic_strength
from ..domain import (
    AcousticObservation,
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

# --- 聲音模態 -------------------------------------------------------------------------
# 聲學指紋在「指紋餘弦」這一項裡佔的比例（有效權重 = W_SIGNATURE × 這個值 = 0.15）。
# 為什麼是 0.20 而不是更高／更低，見模組說明。
W_ACOUSTIC_SHARE = 0.20
# 聲學偏離向量要多長，聲音才算「完全說話」。
# 0.60 的來由：三個故障裡聲學表現最弱的是馬達過載，它在 progress ≈ 0.5
# （也就是感測器訊號才剛開始踩到警戒線的時候）的聲學偏離長度約 0.6。
# 門檻設在這裡，聲音才會和感測器在同一個時間點開始有發言權，而不是慢半拍。
ACOUSTIC_STRENGTH_FULL = 0.60
# **死區**：偏離量低於這個值時，麥克風完全棄權（share 直接歸零）。
#
# 這條不是保守而已，它是一條**保證**。四個聲學指標各自帶量測雜訊，正規化之後
# 每一維約 N(0, 0.06)，四維合成的偏離長度典型值就有 0.12、尾端可以到 0.25 ——
# 也就是說一台完全健康的機器，它的麥克風「偏離量」永遠不會是零。
# 沒有死區的話，健康機台的雜訊會讓 share 變成一個小的非零值，
# 於是「多裝一支麥克風不會動到既有結論」這句話就只是近似成立而不是成立。
# 設在 0.25（約雜訊分布的尾端），讓這句話變成可以被測試逐位元驗證的性質。
ACOUSTIC_STRENGTH_FLOOR = 0.25


@dataclass
class _Match:
    signature: FaultSignature
    cosine: float                 # 融合後的指紋餘弦（實際進入排名的那一個）
    cosine_sensor: float          # 感測器指紋餘弦
    cosine_acoustic: float        # 聲學指紋餘弦（沒有麥克風時為 0）
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

            # 聲音模態：合成麥克風觀測（Agent 側只看得到四個指標，看不到任何故障標籤）。
            observation = snapshot.machines[machine_id].acoustics
            with self.tool("acoustic.fingerprint_match", f"machine={machine_id} mic={observation is not None}"):
                acoustic = self._acoustic_match(observation, list(cosines))

            with self.tool("history.machine_fault_prior", f"machine={machine_id}"):
                priors = self.ctx.kb.machine_fault_prior(machine_id, list(cosines))

            query = self._build_query(machine, readings, observed)
            with self.tool("rag.search", f"chars={len(query)}"):
                docs = self.ctx.kb.fault_affinity(query, list(cosines))
                retrieved = self.ctx.kb.search(query, top_k=6, machine_id=machine_id)

            share = float(acoustic["share"])
            matches = []
            for sig in self.signatures:
                sensor_cos = cosines[sig.fault_id]
                mic_cos = float(acoustic["cosines"].get(sig.fault_id, 0.0))
                # 融合：share = 0 時逐位元退化成原本的純感測器判斷。
                fused = (1.0 - share) * sensor_cos + share * mic_cos
                matches.append(
                    _Match(
                        signature=sig,
                        cosine=fused,
                        cosine_sensor=sensor_cos,
                        cosine_acoustic=mic_cos,
                        prior=priors.get(sig.fault_id, 0.0),
                        docs=docs.get(sig.fault_id, 0.0),
                        combined=(
                            W_SIGNATURE * max(0.0, fused)
                            + W_PRIOR * priors.get(sig.fault_id, 0.0)
                            + W_DOCS * docs.get(sig.fault_id, 0.0)
                        ),
                    )
                )
            confidences = self._confidences(matches, strength)

            candidates = [
                self._build_candidate(
                    m, confidences[m.signature.fault_id], machine_id, readings, retrieved, acoustic
                )
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
            # signature / prior / docs 這三個鍵維持原值，Dashboard 的
            # 「合計 = 各項相加」因此仍然成立；聲音的拆解放在額外的兩個鍵裡。
            weights={
                "signature": W_SIGNATURE,
                "prior": W_PRIOR,
                "docs": W_DOCS,
                "signature_sensor": W_SIGNATURE * (1.0 - share),
                "signature_acoustic": W_SIGNATURE * share,
            },
            thresholds={
                "strength_full": STRENGTH_FULL,
                "no_fault": NO_FAULT_STRENGTH,
                "acoustic_strength_floor": ACOUSTIC_STRENGTH_FLOOR,
                "acoustic_strength_full": ACOUSTIC_STRENGTH_FULL,
            },
            observations=self._observations(machine, readings, observed),
            acoustics=acoustic,
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
                    "cosine_sensor": round(m.cosine_sensor, 3),
                    "cosine_acoustic": round(m.cosine_acoustic, 3),
                    "prior": round(m.prior, 3),
                    "docs": round(m.docs, 3),
                    "combined": round(m.combined, 3),
                    "confidence": round(confidences[m.signature.fault_id], 3),
                }
                for m in matches
            },
            acoustic_share=round(share, 3),
            top=diagnosis.top.fault_id if diagnosis.top else None,
            latency_ms=round(diagnosis.latency_ms, 1),
            note=(
                "未讀取 Simulator ground truth；排名由數值比對決定，LLM 僅寫敘述。"
                "聲學指標為合成音訊特徵（由振動物理推導），非真實錄音。"
            ),
        )
        return diagnosis

    # ------------------------------------------------------------------ 聲音模態
    @staticmethod
    def _acoustic_match(
        observation: AcousticObservation | None, fault_ids: list[str]
    ) -> dict[str, object]:
        """把麥克風觀測比對成每個故障候選的聲學餘弦，並算出這次該給聲音多少發言權。

        回傳的 dict 直接掛到 ``Diagnosis.acoustics``，所以它同時是「推理明細」——
        評審問「聲音到底貢獻了什麼」時，答案就在這一包裡，不需要另外解釋。

        沒有麥克風、或聲音還在正常範圍內時 ``share = 0``，融合式退化成
        ``cos_fused = cos_sensor``，既有行為**逐位元不變**。
        """
        empty: dict[str, object] = {
            "available": False,
            "share": 0.0,
            "gate": 0.0,
            "strength": 0.0,
            "effective_weight": 0.0,
            "cosines": {fid: 0.0 for fid in fault_ids},
            "indicators": {},
            "deviations": {},
            "synthetic": True,
            "note": "本機台未配置麥克風，或聲學偏離量為零。",
        }
        if observation is None:
            return empty

        indicators = observation.indicators
        deviations = acoustic_deviation(indicators)
        strength = acoustic_strength(deviations)
        # 死區之下完全棄權；之上再線性開到 1.0。
        span = max(ACOUSTIC_STRENGTH_FULL - ACOUSTIC_STRENGTH_FLOOR, 1e-9)
        gate = max(0.0, min(1.0, (strength - ACOUSTIC_STRENGTH_FLOOR) / span))
        if gate <= 0.0:
            return {**empty, "available": True, "sensor_id": observation.sensor_id,
                    "strength": strength, "indicators": {n: indicators[n] for n in INDICATOR_NAMES},
                    "deviations": deviations, "nominal": dict(NOMINAL_INDICATORS),
                    "units": dict(INDICATOR_UNITS), "synthetic": bool(observation.synthetic),
                    "provenance": observation.provenance,
                    "note": "聲學偏離量在量測雜訊範圍內，麥克風本次棄權（share = 0，排名完全由感測器決定）。"}
        share = W_ACOUSTIC_SHARE * gate
        signatures = acoustic_signatures()
        return {
            "available": True,
            "sensor_id": observation.sensor_id,
            "strength": strength,
            "gate": gate,
            "share": share,
            # 聲音在「整體排名」裡真正拿到的權重，寫出來省得別人自己乘。
            "effective_weight": W_SIGNATURE * share,
            "cosines": {
                fid: (acoustic_cosine(deviations, signatures[fid].profile) if fid in signatures else 0.0)
                for fid in fault_ids
            },
            "indicators": {name: indicators[name] for name in INDICATOR_NAMES},
            "nominal": dict(NOMINAL_INDICATORS),
            "deviations": deviations,
            "units": dict(INDICATOR_UNITS),
            "synthetic": bool(observation.synthetic),
            "provenance": observation.provenance,
            "note": (
                "SYNTHETIC DEMO AUDIO — 合成音訊特徵（由振動物理推導），非真實錄音。"
                "偵測器本身以 DCASE2020/MIMII 真實工業錄音另行驗證，見 docs/acoustic_validation.md。"
            ),
        }

    @staticmethod
    def _acoustic_evidence(
        acoustic: dict[str, object], fault_id: str, machine_id: str
    ) -> Evidence | None:
        """把聲學比對結果寫成一條可引用的證據。

        只在聲音真的有發言權（gate 開啟）且方向一致（餘弦為正）時才產生 ——
        「聲音沒說話」不該被包裝成一條證據。
        """
        if not acoustic.get("available") or float(acoustic.get("share", 0.0)) <= 1e-9:
            return None
        cos = float(acoustic["cosines"].get(fault_id, 0.0))  # type: ignore[union-attr]
        if cos <= 0.0:
            return None
        deviations: dict[str, float] = acoustic["deviations"]      # type: ignore[assignment]
        indicators: dict[str, float] = acoustic["indicators"]      # type: ignore[assignment]
        # 挑偏離最大的那個指標來講，理由和 _observations 排序一樣：主導判斷的先講。
        name = max(deviations, key=lambda k: abs(deviations[k]))
        direction = "上升" if deviations[name] > 0 else "下降"
        unit = INDICATOR_UNITS.get(name, "")
        return Evidence(
            source="acoustic",
            reference=f"{machine_id}.{acoustic.get('sensor_id', 'MIC')}",
            statement=(
                f"麥克風 {name} 為 {indicators[name]:.2f}{unit}（正常 {NOMINAL_INDICATORS[name]:.2f}{unit}，"
                f"{direction}），與此故障的聲學指紋餘弦 {cos:.2f}。"
                "［合成音訊特徵，非真實錄音］"
            ),
            weight=cos * float(acoustic["effective_weight"]),
        )

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
        acoustic: dict[str, object] | None = None,
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
                        weight=abs(match.cosine_sensor),
                    )
                )

        # 1b) 聲學證據（合成音訊特徵；聲音沒發言權時不會產生）
        if acoustic is not None:
            mic_evidence = self._acoustic_evidence(acoustic, sig.fault_id, machine_id)
            if mic_evidence is not None:
                evidence.append(mic_evidence)

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
                # "cosine" 是實際進入排名的融合值 —— Dashboard 顯示的
                # 「指紋餘弦 × 0.75」用的就是它，所以合計仍然對得起來。
                "cosine": match.cosine,
                "cosine_sensor": match.cosine_sensor,
                "cosine_acoustic": match.cosine_acoustic,
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


__all__ = [
    "ACOUSTIC_STRENGTH_FLOOR",
    "ACOUSTIC_STRENGTH_FULL",
    "DiagnosisAgent",
    "NO_FAULT_ID",
    "W_ACOUSTIC_SHARE",
    "W_DOCS",
    "W_PRIOR",
    "W_SIGNATURE",
]
