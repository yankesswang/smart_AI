"""聲學特徵抽取：log-mel spectrogram 與其時間統計摘要。

這個模組是**唯一**的特徵定義處，真實錄音與合成音訊都走同一條路徑：

* `docs/acoustic_validation.md` 報的 AUC / pAUC，是把這裡的 `log_mel_summary()`
  套在 DCASE2020 Task2（MIMII）**真實泵浦錄音**上算出來的。
* Digital Twin 的 Demo 音訊也是餵給同一個函式（見 `acoustics/synthetic.py`）。

換句話說：「在真實工業錄音上有效」這句話，指的就是這幾行程式碼。

---

## 為什麼是 log-mel，而不是原始頻譜或 MFCC

* **log**：機械聲音的能量跨度有好幾個數量級（背景噪音 vs 衝擊），
  線性尺度下小訊號會被完全壓掉；取對數之後「相對變化」才變成等距。
* **mel**：64 個帶把 513 個 FFT bin 壓到 64 維，同時保留「低頻解析度高、高頻解析度低」
  的特性。滾動軸承缺陷的能量是**寬頻**的，不需要高頻的細解析度；
  真正需要細解析度的是低頻的轉頻與線頻諧波，mel 尺度剛好給了它。
* **不用 MFCC**：DCT 之後前幾個倒頻譜係數描述的是頻譜「包絡」，
  它為語音辨識設計，會刻意丟掉激發訊號的細節 —— 但機械異常常常就藏在那些細節裡。

## 為什麼是「每個 mel band 兩個統計量」，而不是逐 frame 建模

一個 10 秒、16 kHz 的檔案是 313 個 frame。逐 frame 建模（DCASE 官方 baseline 的
autoencoder 就是這樣）會得到上百萬個訓練樣本，但也強迫模型只從 5 個 frame 的短時脈絡
去判斷。機械異常有很大一部分是**整段的統計性質**，把它們直接算出來，模型就不必自己學：

| 統計量 | 物理意義 | 抓得到什麼 |
|---|---|---|
| `mean` | 該頻帶的平均能量 | 「哪些頻帶變大聲了」——寬頻能量上升、少了一個純音 |
| `delta_abs_mean` | 相鄰 frame 差分的平均絕對值 | 「該頻帶的能量抖不抖」——週期性衝擊造成的時間調變 |

這兩個剛好對上軸承缺陷的兩個特徵：**寬頻能量上升**（mean）**且伴隨衝擊調變**
（delta_abs_mean）。只有能量上升而沒有調變，比較像負載變重；兩者同時出現才是缺陷。

`MelConfig.statistics` 可以改，所有支援的統計量列在 `SUMMARY_STATISTICS`。
消融結果（DCASE pump 開發集、LOF k=5）：

| 統計量組合 | 維度 | AUC | pAUC |
|---|---:|---:|---:|
| mean | 64 | 0.8878 | 0.7787 |
| **mean + delta_abs_mean（本專案採用）** | **128** | **0.9030** | **0.7855** |
| mean + std + p10 + p50 + p90 + delta_abs_mean + delta_std | 448 | 0.8903 | 0.7599 |

加更多統計量**沒有**變好。這件事值得寫出來而不是藏起來：它說明分數不是靠堆特徵堆出來的，
也提醒我們這些選擇是在 DCASE **development set** 上做的（完整說明見
`docs/acoustic_validation.md` 的「這些數字的邊界」一節）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

# 2 kHz 以上視為「高頻帶」：滾動軸承缺陷的寬頻衝擊能量主要落在這裡，
# 而主軸轉頻、線頻及其低階諧波都在這條線以下。
HIGH_BAND_HZ: float = 2000.0
# 麥克風校正慣例：1 Pa = 94 dB SPL，對應數位滿刻度 rms = 1.0。
SPL_REF_DB: float = 94.0
# 純音偵測：頻譜功率超過局部噪音底 (median) 這個倍數才算 tonal。
TONAL_PEAK_RATIO: float = 3.0
# 局部噪音底的中值濾波窗（bin 數，必須是奇數）。
TONAL_MEDIAN_BINS: int = 25

# 支援的時間統計量。順序固定，讓稽核時可以指名道姓地說出「第 k 維是第 j 個 mel band 的什麼」。
SUMMARY_STATISTICS: tuple[str, ...] = (
    "mean", "std", "p10", "p50", "p90", "delta_abs_mean", "delta_std",
)
DEFAULT_STATISTICS: tuple[str, ...] = ("mean", "delta_abs_mean")


@dataclass(frozen=True)
class MelConfig:
    """特徵抽取參數。

    sample_rate 與觀測窗由 DCASE2020 pump 資料集決定（10 秒 / 16 kHz / 單聲道）；
    n_fft 與 hop 取語音／機械聲學的常規值（64 ms 窗、32 ms 位移）；
    n_mels = 64 是兼顧頻率解析度與維度的常見取法。
    """

    sample_rate: int = 16_000
    n_fft: int = 1024
    hop_length: int = 512
    n_mels: int = 64
    fmin: float = 0.0
    fmax: float | None = None
    statistics: tuple[str, ...] = field(default=DEFAULT_STATISTICS)

    def __post_init__(self) -> None:
        unknown = [s for s in self.statistics if s not in SUMMARY_STATISTICS]
        if unknown:
            raise ValueError(f"未知的統計量：{unknown}；可用的有 {SUMMARY_STATISTICS}")

    @property
    def feature_dim(self) -> int:
        return self.n_mels * len(self.statistics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "n_fft": self.n_fft,
            "hop_length": self.hop_length,
            "n_mels": self.n_mels,
            "fmin": self.fmin,
            "fmax": self.fmax,
            "statistics": list(self.statistics),
            "feature_dim": self.feature_dim,
        }


DEFAULT_CONFIG = MelConfig()

_MEL_CACHE: dict[tuple, np.ndarray] = {}


def mel_filterbank(config: MelConfig = DEFAULT_CONFIG) -> np.ndarray:
    """mel 濾波器組 (n_mels, 1 + n_fft/2)，稠密形式。建一次就快取。"""
    key = (config.sample_rate, config.n_fft, config.n_mels, config.fmin, config.fmax)
    cached = _MEL_CACHE.get(key)
    if cached is None:
        import librosa  # 延遲載入：Agent 閉環與 CLI 不需要它

        cached = librosa.filters.mel(
            sr=config.sample_rate, n_fft=config.n_fft, n_mels=config.n_mels,
            fmin=config.fmin, fmax=config.fmax,
        ).astype(np.float32)
        _MEL_CACHE[key] = cached
    return cached


def _sparse_mel_filterbank(config: MelConfig):
    """稀疏形式的 mel 濾波器組。

    mel 濾波器是**帶狀**的：(64, 513) 的矩陣裡只有約 1,000 個非零值（3%），
    每個 FFT bin 最多落在兩個相鄰的三角窗裡。

    用稀疏矩陣乘不只是省乘法，更重要的是它**走 scipy 自己的 C 實作而不是 BLAS**。
    稠密 matmul 在這種瘦長形狀（64×513 @ 513×313）上，多執行緒 BLAS 的
    同步成本遠大於運算本身 —— 本機實測稠密版比稀疏版慢兩個數量級，
    而且慢多少取決於使用者的 OPENBLAS_NUM_THREADS。函式庫不該讓自己的效能
    取決於呼叫端的環境變數，所以這裡直接不用它。
    """
    key = (config.sample_rate, config.n_fft, config.n_mels, config.fmin, config.fmax, "sparse")
    cached = _MEL_CACHE.get(key)
    if cached is None:
        from scipy.sparse import csr_matrix

        cached = csr_matrix(mel_filterbank(config))
        _MEL_CACHE[key] = cached
    return cached


def mel_frequencies(config: MelConfig = DEFAULT_CONFIG) -> np.ndarray:
    import librosa

    return librosa.mel_frequencies(n_mels=config.n_mels, fmin=config.fmin,
                                   fmax=config.fmax or config.sample_rate / 2)


def power_spectrogram(waveform: np.ndarray, config: MelConfig = DEFAULT_CONFIG) -> np.ndarray:
    """線性功率頻譜 (1 + n_fft/2, frames)。"""
    import librosa

    y = np.asarray(waveform, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=tuple(range(1, y.ndim)))
    spec = librosa.stft(y, n_fft=config.n_fft, hop_length=config.hop_length)
    return (spec.real ** 2 + spec.imag ** 2).astype(np.float32)


def log_mel_spectrogram(waveform: np.ndarray, config: MelConfig = DEFAULT_CONFIG) -> np.ndarray:
    """log-mel spectrogram (n_mels, frames)，單位 dB。"""
    power = power_spectrogram(waveform, config)
    mel = _sparse_mel_filterbank(config) @ power
    return (20.0 * np.log10(np.maximum(mel, 1e-12))).astype(np.float32)


def summarize_log_mel(log_mel: np.ndarray, config: MelConfig = DEFAULT_CONFIG) -> np.ndarray:
    """把 (n_mels, frames) 壓成一個 len(statistics) × n_mels 的檔案級特徵向量。"""
    x = np.asarray(log_mel, dtype=np.float32)
    if x.ndim == 2:
        x = x[None, ...]

    need_percentile = any(s in ("p10", "p50", "p90") for s in config.statistics)
    percentiles = np.percentile(x, [10, 50, 90], axis=2) if need_percentile else None
    need_delta = any(s.startswith("delta") for s in config.statistics)
    delta = np.diff(x, axis=2) if need_delta else None

    blocks: list[np.ndarray] = []
    for name in config.statistics:
        if name == "mean":
            blocks.append(x.mean(2))
        elif name == "std":
            blocks.append(x.std(2))
        elif name == "p10":
            blocks.append(percentiles[0])           # type: ignore[index]
        elif name == "p50":
            blocks.append(percentiles[1])           # type: ignore[index]
        elif name == "p90":
            blocks.append(percentiles[2])           # type: ignore[index]
        elif name == "delta_abs_mean":
            blocks.append(np.abs(delta).mean(2))    # type: ignore[union-attr]
        elif name == "delta_std":
            blocks.append(delta.std(2))             # type: ignore[union-attr]
    return np.concatenate(blocks, axis=1).astype(np.float32)


def log_mel_summary(waveform: np.ndarray, config: MelConfig = DEFAULT_CONFIG) -> np.ndarray:
    """一段波形 → 一個特徵向量（1-D）。這是偵測器的唯一輸入。"""
    return summarize_log_mel(log_mel_spectrogram(waveform, config), config)[0]


# --------------------------------------------------------------------------------------
# 四個可解讀的聲學指標
# --------------------------------------------------------------------------------------
def indicators_from_waveform(
    waveform: np.ndarray, config: MelConfig = DEFAULT_CONFIG
) -> dict[str, float]:
    """從波形量出 ``acoustics.signatures`` 定義的四個指標。

    這四個數字是給**人**看的（工程師與評審），偵測器本身用的是上面 128 維的特徵向量。
    兩者都從同一段音訊算出來，所以畫面上顯示的指標與偵測分數不會各說各話。
    """
    y = np.asarray(waveform, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=tuple(range(1, y.ndim)))
    rms = float(np.sqrt(np.mean(y ** 2))) if y.size else 0.0
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    spl_db = SPL_REF_DB + 20.0 * np.log10(max(rms, 1e-12))
    crest_db = 20.0 * np.log10(max(peak, 1e-12) / max(rms, 1e-12))

    power = power_spectrogram(y, config).mean(axis=1)          # 平均功率頻譜
    freqs = np.fft.rfftfreq(config.n_fft, d=1.0 / config.sample_rate)
    total = float(power.sum()) or 1e-12
    high_band_ratio = float(power[freqs >= HIGH_BAND_HZ].sum() / total)

    # 純音佔比：把頻譜的「局部中值」當噪音底，超出它 TONAL_PEAK_RATIO 倍的部分算純音。
    from scipy.signal import medfilt

    floor = medfilt(power.astype(np.float64), kernel_size=TONAL_MEDIAN_BINS)
    tonal_mask = power > TONAL_PEAK_RATIO * np.maximum(floor, 1e-12)
    tonal_power = float(np.clip(power - floor, 0.0, None)[tonal_mask].sum())
    tonal_ratio = float(np.clip(tonal_power / total, 0.0, 1.0))

    return {
        "spl_db": float(spl_db),
        "high_band_ratio": float(np.clip(high_band_ratio, 0.0, 1.0)),
        "tonal_ratio": tonal_ratio,
        "crest_factor_db": float(crest_db),
    }


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_STATISTICS",
    "HIGH_BAND_HZ",
    "MelConfig",
    "SPL_REF_DB",
    "SUMMARY_STATISTICS",
    "indicators_from_waveform",
    "log_mel_spectrogram",
    "log_mel_summary",
    "mel_filterbank",
    "mel_frequencies",
    "power_spectrogram",
    "summarize_log_mel",
]
