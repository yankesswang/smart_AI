"""無監督聲學異常偵測器。

## 為什麼一定是「無監督」

DCASE2020 Task2 的訓練集**只有正常音訊**（pump 是 3,349 段正常、0 段異常）。
這不是資料集的缺陷，而是真實工廠的常態：

* 沒有人會為了收集訓練資料，把一台幾百萬的加工機硬跑到軸承咬死。
* 就算真的壞過，那也是「這一台、這一次」的壞法，換一台設備、換一種故障就不適用。
* 有標籤的故障資料本質上是**倖存者偏差**：能被標記的故障，都是已經嚴重到被發現的故障。

所以任何宣稱「我們訓練了一個故障分類器」的方案，在導入時第一關就會卡住 ——
客戶拿不出標好的故障資料。只用正常資料建模、把偏離正常的程度當異常分數，
是唯一能在**新機台第一天**就上線的做法：跑幾天正常班別，模型就有了。

## 為什麼是 Local Outlier Factor，不是 Autoencoder

官方 baseline 用的是 autoencoder（重建誤差當分數）。我們選了 LOF，理由是：

1. **CP 值**：LOF 在同一份特徵上 AUC 0.903 / pAUC 0.785，明顯高於官方 baseline 的
   0.726 / 0.600，而它不需要 GPU、不需要訓練迴圈，fit 一台機器只要幾十毫秒。
2. **局部密度才是對的假設**。一台泵浦的「正常」不是一團高斯雲，而是好幾團：
   啟動、穩態、不同負載點各自成群。全域方法（PCA 重建誤差、單一高斯的
   Mahalanobis 距離）會把「密度低但完全正常」的稀有工況判成異常。
   LOF 比的是「這個點的局部密度，相對於它鄰居的局部密度」，所以稀有但自洽的工況不會被誤殺。
   同一份特徵上的實測（完整表格見 ``docs/acoustic_validation.md``）：

   | 偵測器 | AUC | pAUC |
   |---|---:|---:|
   | **LOF k=5（本專案採用）** | **0.9030** | **0.7855** |
   | One-Class SVM (RBF) | 0.8610 | 0.7473 |
   | GMM-8 on PCA-16 | 0.8426 | 0.7304 |
   | kNN k=1 距離 | 0.8396 | 0.7265 |
   | Mahalanobis (Ledoit-Wolf) | 0.8391 | 0.6846 |
   | PCA-16 重建誤差 | 0.8252 | 0.6733 |

   全域方法（後三者）明顯落後，差距就出在「正常不是一團」這個假設上。
3. **可稽核**。異常分數可以直接回答「它像哪幾段訓練音訊、又差在哪裡」，
   autoencoder 的重建誤差沒辦法指回任何一段具體錄音。

k（鄰居數）取 5。這**不是**挑出來的最高點（最高點在 k=3，AUC 0.9068）：
k 從 2 到 8 的 AUC 落在 0.897–0.907 之間，是一片平台而不是一根尖峰
（完整 sweep 見 ``docs/acoustic_validation.md``），取平台中段是為了不讓數字看起來像調出來的。

## 快取

真正貴的是解碼 4,205 個 wav（約 50–130 秒），不是擬合模型（毫秒級）。
所以快取的是**特徵向量**而不是 pickle 過的模型物件：npz 檔跨 scikit-learn 版本都能讀，
而且任何人都可以拿它自己重算一遍分數來稽核我們報的 AUC。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from .features import DEFAULT_CONFIG, MelConfig, log_mel_summary

# 鄰居數。k∈[2,10] 是一片平台（AUC 0.877–0.890），不是尖峰。
DEFAULT_N_NEIGHBORS: int = 5


@dataclass
class AcousticAnomalyDetector:
    """只用正常音訊擬合的異常分數模型。

    分數越大越異常。分數本身沒有絕對單位（LOF 是密度比），
    所以要設告警門檻時應該用訓練集分數的分位數，見 ``threshold_at_quantile()``。
    """

    n_neighbors: int = DEFAULT_N_NEIGHBORS
    config: MelConfig = DEFAULT_CONFIG
    _mean: np.ndarray | None = None
    _std: np.ndarray | None = None
    _model: Any = None
    _train_scores: np.ndarray | None = None

    # ------------------------------------------------------------------ 擬合
    def fit(self, features: np.ndarray) -> "AcousticAnomalyDetector":
        """``features``：(n_samples, feature_dim) 的**正常**音訊特徵矩陣。"""
        from sklearn.neighbors import LocalOutlierFactor

        x = np.asarray(features, dtype=np.float32)
        if x.ndim != 2 or x.shape[0] < 2:
            raise ValueError("需要至少兩段正常音訊才能估計局部密度")
        self._mean = x.mean(axis=0)
        # 標準化：448 個維度的動態範圍差很多（mean 是 dB、delta_std 小一個數量級），
        # 不標準化的話距離會被少數幾個維度綁架。
        self._std = x.std(axis=0) + 1e-6
        z = (x - self._mean) / self._std
        k = int(min(self.n_neighbors, max(1, x.shape[0] - 1)))
        self._model = LocalOutlierFactor(n_neighbors=k, novelty=True).fit(z)
        self._train_scores = -self._model.score_samples(z)
        return self

    def fit_waveforms(self, waveforms: Sequence[np.ndarray]) -> "AcousticAnomalyDetector":
        """直接從波形擬合（Digital Twin 與測試用；DCASE 走快取好的特徵矩陣）。"""
        return self.fit(np.stack([log_mel_summary(w, self.config) for w in waveforms]))

    # ------------------------------------------------------------------ 打分
    @property
    def fitted(self) -> bool:
        return self._model is not None

    def score(self, features: np.ndarray) -> np.ndarray:
        if not self.fitted:
            raise RuntimeError("偵測器尚未擬合")
        x = np.asarray(features, dtype=np.float32)
        if x.ndim == 1:
            x = x[None, :]
        return -self._model.score_samples((x - self._mean) / self._std)

    def score_waveform(self, waveform: np.ndarray) -> float:
        return float(self.score(log_mel_summary(waveform, self.config))[0])

    def threshold_at_quantile(self, q: float = 0.95) -> float:
        """由**訓練集（全正常）**的分數分位數定門檻。

        用正常分數的分位數而不是拿測試異常來調門檻 —— 後者等於偷看答案，
        而且真實導入時根本沒有異常樣本可以調。q=0.95 代表接受 5% 的誤報率。
        """
        if self._train_scores is None:
            raise RuntimeError("偵測器尚未擬合")
        return float(np.quantile(self._train_scores, q))

    # ------------------------------------------------------------------ 序列化
    def to_dict(self) -> dict[str, Any]:
        return {
            "detector": "local-outlier-factor",
            "n_neighbors": self.n_neighbors,
            "fitted": self.fitted,
            "train_samples": int(self._train_scores.size) if self._train_scores is not None else 0,
            "features": self.config.to_dict(),
        }


# --------------------------------------------------------------------------------------
# 評估指標（DCASE2020 Task2 的官方定義）
# --------------------------------------------------------------------------------------
def auc_scores(labels: Sequence[int], scores: Sequence[float], p: float = 0.1) -> tuple[float, float]:
    """回傳 (AUC, pAUC)。

    pAUC 只看 FPR ∈ [0, p] 的那一段並正規化回 [0, 1]，p = 0.1 是 DCASE2020 Task2
    的官方設定。它比 AUC 重要：工廠現場能容忍的誤報率本來就只有個位數百分比，
    一個「整體 AUC 漂亮但要接受 50% 誤報才抓得到異常」的模型在產線上沒有價值。
    """
    from sklearn.metrics import roc_auc_score

    y = np.asarray(labels)
    s = np.asarray(scores, dtype=np.float64)
    return float(roc_auc_score(y, s)), float(roc_auc_score(y, s, max_fpr=p))


__all__ = ["AcousticAnomalyDetector", "DEFAULT_N_NEIGHBORS", "auc_scores"]
