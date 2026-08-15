"""Digital Twin 的合成聲學觀測。

# ⚠️ 誠實邊界（本專案的可信度核心，讀這個模組前務必先讀完這段）

本模組產生的**全部**是合成音訊特徵，由 `twin/faults.py` 既有的振動／轉速／電流物理推導而來。

* 它**不是**真實錄音，也**不是**用真實錄音訓練出來的生成模型。
* Demo 畫面上的每一個聲學數字都來自這裡，因此都是合成的，且一律標記
  `synthetic=True`，與 Sensor / Orders / Manual / History 的處理方式一致。
* `data/external/` 底下那份 **DCASE2020 / MIMII 真實泵浦錄音，只用來驗證偵測器有效**
  （見 `acoustics/dcase.py` 與 `docs/acoustic_validation.md`），
  **完全不參與 Demo 閉環**，也沒有任何一個 Demo 數字來自它。

這兩件事必須分得清清楚楚。程式上的保證是：`acoustics/dcase.py` 不被
`twin/`、`agents/`、`api/` 的任何一處匯入 —— 真實資料在架構上就到不了 Demo。

# 那合成聲音還有什麼意義？

一句話：**它證明介面是通的，不證明數字是真的。**

合成的聲學指標是振動物理的重新編碼，在這個 Demo 上它與振動高度相關，
所以它**補強**（corroborate）而不是**新增**（add）獨立證據。
Diagnosis Agent 因此把聲音的有效權重壓在 0.15（見 `agents/diagnosis.py`），
不讓一個推導出來的訊號蓋過真的量到的訊號。

真正有價值的是「真實工廠換上真麥克風之後會怎樣」，而那個問題由 DCASE 那份驗證回答：
同一個 `log_mel_summary()`、同一個 `AcousticAnomalyDetector`，在真實泵浦錄音上
AUC 0.903 / pAUC 0.785，高於官方 baseline 的 0.726 / 0.600。
導入時要換掉的只有 `fit()` 吃進去的那批音訊，程式一行都不用改。

# 兩條路徑

1. **每 tick 的指標**（`target_indicators`）—— 純 Python 閉式運算，沒有 numpy，
   微秒級。Digital Twin 每個 tick 對每台加工機呼叫一次，成本可以忽略。
2. **波形渲染**（`synthesize_waveform`）—— 把同一組指標「畫」成一段真的可以聽、
   可以做 FFT 的波形，讓 DCASE 驗證用的那個特徵抽取器可以直接跑在它上面。
   這條路徑**不在每 tick 的迴圈裡**（它要做 FFT，會拖垮測試），
   只在需要展示或測試「兩條路徑接得起來」時呼叫。

第 2 條路徑是第 1 條的**渲染器**，不是獨立的第二套物理：
`target_indicators` 是模型，`synthesize_waveform` 把模型畫出來，
`features.indicators_from_waveform` 再把畫出來的東西量回去 —— 三者形成一個
可被測試檢查的閉環（見 `tests/test_acoustics.py`）。
"""

from __future__ import annotations

from typing import Any

from .signatures import (
    ACOUSTIC_RESPONSE,
    DERATE_SPL_DELTA_DB,
    INDICATOR_NAMES,
    NOMINAL_INDICATORS,
    OFFLINE_INDICATORS,
)

# 指標的物理上下限（渲染器與孿生體都要遵守）。
INDICATOR_BOUNDS: dict[str, tuple[float, float]] = {
    "spl_db": (30.0, 120.0),
    "high_band_ratio": (0.01, 0.90),
    "tonal_ratio": (0.0, 0.90),
    "crest_factor_db": (3.0, 30.0),
}

# --- 波形渲染器的音源設定（假設參數，量級取自一般工具機的頻譜結構）---------------------
# 主軸轉頻及其諧波、冷卻泵葉片通過頻率、電源線頻及其諧波。全部在 2 kHz 以下，
# 所以它們只貢獻 tonal_ratio 與低頻能量，不影響 high_band_ratio。
TONE_FREQUENCIES_HZ: tuple[float, ...] = (47.0, 60.0, 120.0, 180.0, 240.0, 360.0, 720.0)
# 軸承缺陷通過頻率（BPFO 量級）與它激起的結構共振。
DEFECT_RATE_HZ: float = 105.0
DEFECT_RESONANCE_HZ: float = 4200.0
DEFECT_DECAY_S: float = 0.0012
# 滾動體滑移（slip）造成的週期抖動，與負載區造成的振幅調變。
# 這兩個**不是**為了好看才加的：真實軸承的 BPFO 本來就不是嚴格週期
# （滾動體會滑移 1–2%），滾動體通過負載區時的撞擊力也比通過非負載區大。
# 少了它們，衝擊串會是一個嚴格週期訊號 —— 頻譜上是一排等距的譜線，
# 於是「純音佔比」偵測器會把整串衝擊算成純音（實測 tonal_ratio 會衝到 0.96），
# 明明它在物理上是寬頻衝擊。加了抖動之後譜線才會化開成寬頻。
DEFECT_SLIP: float = 0.02
DEFECT_LOAD_MODULATION: float = 0.35


def target_indicators(
    fault_id: str | None,
    progress: float,
    *,
    online: bool = True,
    derated: bool = False,
    fault_relief: float = 1.0,
) -> dict[str, float]:
    """這台機器在目前狀態下，麥克風「應該」聽到的四個指標（穩態目標值）。

    結構刻意與 `twin.engine.FactoryTwin._target_signal` 一模一樣：
    停機 → 背景值；降速 → 音壓下降；有故障 → 名目值加上 delta × progress × relief。
    孿生體拿到這個目標值之後，會再套一階遲滯與量測雜訊，才變成 Agent 看到的觀測。
    """
    if not online:
        return dict(OFFLINE_INDICATORS)

    values = dict(NOMINAL_INDICATORS)
    if derated:
        values["spl_db"] += DERATE_SPL_DELTA_DB

    response = ACOUSTIC_RESPONSE.get(fault_id or "")
    if response is not None:
        for name in INDICATOR_NAMES:
            values[name] += response.deltas.get(name, 0.0) * progress * fault_relief

    return {name: clamp_indicator(name, values[name]) for name in INDICATOR_NAMES}


def clamp_indicator(name: str, value: float) -> float:
    low, high = INDICATOR_BOUNDS.get(name, (float("-inf"), float("inf")))
    return max(low, min(high, value))


# --------------------------------------------------------------------------------------
# 波形渲染器（需要 numpy；不在每 tick 的路徑上）
# --------------------------------------------------------------------------------------
def synthesize_waveform(
    indicators: dict[str, float],
    duration_s: float = 2.0,
    sample_rate: int = 16_000,
    seed: int = 20260809,
) -> Any:
    """把四個聲學指標渲染成一段波形。

    ⚠️ 合成音訊，不是錄音。用途是讓 DCASE 驗證用的特徵抽取器可以直接跑在孿生體上，
    證明兩條路徑接得起來。

    合成方式是把訊號拆成四個物理成分，再依指標分配各自的功率：

    * **純音**（主軸／冷卻泵／線頻諧波）→ 決定 ``tonal_ratio``
    * **高頻寬頻噪音**（> 2 kHz）→ 與衝擊一起決定 ``high_band_ratio``
    * **衝擊串**（軸承缺陷以 BPFO 敲擊，激起 4.2 kHz 結構共振後指數衰減）
      → 決定 ``crest_factor_db``，同時也落在高頻帶
    * **低頻寬頻噪音** → 補足剩下的能量

    衝擊的功率佔比用二分搜尋逼近目標波峰因數（閉式解不存在，因為峰值取的是 max）。
    整段最後正規化到 ``spl_db`` 對應的 RMS（1 Pa = 94 dB SPL 的麥克風校正慣例）。
    """
    import numpy as np

    from .features import HIGH_BAND_HZ, SPL_REF_DB

    n = max(int(round(duration_s * sample_rate)), sample_rate // 8)
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=np.float64) / sample_rate

    spl_db = clamp_indicator("spl_db", float(indicators.get("spl_db", NOMINAL_INDICATORS["spl_db"])))
    tonal = clamp_indicator("tonal_ratio", float(indicators.get("tonal_ratio", NOMINAL_INDICATORS["tonal_ratio"])))
    high = clamp_indicator("high_band_ratio", float(indicators.get("high_band_ratio", NOMINAL_INDICATORS["high_band_ratio"])))
    crest = clamp_indicator("crest_factor_db", float(indicators.get("crest_factor_db", NOMINAL_INDICATORS["crest_factor_db"])))
    if tonal + high > 0.95:                       # 兩個佔比不能把總能量吃光
        scale = 0.95 / (tonal + high)
        tonal, high = tonal * scale, high * scale

    def unit(x: np.ndarray) -> np.ndarray:
        rms = float(np.sqrt(np.mean(x ** 2)))
        return x / rms if rms > 1e-12 else x

    def band(x: np.ndarray, low_pass: bool) -> np.ndarray:
        spectrum = np.fft.rfft(x)
        freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
        spectrum[freqs >= HIGH_BAND_HZ if low_pass else freqs < HIGH_BAND_HZ] = 0.0
        return np.fft.irfft(spectrum, n=n)

    # 1) 純音：等功率分配到各諧波，相位由 seed 決定（決定性）。
    phases = rng.uniform(0.0, 2.0 * np.pi, size=len(TONE_FREQUENCIES_HZ))
    tones = unit(sum(np.sin(2.0 * np.pi * f * t + p) for f, p in zip(TONE_FREQUENCIES_HZ, phases)))

    # 2) 寬頻噪音：同一段白噪音切成低頻與高頻兩半，避免兩者相關。
    noise = rng.standard_normal(n)
    low_noise = unit(band(noise, low_pass=True))
    high_noise = unit(band(rng.standard_normal(n), low_pass=False))

    # 3) 衝擊串：每次滾動體通過缺陷 → 敲一下 → 結構共振指數衰減。
    #    週期帶滑移抖動、振幅帶負載區調變（見 DEFECT_SLIP / DEFECT_LOAD_MODULATION）。
    impulses = np.zeros(n)
    period = max(int(round(sample_rate / DEFECT_RATE_HZ)), 1)
    burst_len = min(int(round(6.0 * DEFECT_DECAY_S * sample_rate)), n)
    bt = np.arange(burst_len, dtype=np.float64) / sample_rate
    burst = np.exp(-bt / DEFECT_DECAY_S) * np.sin(2.0 * np.pi * DEFECT_RESONANCE_HZ * bt)
    position = 0.0
    while position < n - burst_len:
        start = int(position)
        gain = 1.0 + DEFECT_LOAD_MODULATION * rng.standard_normal()
        impulses[start:start + burst_len] += max(0.1, gain) * burst
        position += period * (1.0 + DEFECT_SLIP * rng.standard_normal())
    impulses = unit(impulses) if np.any(impulses) else impulses
    impulse_high_share = float(np.mean(band(impulses, low_pass=False) ** 2))

    def build(impulse_power: float) -> np.ndarray:
        # 高頻帶的能量由「衝擊落在高頻的部分」與「高頻噪音」共同組成。
        high_noise_power = max(0.0, high - impulse_power * impulse_high_share)
        low_power = max(0.0, 1.0 - tonal - high_noise_power - impulse_power)
        return (
            np.sqrt(tonal) * tones
            + np.sqrt(high_noise_power) * high_noise
            + np.sqrt(impulse_power) * impulses
            + np.sqrt(low_power) * low_noise
        )

    def crest_of(x: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(x ** 2)))
        return 20.0 * np.log10(max(float(np.max(np.abs(x))), 1e-12) / max(rms, 1e-12))

    # 二分搜尋衝擊功率佔比，逼近目標波峰因數。單調遞增（衝擊越強、峰值越尖）。
    lo, hi = 0.0, min(0.85, max(0.0, 1.0 - tonal))
    if crest_of(build(hi)) <= crest:
        impulse_power = hi
    elif crest_of(build(lo)) >= crest:
        impulse_power = lo
    else:
        # 14 次二分把區間收到 2⁻¹⁴，遠細於波峰因數本身的量測不確定度。
        for _ in range(14):
            mid = 0.5 * (lo + hi)
            if crest_of(build(mid)) < crest:
                lo = mid
            else:
                hi = mid
        impulse_power = 0.5 * (lo + hi)

    waveform = unit(build(impulse_power))
    return (waveform * 10.0 ** ((spl_db - SPL_REF_DB) / 20.0)).astype("float32")


__all__ = [
    "DEFECT_DECAY_S",
    "DEFECT_RATE_HZ",
    "DEFECT_RESONANCE_HZ",
    "INDICATOR_BOUNDS",
    "TONE_FREQUENCIES_HZ",
    "clamp_indicator",
    "synthesize_waveform",
    "target_indicators",
]
