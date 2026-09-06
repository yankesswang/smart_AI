"""機器聲音的「故障指紋」知識與模擬器物理。

本檔案刻意複製 ``twin/faults.py`` 的結構，因為它要守住同一條紅線：

* ``ACOUSTIC_RESPONSE`` —— **Simulator 內部**用的物理效果（故障如何改變聲音），
  Agent 看不到，只有 ``twin/engine.py`` 的麥克風渲染器會讀。
* ``acoustic_signatures()`` —— **Diagnosis Agent 可以看到**的手冊知識
  （「軸承劣化時會聽到什麼」），來源等同工程師手上的設備手冊。

兩者在 Demo 中一致是合理的（手冊本來就描述故障徵兆），但它是「知識」不是「答案」：
Agent 仍須從帶雜訊的麥克風指標比對出最像的那一個。

---

## 四個聲學指標，以及為什麼是這四個

麥克風每個觀測窗回報四個純量。它們不是隨便挑的，每一個都對應一種**不同的**物理機制，
所以三種故障在這個四維空間裡是分得開的（見下方 profile 的兩兩餘弦）：

| 指標 | 物理意義 | 為什麼需要它 |
|---|---|---|
| ``spl_db`` | 整體音壓級（dB SPL） | 能量總量，最粗但最穩健 |
| ``high_band_ratio`` | 2–8 kHz 能量佔比 | 滾動體剝落／點蝕的能量集中在高頻，低頻聽不出來 |
| ``tonal_ratio`` | 諧波（純音）能量佔比 | **冷卻泵停了會少一個 tone**；馬達過載則會多出線頻諧波 |
| ``crest_factor_db`` | 峰值／RMS（衝擊性） | 軸承缺陷是週期性衝擊，平均能量看不出來、波峰因數看得出來 |

``tonal_ratio`` 是這一組裡最有價值的一個，理由是它補上了感測器指紋補不上的鑑別力：
**冷卻失效與馬達過載都會推高溫度**，溫度訊號本身無法區分；但前者是「泵停了 → 少一個 tone」，
後者是「負載上升 → 多出線頻諧波」，方向完全相反。這兩個故障的聲學 profile 餘弦是 **−0.89**
（幾乎相反向量），而它們的感測器指紋餘弦則相當接近。這就是「多模態」的具體價值，
不是把資料堆在一起而已。

> **SYNTHETIC DEMO DATA** —— 本檔案的 nominal 值與 delta 值皆為競賽用假設參數，
> 由公開文獻對滾動軸承故障聲學特徵的一般性描述設定量級，**不代表任何真實設備商規格**。
> 偵測器本身的有效性是用**外部真實工業錄音**（DCASE2020 Task2 / MIMII pump）驗證的，
> 見 ``docs/factory_guardian/acoustic_validation.md``；那份驗證與這裡的合成參數完全獨立。
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------------------
# 健康機台的名目聲學狀態（假設參數）
# --------------------------------------------------------------------------------------
# 量級參考：中型立式綜合加工中心運轉中，1 公尺處約 78 dB(A)。
NOMINAL_INDICATORS: dict[str, float] = {
    "spl_db": 78.0,
    "high_band_ratio": 0.22,
    "tonal_ratio": 0.45,
    "crest_factor_db": 9.5,
}

# 正規化尺度：一個「訊號單位」的嚴重程度。
# 與 twin/topology.py 的 SignalSpec.scale 同樣的角色 —— 讓四個不同單位的指標
# 可以放進同一個向量空間做餘弦比對。
INDICATOR_SCALES: dict[str, float] = {
    "spl_db": 6.0,
    "high_band_ratio": 0.12,
    "tonal_ratio": 0.12,
    "crest_factor_db": 5.0,
}

# 指標順序固定，讓向量化與稽核輸出都是決定性的。
INDICATOR_NAMES: tuple[str, ...] = ("spl_db", "high_band_ratio", "tonal_ratio", "crest_factor_db")

INDICATOR_UNITS: dict[str, str] = {
    "spl_db": "dB",
    "high_band_ratio": "",
    "tonal_ratio": "",
    "crest_factor_db": "dB",
}

# 機台停機／維修中時麥克風聽到的背景值（現場環境噪音）。
OFFLINE_INDICATORS: dict[str, float] = {
    "spl_db": 52.0,
    "high_band_ratio": 0.12,
    "tonal_ratio": 0.06,
    "crest_factor_db": 7.5,
}

# 降速運轉：主軸轉速降低 → 整體音壓下降，頻譜結構大致不變。
DERATE_SPL_DELTA_DB: float = -4.0


# --------------------------------------------------------------------------------------
# Simulator 內部物理（Agent 看不到）
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AcousticResponse:
    """一種故障在 progress = 1.0 時，對四個聲學指標的絕對偏移量。"""

    fault_id: str
    deltas: dict[str, float]
    note: str = ""


ACOUSTIC_RESPONSE: dict[str, AcousticResponse] = {
    "bearing_degradation": AcousticResponse(
        fault_id="bearing_degradation",
        deltas={"spl_db": 8.0, "high_band_ratio": 0.18, "tonal_ratio": -0.04, "crest_factor_db": 7.0},
        note="滾動體通過缺陷 → 週期性衝擊：寬頻高頻能量大幅上升，波峰因數顯著上升。",
    ),
    "cooling_failure": AcousticResponse(
        fault_id="cooling_failure",
        deltas={"spl_db": -2.0, "high_band_ratio": -0.02, "tonal_ratio": -0.22, "crest_factor_db": 0.5},
        note="冷卻泵停止 → 少掉一個穩定純音：整體音壓略降，諧波佔比大幅下降。",
    ),
    "motor_overload": AcousticResponse(
        fault_id="motor_overload",
        deltas={"spl_db": 5.0, "high_band_ratio": 0.01, "tonal_ratio": 0.14, "crest_factor_db": 0.8},
        note="負載上升 → 線頻與其諧波的電磁噪音增強：諧波佔比上升，高頻幾乎不變。",
    ),
}


# --------------------------------------------------------------------------------------
# Agent 可見的手冊知識
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AcousticSignature:
    """Diagnosis Agent 用來比對的正規化聲學指紋。"""

    fault_id: str
    profile: dict[str, float]
    note: str = ""


def acoustic_signatures(scales: dict[str, float] | None = None) -> dict[str, AcousticSignature]:
    """把聲學響應轉成 Diagnosis Agent 使用的正規化指紋（delta / scale）。

    與 ``twin.faults.fault_signatures()`` 完全相同的做法，理由也相同：
    dB、比例、dB 三種單位必須先正規化才能放進同一個向量空間比較。
    """
    scale = scales or INDICATOR_SCALES
    return {
        fid: AcousticSignature(
            fault_id=fid,
            profile={
                name: response.deltas.get(name, 0.0) / scale.get(name, 1.0)
                for name in INDICATOR_NAMES
            },
            note=response.note,
        )
        for fid, response in ACOUSTIC_RESPONSE.items()
    }


def deviation_vector(indicators: dict[str, float], scales: dict[str, float] | None = None) -> dict[str, float]:
    """觀測偏離向量：(觀測值 − 名目值) / scale。與感測器指紋的定義一致。"""
    scale = scales or INDICATOR_SCALES
    return {
        name: (indicators.get(name, NOMINAL_INDICATORS[name]) - NOMINAL_INDICATORS[name]) / scale.get(name, 1.0)
        for name in INDICATOR_NAMES
    }


def cosine(observed: dict[str, float], profile: dict[str, float]) -> float:
    keys = set(observed) | set(profile)
    dot = sum(observed.get(k, 0.0) * profile.get(k, 0.0) for k in keys)
    n1 = sum(observed.get(k, 0.0) ** 2 for k in keys) ** 0.5
    n2 = sum(profile.get(k, 0.0) ** 2 for k in keys) ** 0.5
    if n1 < 1e-9 or n2 < 1e-9:
        return 0.0
    return dot / (n1 * n2)


def strength(observed: dict[str, float]) -> float:
    """偏離向量長度 —— 聲學訊號有多強。用於「要不要相信聲音」的閘門。"""
    return sum(v * v for v in observed.values()) ** 0.5


__all__ = [
    "ACOUSTIC_RESPONSE",
    "AcousticResponse",
    "AcousticSignature",
    "DERATE_SPL_DELTA_DB",
    "INDICATOR_NAMES",
    "INDICATOR_SCALES",
    "INDICATOR_UNITS",
    "NOMINAL_INDICATORS",
    "OFFLINE_INDICATORS",
    "acoustic_signatures",
    "cosine",
    "deviation_vector",
    "strength",
]
