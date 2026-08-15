"""對照組。

研究文件 §3.3 明列失分警訊：「只報模型準確率，沒有和現行流程、人工判斷或簡單 baseline 比較。」
所以指紋法必須同時對上三種對照，缺一不可：

1. **`ThresholdRuleBaseline`｜現行流程** —— 單一訊號門檻告警。
   這就是工廠現在在做的事，也是專案 Benchmark 裡的 Baseline A。
   它不是稻草人：門檻是**在訓練切分上以 F1 最佳化選出來的**，比人工拍的數字更強。

2. **`sklearn_baselines()`｜標準機器學習** —— LogisticRegression 與 RandomForest。
   這一組是「你為什麼不直接丟 XGBoost」的答案。它們吃到的特徵與指紋法**完全相同**，
   所以比的是方法，不是特徵工程。

3. **`MajorityBaseline` / `StratifiedRandomBaseline`｜下限** —— 不平衡資料下的參考底線。
   全猜正常就有 96.6% accuracy，這兩個 baseline 存在的意義就是讓那個數字現形。

sklearn 不在 `pyproject.toml` 的相依裡，所以第 2 組是**選配**：
`sklearn_baselines()` 在缺套件時回傳 `None`，呼叫端負責在報告中標明「未執行」而不是靜默跳過。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .ai4i import DIAGNOSABLE_MODES, NO_FAULT_LABEL, PRODUCT_TYPES, Sample
from .fingerprint import Normalizer


# --------------------------------------------------------------------------- 1. 門檻規則
@dataclass(frozen=True)
class ThresholdRule:
    fault_id: str
    channel: str
    direction: int
    """+1 = 高於門檻觸發；-1 = 低於門檻觸發。"""

    threshold: float
    """以正規化偏離量（z 分數）表示。"""

    train_f1: float


@dataclass
class ThresholdRuleBaseline:
    """單一訊號門檻規則 —— 工廠現行做法的等價物。

    每個故障模式挑一個最具代表性的訊號（訓練集上平均偏離量絕對值最大者），
    在該訊號上找一個 F1 最佳門檻。推論時取「超出門檻最多」的那條規則；都沒觸發就報正常。

    它與指紋法的**唯一**差別是：門檻規則一次只看一個維度，
    指紋法看的是整個向量的方向。這個對照因此直接量化了「多訊號組合」本身的價值。
    """

    channels: tuple[str, ...]
    normalizer: Normalizer | None = field(default=None, init=False)
    rules: list[ThresholdRule] = field(default_factory=list, init=False)

    def fit(self, train: list[Sample]) -> "ThresholdRuleBaseline":
        self.normalizer = Normalizer.fit(train, self.channels)
        devs = [(self.normalizer.deviation(s), s) for s in train]
        self.rules = []
        for code in DIAGNOSABLE_MODES:
            positives = [d for d, s in devs if code in s.modes]
            if not positives:
                continue
            # 代表訊號：訓練集上該模式平均偏離量絕對值最大的通道。
            means = {ch: sum(d[ch] for d in positives) / len(positives) for ch in self.channels}
            channel = max(self.channels, key=lambda ch: abs(means[ch]))
            direction = 1 if means[channel] >= 0 else -1
            values = sorted({round(d[channel] * direction, 3) for d, _ in devs})
            labels = [code in s.modes for _, s in devs]
            series = [d[channel] * direction for d, _ in devs]
            threshold, f1 = _best_threshold(series, labels, values)
            self.rules.append(ThresholdRule(code, channel, direction, threshold, f1))
        return self

    def predict(self, sample: Sample) -> str:
        assert self.normalizer is not None, "必須先 fit()"
        dev = self.normalizer.deviation(sample)
        best: tuple[float, str] | None = None
        for rule in self.rules:
            margin = dev[rule.channel] * rule.direction - rule.threshold
            if margin >= 0 and (best is None or margin > best[0]):
                best = (margin, rule.fault_id)
        return best[1] if best else NO_FAULT_LABEL

    def rank(self, sample: Sample) -> list[str]:
        """依超出門檻的幅度排序，未觸發的規則排在 no-fault 之後。"""
        assert self.normalizer is not None, "必須先 fit()"
        dev = self.normalizer.deviation(sample)
        scored = [
            (dev[r.channel] * r.direction - r.threshold, r.fault_id) for r in self.rules
        ]
        scored.sort(key=lambda kv: -kv[0])
        fired = [fid for margin, fid in scored if margin >= 0]
        rest = [fid for margin, fid in scored if margin < 0]
        return fired + [NO_FAULT_LABEL] + rest


def _best_threshold(
    series: list[float], labels: list[bool], candidates: list[float]
) -> tuple[float, float]:
    """在候選門檻中選 F1 最大者。候選過多時等距抽樣，避免 O(n²)。"""
    if len(candidates) > 400:
        step = len(candidates) / 400.0
        candidates = [candidates[int(i * step)] for i in range(400)]
    total_pos = sum(labels)
    best = (candidates[0] if candidates else 0.0, 0.0)
    for thr in candidates:
        tp = fp = 0
        for value, positive in zip(series, labels):
            if value >= thr:
                if positive:
                    tp += 1
                else:
                    fp += 1
        if tp == 0:
            continue
        precision = tp / (tp + fp)
        recall = tp / total_pos if total_pos else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        if f1 > best[1]:
            best = (thr, f1)
    return best


# --------------------------------------------------------------------------- 3. 下限
@dataclass
class MajorityBaseline:
    """永遠回答訓練集中最常見的標籤。不平衡資料的 accuracy 現形劑。"""

    label: str = NO_FAULT_LABEL

    def fit(self, train: list[Sample]) -> "MajorityBaseline":
        counts: dict[str, int] = {}
        for s in train:
            key = s.modes[0] if s.is_single_mode else (NO_FAULT_LABEL if s.is_normal else s.modes[0])
            counts[key] = counts.get(key, 0) + 1
        self.label = max(counts, key=lambda k: counts[k]) if counts else NO_FAULT_LABEL
        return self

    def predict(self, sample: Sample) -> str:
        return self.label

    def rank(self, sample: Sample) -> list[str]:
        others = [c for c in (*DIAGNOSABLE_MODES, NO_FAULT_LABEL) if c != self.label]
        return [self.label, *others]


@dataclass
class StratifiedRandomBaseline:
    """依訓練集類別分布隨機猜。固定 seed，所以報告數字可重現。"""

    seed: int = 20260809
    weights: dict[str, float] = field(default_factory=dict, init=False)
    _rng: random.Random = field(default_factory=lambda: random.Random(20260809), init=False)

    def fit(self, train: list[Sample]) -> "StratifiedRandomBaseline":
        counts: dict[str, float] = {c: 0.0 for c in (*DIAGNOSABLE_MODES, NO_FAULT_LABEL)}
        for s in train:
            key = NO_FAULT_LABEL if s.is_normal else (s.modes[0] if s.modes else NO_FAULT_LABEL)
            counts[key] = counts.get(key, 0.0) + 1.0
        total = sum(counts.values()) or 1.0
        self.weights = {k: v / total for k, v in counts.items()}
        self._rng = random.Random(self.seed)
        return self

    def predict(self, sample: Sample) -> str:
        return self.rank(sample)[0]

    def rank(self, sample: Sample) -> list[str]:
        labels = list(self.weights)
        # 用 uid 當 seed：同一筆樣本永遠得到同一個隨機排序，重跑報告數字不會變。
        rng = random.Random(self.seed ^ sample.uid)
        rng.shuffle(labels)
        return labels


# --------------------------------------------------------------------------- 2. sklearn
def sklearn_available() -> bool:
    try:
        import sklearn  # noqa: F401
    except Exception:
        return False
    return True


def feature_matrix(
    samples: list[Sample], normalizer: Normalizer
) -> tuple[list[list[float]], list[str]]:
    """指紋法看得到什麼，分類器就看得到什麼 —— 一模一樣的正規化偏離向量。

    另外補上產品等級的 one-hot，因為指紋法也用到它（作為歷史先驗）。
    不給的話這個對照就不公平，贏了也不能算數。
    """
    names = [*normalizer.channels, *(f"type_{t}" for t in PRODUCT_TYPES)]
    rows: list[list[float]] = []
    for s in samples:
        dev = normalizer.deviation(s)
        rows.append(
            [dev[ch] for ch in normalizer.channels]
            + [1.0 if s.product_type == t else 0.0 for t in PRODUCT_TYPES]
        )
    return rows, names


@dataclass
class SklearnModel:
    """把 sklearn 估計器包成和其他 baseline 相同的介面（`predict` / `rank`）。"""

    name: str
    estimator: object
    normalizer: Normalizer
    classes: list[str] = field(default_factory=list)

    def rank(self, sample: Sample) -> list[str]:
        rows, _ = feature_matrix([sample], self.normalizer)
        proba = self.estimator.predict_proba(rows)[0]  # type: ignore[attr-defined]
        order = sorted(range(len(proba)), key=lambda i: -proba[i])
        return [self.classes[i] for i in order]

    def rank_batch(self, samples: list[Sample]) -> list[list[str]]:
        """批次版本。逐筆呼叫 sklearn 在 10,000 筆 × 5 fold 下慢得沒必要。"""
        rows, _ = feature_matrix(samples, self.normalizer)
        probas = self.estimator.predict_proba(rows)  # type: ignore[attr-defined]
        out: list[list[str]] = []
        for proba in probas:
            order = sorted(range(len(proba)), key=lambda i: -proba[i])
            out.append([self.classes[i] for i in order])
        return out

    def predict(self, sample: Sample) -> str:
        return self.rank(sample)[0]


def sklearn_baselines(
    train: list[Sample], channels: tuple[str, ...], seed: int = 20260809
) -> dict[str, SklearnModel] | None:
    """訓練 LogisticRegression 與 RandomForest；缺 sklearn 時回傳 `None`。

    兩者都設 ``class_weight="balanced"``。不設的話，故障只佔 3.39%，
    兩個模型都會學成「全部猜正常」，比較就變成沒有意義的表演。
    """
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.linear_model import LogisticRegression
    except Exception:
        return None

    normalizer = Normalizer.fit(train, channels)
    # 與指紋法一致：只用單一模式與正常樣本訓練（多模式樣本沒有唯一標籤）。
    fit_rows = [s for s in train if s.is_normal or s.is_single_mode]
    fit_rows = [s for s in fit_rows if s.is_normal or s.modes[0] in DIAGNOSABLE_MODES]
    x, _ = feature_matrix(fit_rows, normalizer)
    y = [NO_FAULT_LABEL if s.is_normal else s.modes[0] for s in fit_rows]

    models: dict[str, SklearnModel] = {}
    lr = LogisticRegression(max_iter=2000, class_weight="balanced", random_state=seed)
    lr.fit(x, y)
    models["logistic_regression"] = SklearnModel(
        "LogisticRegression", lr, normalizer, list(lr.classes_)
    )

    rf = RandomForestClassifier(
        n_estimators=300, class_weight="balanced_subsample", random_state=seed, n_jobs=-1
    )
    rf.fit(x, y)
    models["random_forest"] = SklearnModel(
        "RandomForest", rf, normalizer, list(rf.classes_)
    )
    return models


__all__ = [
    "MajorityBaseline",
    "SklearnModel",
    "StratifiedRandomBaseline",
    "ThresholdRule",
    "ThresholdRuleBaseline",
    "feature_matrix",
    "sklearn_available",
    "sklearn_baselines",
]
