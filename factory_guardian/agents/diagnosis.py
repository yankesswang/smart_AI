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
> 兩者沒有任何資料流往來 —— 見 `docs/factory_guardian/acoustic_validation.md`。

---

## 一個故障可以有多個指紋（多原型）

原本每個故障只有一個方向向量。外部驗證量到了這個假設的代價
（`docs/factory_guardian/external_validation.md` §8.2）：AI4I 2020 的 PWF 是**雙側**故障
（功率過低**或**過高），單一質心落在兩簇中間、方向失去意義，recall 只有 0.388。
改成每模式 2 個原型、取最大餘弦後，PWF recall 0.788、整體 Top-1 0.724 → **0.821**。

真實設備上是同一件事：馬達過載與失載都是動力異常、冷卻迴路可以不足也可以過度。
所以 `FaultSignature.profile` 保留為主原型（既有數字全綁在它身上），
額外方向放進 `alt_prototypes`，比對時取最大餘弦。只有主原型的故障
（例如 `bearing_degradation`）行為**逐位元不變**。

命中哪一個原型會一路寫進 `scores.prototype`（數字索引）、稽核 log
（`scores.<fault>.prototype`，名稱）與 Evidence 文字 —— 排名被哪個方向拿下，
說明就必須講同一個故事。

---

## 判別式接手層（`DiscriminativeReranker`）

`docs/factory_guardian/external_validation.md` §8.3 的實測：同樣特徵下 LogisticRegression 的
歸因 Top-1 是 0.964，指紋餘弦法是 0.724。誠實的結論是「有標註歷史時該加一層判別式模型」，
但那**不會**取代指紋法 —— 兩者需要的輸入不同（標註樣本 vs 手冊徵兆）。

因此這裡的定位是：**指紋 `combined` 仍是主排名**，判別式模型只在
「同機台帶讀值的標註案例 ≥ `MIN_LABELLED_CASES`」時，以一個固定權重加進合分：

    combined = 0.75 × cos_fused + 0.15 × prior + 0.10 × docs + w_rerank × P(fault)

**預設 `w_rerank = 0.0` 且 `DiagnosisAgent` 不掛載這一層**，此時上式的第四項
根本不會被執行，行為逐位元等同改動前。啟用與否、為什麼沒啟用（缺 scikit-learn、
案例不足、只有單一類別），全部寫進 `Diagnosis.reranker` 與稽核 log ——
降級必須被看見，不能靜默。
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
    PRIMARY_PROTOTYPE,
    AcousticObservation,
    AnomalyEvent,
    Diagnosis,
    Evidence,
    FactorySnapshot,
    FaultSignature,
    RootCauseCandidate,
)
from ..knowledge.corpus import MAINTENANCE_HISTORY, manual_by_ref
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
# --- 判別式接手層 ---------------------------------------------------------------------
# 同一台機台要累積到幾筆「帶感測器讀值的標註案例」，判別式模型才准參與排名。
# 8 筆的來由：這是一個 3 類別、4 維特徵的多項式 LogisticRegression，
# 每類平均不到 3 筆時它學到的只是雜訊，而排名一旦被雜訊推動就再也解釋不了。
# 這個門檻同時是對評審的承諾：**新產線第一天不會有這一層**，指紋法自己撐冷啟動。
MIN_LABELLED_CASES = 8
# 判別式模型在合分裡的固定權重。**預設 0.0 = 完全不參與**。
# 為什麼預設是 0：`docs/factory_guardian/external_validation.md` §8.3 的結論是「有標註歷史時應該加這一層」，
# 但 Demo 的維修歷史是合成的，用它去推動排名等於用自己編的資料證明自己。
# 所以程式路徑先建好、可稽核、可開啟，權重留給有真實標註歷史的場域再調。
W_RERANK = 0.0

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
    # 感測器餘弦是被哪一個原型拿下的（0 = 主原型）。單原型故障永遠是 0。
    prototype_index: int = 0
    prototype_name: str = PRIMARY_PROTOTYPE
    # 判別式接手層給這個候選的機率；沒啟用時為 0.0，且不會進入 combined。
    rerank: float = 0.0


# =====================================================================================
# 判別式接手層
# =====================================================================================
class DiscriminativeReranker:
    """有標註歷史時，加一層判別式模型參與排名。指紋法仍然是主排名與解釋來源。

    ## 為什麼有這一層

    `docs/factory_guardian/external_validation.md` §8.3 的實測結論：在 AI4I 2020 上，同樣的特徵下
    LogisticRegression 的歸因 Top-1 是 0.964，指紋餘弦法是 0.724，而且學習曲線顯示
    這個差距在「每個模式只有 1 筆標註」時就已經存在。誠實的結論是 ——
    **在已經累積標註故障歷史的產線上，判別式模型該被加進來。**

    ## 為什麼它不取代指紋法

    兩者需要的輸入不同。判別式模型需要每個故障模式的標註樣本；新產線、新設備、罕見故障，
    樣本就是不存在。指紋法只需要手冊上的徵兆描述，設備進廠第一天就有。
    所以這裡的定位是**接手**不是取代：指紋 `combined` 仍是主排名，
    這一層只在同機台標註案例夠多時，以一個固定權重加進合分。

    ## 三條紅線

    1. **預設不參與**（`W_RERANK = 0.0`，且 `DiagnosisAgent` 預設 `reranker=None`）。
       權重為 0 或沒有實例時，`combined` 的算式一個字都不會被碰到，行為逐位元不變。
    2. **降級必須被看見**。沒有 scikit-learn、同機台案例不足、只有單一類別 ——
       任何一種情況都會在 `status()` 留下 `enabled=False` 與 `reason`，
       並被寫進 `Diagnosis.reranker` 與稽核 log。靜默跳過等於騙人。
    3. **不讀 ground truth**。訓練資料是 `knowledge/corpus.py` 的維修歷史
       （＝人類技師事後寫下的判定），不是 Simulator 的故障標籤。

    ## 訓練資料

    `MAINTENANCE_HISTORY` 中帶 `readings` 的案例，特徵與指紋法**完全相同**：
    正規化偏離向量 `(值 − nominal) / scale`。特徵相同才比得出「方法」的差別，
    而不是比特徵工程。
    """

    def __init__(self, weight: float = W_RERANK, min_cases: int = MIN_LABELLED_CASES) -> None:
        self.weight = float(weight)
        self.min_cases = int(min_cases)
        self._cache: dict[str, dict[str, object]] = {}

    # ------------------------------------------------------------------ 可用性
    @staticmethod
    def sklearn_available() -> bool:
        try:
            import sklearn  # noqa: F401
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------ 主入口
    def probabilities(
        self, machine, machine_id: str, readings: dict[str, float], fault_ids: list[str]
    ) -> dict[str, object]:
        """回傳這一層的完整狀態（含機率）。永遠回傳一個 dict，不會丟例外。

        鍵：``enabled`` / ``reason`` / ``weight`` / ``probabilities`` / ``cases`` /
        ``sklearn`` / ``min_cases`` / ``synthetic_training_data``。
        """
        base: dict[str, object] = {
            "enabled": False,
            "weight": 0.0,
            "configured_weight": self.weight,
            "min_cases": self.min_cases,
            "sklearn": self.sklearn_available(),
            "cases": 0,
            "probabilities": {fid: 0.0 for fid in fault_ids},
            "synthetic_training_data": True,
            "reason": "",
            "note": (
                "訓練資料為 knowledge/corpus.py 的合成維修歷史（readings 欄位亦為合成），"
                "不是 Simulator 的 ground truth，也不是真實產線紀錄。"
            ),
        }
        if self.weight <= 0.0:
            return {**base, "reason": f"權重為 {self.weight:g}，這一層不參與排名（預設狀態）。"}
        if not self.sklearn_available():
            return {**base, "reason": "scikit-learn 未安裝，判別式接手層降級為停用（排名完全由指紋法決定）。"}

        model = self._model_for(machine, machine_id)
        base["cases"] = model["cases"]
        if model["error"]:
            return {**base, "reason": str(model["error"])}

        features = self._features(machine, readings)
        if features is None:
            return {**base, "reason": "本次觀測缺少訓練時使用的訊號，無法組出特徵向量。"}

        classes: list[str] = model["classes"]  # type: ignore[assignment]
        proba = model["clf"].predict_proba([features])[0]  # type: ignore[index]
        table = {fid: 0.0 for fid in fault_ids}
        for cls, p in zip(classes, proba):
            if cls in table:
                table[cls] = float(p)
        return {
            **base,
            "enabled": True,
            "weight": self.weight,
            "probabilities": table,
            "reason": (
                f"同機台標註案例 {model['cases']} 筆 ≥ {self.min_cases}，"
                f"LogisticRegression 以固定權重 {self.weight:g} 參與合分。"
            ),
        }

    # ------------------------------------------------------------------ 內部
    def _model_for(self, machine, machine_id: str) -> dict[str, object]:
        """為單一機台訓練（並快取）一個多類別 LogisticRegression。

        只用**這台機台自己**的案例：不同機台的 nominal/scale 與工況不同，
        把 M-B 的案例混進 M-A 的模型，等於用別台機器的歷史去推翻這台機器的觀測。
        """
        if machine_id in self._cache:
            return self._cache[machine_id]

        rows: list[list[float]] = []
        labels: list[str] = []
        for case in MAINTENANCE_HISTORY:
            if case.machine_id != machine_id or not case.readings:
                continue
            if case.diagnosed_fault not in FAULTS:
                continue
            vector = self._features(machine, case.readings)
            if vector is None:
                continue
            rows.append(vector)
            labels.append(case.diagnosed_fault)

        result: dict[str, object] = {"cases": len(rows), "error": None, "clf": None, "classes": []}
        if len(rows) < self.min_cases:
            result["error"] = (
                f"{machine_id} 只有 {len(rows)} 筆帶讀值的標註案例，未達門檻 {self.min_cases}；"
                "判別式接手層停用，排名完全由指紋法決定（這正是冷啟動的預期狀態）。"
            )
        elif len(set(labels)) < 2:
            result["error"] = f"{machine_id} 的標註案例只涵蓋 1 種故障，判別式模型無從鑑別，停用。"
        else:
            from sklearn.linear_model import LogisticRegression

            clf = LogisticRegression(
                # class_weight balanced：維修歷史本來就偏向常見故障，
                # 不平衡下不設它會學成「一律猜最常見的那一個」，那就退化成多數決 baseline。
                class_weight="balanced",
                max_iter=1000,
                # 固定 seed，讓稽核軌跡上的機率可重現（規格：所有數字要可重現）。
                random_state=20260809,
            )
            clf.fit(rows, labels)
            result["clf"] = clf
            result["classes"] = list(clf.classes_)
        self._cache[machine_id] = result
        return result

    @staticmethod
    def _features(machine, readings: dict[str, float]) -> list[float] | None:
        """特徵 = 指紋法的那個正規化偏離向量，順序固定為 `machine.signals`。

        刻意與 `DiagnosisAgent._deviation_vector()` 用同一個定義：
        兩個方法吃**完全相同**的輸入，比較才是在比方法而不是比特徵工程。
        """
        vector: list[float] = []
        for spec in machine.signals:
            value = readings.get(spec.name)
            if value is None:
                return None
            vector.append((value - spec.nominal) / spec.scale)
        return vector


class DiagnosisAgent(Agent):
    name = "diagnosis-agent"
    role = "根因分析"

    def __init__(self, ctx=None, reranker: "DiscriminativeReranker | None" = None) -> None:
        super().__init__(ctx)
        self.signatures: list[FaultSignature] = []
        # 預設 None＝判別式接手層不存在。這條路徑要被明確打開才會影響任何一個數字。
        self.reranker = reranker

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
                # 多原型：同一個故障可能有一個以上的徵兆方向（過載 vs 失載、冷卻不足 vs 過度），
                # 取最大餘弦 —— 任何一個方向像就算像。只有主原型的故障結果與單原型逐位元相同。
                prototype_hits = {
                    sig.fault_id: self._match_prototype(observed, sig) for sig in self.signatures
                }
                cosines = {fid: hit[2] for fid, hit in prototype_hits.items()}

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

            # 判別式接手層：只有在被明確裝上、權重 > 0、且同機台標註案例夠多時才會參與。
            with self.tool("history.discriminative_rerank", f"machine={machine_id}"):
                rerank = self._rerank_state(machine, machine_id, readings, list(cosines))
            rerank_enabled = bool(rerank["enabled"])
            rerank_weight = float(rerank["weight"]) if rerank_enabled else 0.0
            rerank_probs: dict[str, float] = rerank["probabilities"]  # type: ignore[assignment]

            share = float(acoustic["share"])
            matches = []
            for sig in self.signatures:
                sensor_cos = cosines[sig.fault_id]
                mic_cos = float(acoustic["cosines"].get(sig.fault_id, 0.0))
                # 融合：share = 0 時逐位元退化成原本的純感測器判斷。
                fused = (1.0 - share) * sensor_cos + share * mic_cos
                base = (
                    W_SIGNATURE * max(0.0, fused)
                    + W_PRIOR * priors.get(sig.fault_id, 0.0)
                    + W_DOCS * docs.get(sig.fault_id, 0.0)
                )
                proba = float(rerank_probs.get(sig.fault_id, 0.0))
                # 沒啟用時**完全不碰這條算式**（不是加 0.0，是根本不執行加法），
                # 「預設行為逐位元不變」因此是結構上成立，而不是靠浮點數剛好相等。
                combined = base + rerank_weight * proba if rerank_enabled else base
                index, name, _ = prototype_hits[sig.fault_id]
                matches.append(
                    _Match(
                        signature=sig,
                        cosine=fused,
                        cosine_sensor=sensor_cos,
                        cosine_acoustic=mic_cos,
                        prior=priors.get(sig.fault_id, 0.0),
                        docs=docs.get(sig.fault_id, 0.0),
                        combined=combined,
                        prototype_index=index,
                        prototype_name=name,
                        rerank=proba,
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
                # 判別式接手層的權重。停用時為 0.0，「合計 = 各項相加」因此照樣成立
                #（多出來的那一項乘以 0）。啟用時 Dashboard 需要多渲染一列，見 docs。
                "rerank": rerank_weight,
            },
            thresholds={
                "strength_full": STRENGTH_FULL,
                "no_fault": NO_FAULT_STRENGTH,
                "acoustic_strength_floor": ACOUSTIC_STRENGTH_FLOOR,
                "acoustic_strength_full": ACOUSTIC_STRENGTH_FULL,
            },
            observations=self._observations(machine, readings, observed),
            acoustics=acoustic,
            reranker=rerank,
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
                    "rerank": round(m.rerank, 3),
                    "combined": round(m.combined, 3),
                    "confidence": round(confidences[m.signature.fault_id], 3),
                    # 稽核 log 是純 JSON，這裡可以放原型的**名稱**；
                    # RootCauseCandidate.scores 只能放數字（見 domain.py 的說明）。
                    "prototype": m.prototype_name,
                    "prototype_index": m.prototype_index,
                    "prototype_count": len(m.signature.prototypes),
                }
                for m in matches
            },
            acoustic_share=round(share, 3),
            reranker={
                "enabled": rerank["enabled"],
                "weight": rerank["weight"],
                "cases": rerank["cases"],
                "sklearn": rerank["sklearn"],
                "reason": rerank["reason"],
                "probabilities": {k: round(v, 3) for k, v in rerank_probs.items()},
            },
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
                "偵測器本身以 DCASE2020/MIMII 真實工業錄音另行驗證，見 docs/factory_guardian/acoustic_validation.md。"
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

    def _rerank_state(
        self, machine, machine_id: str, readings: dict[str, float], fault_ids: list[str]
    ) -> dict[str, object]:
        """判別式接手層的完整狀態。沒有裝上這一層時也要留下紀錄，而不是什麼都不寫。

        「沒有啟用」與「啟用了但沒作用」在稽核上是兩件不同的事，
        所以未安裝時也回傳一個帶 `reason` 的 dict，Dashboard 與 log 都看得到。
        """
        if self.reranker is None:
            return {
                "enabled": False,
                "weight": 0.0,
                "configured_weight": 0.0,
                "min_cases": MIN_LABELLED_CASES,
                "sklearn": DiscriminativeReranker.sklearn_available(),
                "cases": 0,
                "probabilities": {fid: 0.0 for fid in fault_ids},
                "synthetic_training_data": True,
                "reason": "本次診斷未掛載判別式接手層；排名完全由指紋法決定（預設狀態）。",
                "note": "指紋法負責冷啟動與可解釋性，判別式模型在標註歷史累積後才接手排序。",
            }
        return self.reranker.probabilities(machine, machine_id, readings, fault_ids)

    @classmethod
    def _match_prototype(
        cls, observed: dict[str, float], signature: FaultSignature
    ) -> tuple[int, str, float]:
        """在一個故障的所有原型裡取最大餘弦，回傳 ``(索引, 名稱, 餘弦)``。

        平手時取索引小的，也就是優先採信主原型 —— 一來結果確定可重現，
        二來「手冊主徵兆」在說得通的時候不該被替代方向搶走解釋權。
        """
        best_index, best_name, best_cos = 0, PRIMARY_PROTOTYPE, -2.0
        for index, prototype in enumerate(signature.prototypes):
            value = cls._cosine(observed, prototype.profile)
            if value > best_cos:
                best_index, best_name, best_cos = index, prototype.name, value
        return best_index, best_name, best_cos

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

        # 1) 感測器證據：貢獻最大的兩個訊號。
        # 用**實際命中的那個原型**來挑訊號與寫文字：若排名是被「失載」方向拿下的，
        # 卻拿「過載」方向的訊號當證據，Evidence 就會和排名說不同的故事。
        hit = sig.prototypes[match.prototype_index]
        contributions = sorted(hit.profile.items(), key=lambda kv: -abs(kv[1]))[:2]
        variant = "" if match.prototype_index == 0 else f"（{hit.name} 變異型）"
        for name, _ in contributions:
            if name in readings:
                evidence.append(
                    Evidence(
                        source="sensor",
                        reference=f"{machine_id}.{name}",
                        statement=(
                            f"{name} 觀測值 {readings[name]:.2f}，符合此故障指紋{variant}的主要方向。"
                        ),
                        weight=abs(match.cosine_sensor),
                    )
                )
        if match.prototype_index != 0 and hit.rationale:
            evidence.append(
                Evidence(
                    source="manual",
                    reference=(sig.manual_refs[0] if sig.manual_refs else "MANUAL"),
                    statement=f"命中的是「{hit.name}」變異型指紋。{hit.rationale}",
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
                # 判別式接手層的機率。停用時為 0.0，且沒有進入 combined。
                "rerank": match.rerank,
                "combined": match.combined,
                # 命中的是第幾個原型（0 = 手冊主徵兆）。這裡只能放數字，
                # 名稱請看稽核 log 的 scores.<fault>.prototype（理由見 domain.py）。
                "prototype": float(match.prototype_index),
                "prototype_count": float(len(match.signature.prototypes)),
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
    "MIN_LABELLED_CASES",
    "DiagnosisAgent",
    "DiscriminativeReranker",
    "NO_FAULT_ID",
    "W_ACOUSTIC_SHARE",
    "W_DOCS",
    "W_PRIOR",
    "W_RERANK",
    "W_SIGNATURE",
]
