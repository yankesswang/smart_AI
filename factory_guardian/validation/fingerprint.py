"""感測器指紋餘弦診斷器 —— `agents/diagnosis.py` 的**獨立等價實作**。

## 為什麼要獨立實作而不是 import

`agents/diagnosis.py` 綁在本專案的 `FactorySnapshot` / `AnomalyEvent` / `KnowledgeBase` 上，
外部資料集沒有這些東西。而且它正在被持續修改（新增模態、調整融合權重），
外部驗證模組如果 import 它，方法一改、驗證數字就會跟著漂，
「這個數字是在驗證什麼版本的方法」會說不清楚。

所以這裡把**方法本身**抽出來重寫一次，並在每一段標明它對應 diagnosis.py 的哪個部分。
兩邊的常數也各自定義（不是 import），改動不會互相波及。

代價是**兩邊會漂**：diagnosis.py 之後若調整融合權重或新增模態（例如聲學），
這裡不會自動跟上。這是刻意的取捨 —— 外部驗證數字必須綁定「被驗證的那個版本的方法」，
否則「這個 0.724 是在驗證什麼」會說不清楚。本檔驗證的版本記錄在
`docs/factory_guardian/external_validation.md` 的「被驗證的方法版本」一節；要重新驗證新版本時，
更新這裡的常數並重跑 `python -m factory_guardian.validation`。

## 對應關係

| 這裡 | `agents/diagnosis.py` |
|---|---|
| `Normalizer` | `DiagnosisAgent._deviation_vector()` + `SignalSpec.nominal/scale` |
| `FingerprintModel.fit()` 的 centroid | `twin/faults.py::fault_signatures()`（profile = deltas / scale） |
| `_cosine()` | `DiagnosisAgent._cosine()`（逐字等價） |
| `combined` 三項加權 | `W_SIGNATURE / W_PRIOR / W_DOCS` 的線性組合 |
| `_confidences()` | `DiagnosisAgent._confidences()`（softmax + 強度衰減） |
| no-fault gate | `DiagnosisAgent._no_fault_candidate()` |

## 一個關鍵差異，必須講清楚

專案裡的指紋來自**手冊**（`FAULTS[*].deltas`，工程師寫的領域知識）。
AI4I 沒有手冊，所以這裡的指紋是**從訓練切分的歷史故障案例算出來的類別質心**。

這不是偷看答案：真實工廠的手冊本來就是這樣寫出來的（累積案例 → 歸納徵兆），
而且質心只用 train fold 的資料算，test fold 完全沒參與。
但它確實讓方法多了一點「學習」成分，所以本模組同時報告
「質心指紋」與「規則不可見」兩件事，並且拿它跟真的會學習的分類器（LR / RF）比。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .ai4i import (
    DIAGNOSABLE_MODES,
    NO_FAULT_LABEL,
    PRODUCT_TYPES,
    STRICT_CHANNELS,
    Sample,
)

# --------------------------------------------------------------------------- 常數
# 與 agents/diagnosis.py 的 W_SIGNATURE / W_PRIOR / W_DOCS 對齊。
# 刻意重新定義而非 import：見模組 docstring。
W_SIGNATURE = 0.75
W_PRIOR = 0.15
W_DOCS = 0.10
SOFTMAX_TEMPERATURE = 0.16

# AI4I 沒有手冊 / SOP / 維修紀錄語料，文件支持度對每個候選一律 0。
# 保留這一項而不是把權重併掉，是為了讓算式和 diagnosis.py 逐項對得上：
# 常數項對所有候選相同，不影響排名，只讓 combined 的絕對值低 0.10。
DOCS_AFFINITY_UNAVAILABLE = 0.0

#: no-fault 門檻的訓練分位數。專案用固定常數 0.45（因為它的 scale 是手冊訂的物理量），
#: AI4I 的通道是 z 分數，固定常數沒有意義，所以改成「訓練正常樣本強度的 q 分位」。
#: 取 0.99 = 容許 1% 的正常樣本被當成有徵兆，對應產線可接受的誤報率。
NO_FAULT_QUANTILE = 0.99


# --------------------------------------------------------------------------- 正規化
@dataclass
class Normalizer:
    """把原始通道值轉成正規化偏離向量：``(value − nominal) / scale``。

    對應 `DiagnosisAgent._deviation_vector()`。差別只在 nominal / scale 的來源：
    專案是 `SignalSpec` 寫死的機台規格，這裡是**訓練切分中正常樣本**的平均數與標準差。
    用正常樣本（而不是全部樣本）估計，是因為 nominal 的定義就是「這台機器沒事時長什麼樣」；
    把故障樣本混進去會把基準線往故障方向拉，指紋的偏離量就被稀釋了。
    """

    channels: tuple[str, ...]
    nominal: dict[str, float] = field(default_factory=dict)
    scale: dict[str, float] = field(default_factory=dict)

    @classmethod
    def fit(cls, samples: list[Sample], channels: tuple[str, ...] = STRICT_CHANNELS) -> "Normalizer":
        normals = [s for s in samples if s.is_normal] or samples
        nominal: dict[str, float] = {}
        scale: dict[str, float] = {}
        for ch in channels:
            values = [s.channels[ch] for s in normals]
            mean = sum(values) / len(values)
            var = sum((v - mean) ** 2 for v in values) / max(1, len(values) - 1)
            nominal[ch] = mean
            # 下限防呆：常數通道會讓除法爆掉。
            scale[ch] = max(math.sqrt(var), 1e-9)
        return cls(channels=channels, nominal=nominal, scale=scale)

    def deviation(self, sample: Sample) -> dict[str, float]:
        return {ch: (sample.channels[ch] - self.nominal[ch]) / self.scale[ch] for ch in self.channels}

    @staticmethod
    def strength(vector: dict[str, float]) -> float:
        """訊號強度＝偏離向量的 L2 長度。對應 diagnosis.py 的 ``strength``。"""
        return math.sqrt(sum(v * v for v in vector.values()))


def cosine(observed: dict[str, float], profile: dict[str, float]) -> float:
    """與 `DiagnosisAgent._cosine()` 逐字等價。"""
    keys = set(observed) | set(profile)
    dot = sum(observed.get(k, 0.0) * profile.get(k, 0.0) for k in keys)
    n1 = math.sqrt(sum(observed.get(k, 0.0) ** 2 for k in keys))
    n2 = math.sqrt(sum(profile.get(k, 0.0) ** 2 for k in keys))
    if n1 < 1e-9 or n2 < 1e-9:
        return 0.0
    return dot / (n1 * n2)


# --------------------------------------------------------------------------- 候選
@dataclass(frozen=True)
class Candidate:
    """一個根因候選。欄位刻意與 `RootCauseCandidate.scores` 同名，方便交叉比對。"""

    fault_id: str
    cosine: float
    prior: float
    docs: float
    combined: float
    confidence: float
    prototype: int = 0
    """命中的是這個故障的第幾個原型（0 = 第一個）。

    對應 `RootCauseCandidate.scores["prototype"]`。單原型時永遠是 0，
    多原型時它讓「這一筆是被哪個子指紋救回來的」變成可以清點的事實，
    而不是只能看總分猜。"""


@dataclass
class Prototype:
    """一個故障模式的指紋。``profile`` 對應 `FaultSignature.profile`。"""

    fault_id: str
    profile: dict[str, float]
    support: int
    """由幾筆訓練案例歸納而來。support 太小的指紋要在文件中標明不可靠。"""


# --------------------------------------------------------------------------- 模型
@dataclass
class FingerprintModel:
    """指紋餘弦多類別根因判定。

    `predict()` 的輸出是**排序過的候選清單**，不是單一標籤 ——
    這和 `Diagnosis.candidates` 一致，也才有 Top-3 命中率可以量。
    """

    channels: tuple[str, ...] = STRICT_CHANNELS
    n_prototypes: int = 1
    """每個故障模式的原型數量。**預設 1**，讓 `docs/factory_guardian/external_validation.md` 既有的數字原地可重現。

    >1 時改用 k-means 分群出多個子指紋、取最大餘弦。這原本只是 ablation，
    但 §8.2 的結果（Top-1 0.724 → 0.821）已經被 `agents/diagnosis.py` 採納：
    診斷 Agent 現在支援一個故障對多個指紋（`FaultSignature.alt_prototypes`），
    來源是手冊語意而不是 k-means —— 兩邊「取最大餘弦」的規則相同，
    差別只在原型從哪裡來（手冊 vs 訓練切分的分群），這正是本模組與 Agent 的既有分工。

    主實驗要跑多原型時用 `python -m factory_guardian.validation --prototypes 2`。
    """

    use_prior: bool = True
    seed: int = 20260809

    normalizer: Normalizer | None = field(default=None, init=False)
    prototypes: dict[str, list[Prototype]] = field(default_factory=dict, init=False)
    priors: dict[str, dict[str, float]] = field(default_factory=dict, init=False)
    no_fault_threshold: float = field(default=0.0, init=False)
    strength_full: float = field(default=1.0, init=False)

    # ------------------------------------------------------------------ 訓練
    def fit(self, train: list[Sample]) -> "FingerprintModel":
        self.normalizer = Normalizer.fit(train, self.channels)

        # 指紋只用「單一模式」的訓練樣本歸納。
        # 多模式樣本（同時 HDF+PWF 之類）會同時污染兩個質心，讓兩個指紋互相靠攏。
        by_mode: dict[str, list[dict[str, float]]] = {code: [] for code in DIAGNOSABLE_MODES}
        for s in train:
            if s.is_single_mode and s.modes[0] in by_mode:
                by_mode[s.modes[0]].append(self.normalizer.deviation(s))

        self.prototypes = {}
        for code, vectors in by_mode.items():
            if not vectors:
                continue
            if self.n_prototypes <= 1 or len(vectors) < 2 * self.n_prototypes:
                self.prototypes[code] = [Prototype(code, _centroid(vectors, self.channels), len(vectors))]
            else:
                groups = _kmeans(vectors, self.channels, k=self.n_prototypes, seed=self.seed)
                self.prototypes[code] = [
                    Prototype(code, _centroid(g, self.channels), len(g)) for g in groups if g
                ]

        # 歷史先驗：對應 `KnowledgeBase.machine_fault_prior()`。
        # 專案是「這台機器過去得過什麼病」；AI4I 沒有機台 id，最接近的分群是產品等級 L/M/H
        #（OSF 的門檻本來就依等級不同），所以用「同等級產品的歷史故障分布」當先驗。
        self.priors = self._fit_priors(train)

        # 門檻校準：只用 train，test fold 不參與。
        strengths = sorted(
            self.normalizer.strength(self.normalizer.deviation(s)) for s in train if s.is_normal
        )
        self.no_fault_threshold = _quantile(strengths, NO_FAULT_QUANTILE) if strengths else 0.0

        fault_strengths = sorted(
            self.normalizer.strength(self.normalizer.deviation(s))
            for s in train
            if s.diagnosable_modes
        )
        # 對應 STRENGTH_FULL：訊號到這個強度就給滿信心。取訓練故障樣本強度中位數。
        self.strength_full = max(_quantile(fault_strengths, 0.5), self.no_fault_threshold, 1e-6)
        return self

    def _fit_priors(self, train: list[Sample]) -> dict[str, dict[str, float]]:
        """P(mode | product_type)，Laplace 平滑後在故障模式間正規化。"""
        priors: dict[str, dict[str, float]] = {}
        for ptype in PRODUCT_TYPES:
            counts = {code: 1.0 for code in DIAGNOSABLE_MODES}  # Laplace α=1
            for s in train:
                if s.product_type != ptype:
                    continue
                for code in s.diagnosable_modes:
                    counts[code] = counts.get(code, 0.0) + 1.0
            total = sum(counts.values()) or 1.0
            priors[ptype] = {code: c / total for code, c in counts.items()}
        return priors

    # ------------------------------------------------------------------ 推論
    def rank(self, sample: Sample) -> list[Candidate]:
        """回傳排序過的候選清單（含 no-fault）。對應 `Diagnosis.candidates`。"""
        assert self.normalizer is not None, "必須先 fit()"
        observed = self.normalizer.deviation(sample)
        strength = self.normalizer.strength(observed)
        prior_row = self.priors.get(sample.product_type, {})

        raw: list[tuple[str, float, float, float, int]] = []
        for code, protos in self.prototypes.items():
            # 多原型時取最大餘弦：任何一個子指紋像就算像。
            # 平手取索引小的，與 `DiagnosisAgent._match_prototype()` 同一條規則（結果可重現）。
            index, cos = max(
                ((i, cosine(observed, p.profile)) for i, p in enumerate(protos)),
                key=lambda pair: (pair[1], -pair[0]),
            )
            prior = prior_row.get(code, 0.0) if self.use_prior else 0.0
            combined = (
                W_SIGNATURE * max(0.0, cos)
                + W_PRIOR * prior
                + W_DOCS * DOCS_AFFINITY_UNAVAILABLE
            )
            raw.append((code, cos, prior, combined, index))

        confidences = self._confidences({code: comb for code, _, _, comb, _ in raw}, strength)
        candidates = [
            Candidate(
                fault_id=code,
                cosine=cos,
                prior=prior,
                docs=DOCS_AFFINITY_UNAVAILABLE,
                combined=comb,
                confidence=confidences[code],
                prototype=index,
            )
            for code, cos, prior, comb, index in raw
        ]
        candidates.sort(key=lambda c: -c.combined)

        no_fault = self._no_fault_candidate(strength)
        if candidates and no_fault.confidence > candidates[0].confidence:
            # 對應 diagnosis.py：no-fault 信心高於最佳故障候選時插到最前面。
            candidates.insert(0, no_fault)
        else:
            # diagnosis.py 在這種情況不會產生 no-fault 候選；這裡把它掛在最後，
            # 是為了讓 Top-k 的候選集合大小固定（k 個故障 + 1），指標才可比。
            candidates.append(no_fault)
        return candidates

    def predict(self, sample: Sample) -> str:
        return self.rank(sample)[0].fault_id

    def strength_of(self, sample: Sample) -> float:
        assert self.normalizer is not None, "必須先 fit()"
        return self.normalizer.strength(self.normalizer.deviation(sample))

    # ------------------------------------------------------------------ 內部
    def _confidences(self, combined: dict[str, float], strength: float) -> dict[str, float]:
        """Softmax 後依訊號強度往均勻分布拉。與 `DiagnosisAgent._confidences()` 等價。"""
        if not combined:
            return {}
        top = max(combined.values())
        exps = {k: math.exp((v - top) / SOFTMAX_TEMPERATURE) for k, v in combined.items()}
        total = sum(exps.values()) or 1.0
        sharp = {k: v / total for k, v in exps.items()}
        uniform = 1.0 / len(combined)
        certainty = max(0.0, min(1.0, strength / self.strength_full))
        return {k: uniform + (v - uniform) * certainty for k, v in sharp.items()}

    def _no_fault_candidate(self, strength: float) -> Candidate:
        """對應 `DiagnosisAgent._no_fault_candidate()`：訊號太弱就說「沒有故障徵兆」。"""
        if self.no_fault_threshold <= 0.0:
            conf = 0.0
        else:
            conf = max(0.0, min(1.0, 1.0 - strength / self.no_fault_threshold))
        return Candidate(
            fault_id=NO_FAULT_LABEL,
            cosine=0.0,
            prior=0.0,
            docs=0.0,
            combined=conf,
            confidence=conf,
        )


# --------------------------------------------------------------------------- 小工具
def _centroid(vectors: list[dict[str, float]], channels: tuple[str, ...]) -> dict[str, float]:
    n = len(vectors)
    return {ch: sum(v[ch] for v in vectors) / n for ch in channels}


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def _kmeans(
    vectors: list[dict[str, float]], channels: tuple[str, ...], k: int, seed: int, iters: int = 40
) -> list[list[dict[str, float]]]:
    """最小可用 k-means（確定性初始化）。只服務 `n_prototypes > 1` 的 ablation。

    初始化用「沿第一主要變異通道等分位取點」而非亂數，讓結果可重現、不依賴 seed 品質。
    """
    if k <= 1 or len(vectors) <= k:
        return [vectors]
    spread = max(channels, key=lambda ch: _variance([v[ch] for v in vectors]))
    ordered = sorted(vectors, key=lambda v: v[spread])
    centers = [
        _centroid(chunk, channels)
        for chunk in (ordered[i * len(ordered) // k : (i + 1) * len(ordered) // k] for i in range(k))
        if chunk
    ]
    for _ in range(iters):
        groups: list[list[dict[str, float]]] = [[] for _ in centers]
        for v in vectors:
            best = min(range(len(centers)), key=lambda i: _sqdist(v, centers[i], channels))
            groups[best].append(v)
        new_centers = [_centroid(g, channels) if g else centers[i] for i, g in enumerate(groups)]
        if all(_sqdist(a, b, channels) < 1e-12 for a, b in zip(centers, new_centers)):
            centers = new_centers
            break
        centers = new_centers
    groups = [[] for _ in centers]
    for v in vectors:
        best = min(range(len(centers)), key=lambda i: _sqdist(v, centers[i], channels))
        groups[best].append(v)
    return groups


def _variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)


def _sqdist(a: dict[str, float], b: dict[str, float], channels: tuple[str, ...]) -> float:
    return sum((a[ch] - b[ch]) ** 2 for ch in channels)


__all__ = [
    "DOCS_AFFINITY_UNAVAILABLE",
    "NO_FAULT_QUANTILE",
    "SOFTMAX_TEMPERATURE",
    "W_DOCS",
    "W_PRIOR",
    "W_SIGNATURE",
    "Candidate",
    "FingerprintModel",
    "Normalizer",
    "Prototype",
    "cosine",
]
