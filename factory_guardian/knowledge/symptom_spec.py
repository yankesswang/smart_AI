"""手冊徵兆規格（Symptom Specification）與鑑別診斷規則。

⚠ 全部為競賽用合成資料（Synthetic），不代表任何真實設備商規格。

這個檔案存在的理由 —— 也是整個診斷可信度的關鍵：

**Diagnosis Agent 的排名依據必須來自手冊，不能來自模擬器的故障參數。**

早期版本的做法是把 ``FAULTS[].deltas``（模擬器用來「生成」訊號的向量）
直接除以 scale 當成故障指紋。那樣做的話，觀測向量本質上就是 ``deltas × progress ÷ scale``，
指紋是 ``deltas ÷ scale``，兩者共線 —— 餘弦相似度對純量免疫，
所以「正確答案」的餘弦恆等於 1.0。診斷不是在推論，是在查表。
評審只要問「如果手冊寫的跟實際機台不一樣呢？」，整個論證就垮了。

現在改成這樣：

* 手冊描述的是**區間**（``vibration: +1.6 ~ +4.5 mm/s``），不是點值。
  真實手冊本來就這樣寫 —— 因為同型號設備、不同劣化程度的表現本來就是一個範圍。
* 區間的中心**刻意偏離**模擬器的 deltas（15~30%）。
  手冊是設備商用他們的試驗機寫的，你廠裡這台不會剛好一樣。
* 徵兆會**重疊**：三種故障都有溫升，因為現實中它們就是都會溫升。
  分辨它們靠的不是「哪個溫度高」，而是**訊號之間的比值**。
* 因此另外有一組**鑑別診斷規則**（differential rules），
  對應真實手冊裡「關鍵鑑別點」那一段 —— 這才是工程師真正在用的判準。

比對流程也跟著改：不再是「觀測向量 vs 指紋向量」的餘弦，而是
「每個訊號的觀測偏離值，落在手冊區間內嗎？」逐項打分，再用鑑別規則調整。
落在區間內得高分，落在區間外依距離衰減。這是可解釋的 —— 前端可以逐項顯示
「振動 +2.8，手冊區間 +1.6~+4.5 ✓」，而不是一個沒有物理意義的餘弦值。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SymptomRange:
    """手冊對單一訊號的期望變化區間（相對本機基準的絕對變化量）。

    ``low`` / ``high`` 同號時代表方向明確（都正 = 上升、都負 = 下降）。
    跨越 0 代表手冊認為這個訊號「不一定會動」。
    """

    signal: str
    unit: str
    low: float
    high: float
    # 這個訊號對本故障的鑑別權重：手冊裡被列為「主要徵兆」的權重高。
    weight: float = 1.0

    @property
    def center(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def half_width(self) -> float:
        return max(1e-6, (self.high - self.low) / 2.0)

    def score(self, delta: float) -> float:
        """觀測到的變化量落在區間內嗎？回傳 0~1。

        區間外按「超出幾個半寬」以高斯衰減 —— 手冊區間是經驗值，
        略微超出不該直接判定為 0，但超得越多支持度掉得越快。

        區間內**不是**一律 1.0：越靠近區間中心分數越高（1.0 ~ 0.72）。
        理由是這些區間刻意設得寬且彼此重疊（現實就是如此），
        如果「在區間內」一律滿分，三個候選的手冊分數會同時貼在 1.0，
        這一項就失去鑑別力，排名等於全交給鑑別規則決定。
        給中心一點優勢，可以在「都符合」時仍分得出「誰更典型」。
        """
        if self.low <= delta <= self.high:
            offset = abs(delta - self.center) / self.half_width  # 0（正中心）~ 1（邊緣）
            return 1.0 - 0.28 * offset**2
        excess = (self.low - delta) if delta < self.low else (delta - self.high)
        return 0.72 * math.exp(-0.5 * (excess / self.half_width) ** 2)

    def describe(self) -> str:
        sign = lambda v: f"{v:+g}"  # noqa: E731
        return f"{self.signal} {sign(self.low)}~{sign(self.high)} {self.unit}"


@dataclass(frozen=True)
class DifferentialRule:
    """鑑別診斷規則：兩個訊號的比值用來區分容易混淆的故障。

    對應真實手冊「關鍵鑑別點」那一段，例如：
      「ΔVibration / ΔTemperature > 0.35 → 軸承劣化；< 0.10 → 冷卻失效」

    這是現實中工程師真正用的判準 —— 因為絕對值會被劣化程度、
    環境溫度、負載影響，但**比值**相對穩定。
    """

    numerator: str
    denominator: str
    low: float
    high: float
    statement: str
    weight: float = 1.0

    def score(self, deltas: dict[str, float]) -> float | None:
        """回傳 0~1 的符合度；分母太小（訊號沒動）時回傳 None 代表不適用。"""
        den = deltas.get(self.denominator, 0.0)
        num = deltas.get(self.numerator, 0.0)
        if abs(den) < 1e-6:
            return None
        ratio = num / den
        if self.low <= ratio <= self.high:
            return 1.0
        half = max(1e-6, (self.high - self.low) / 2.0)
        excess = (self.low - ratio) if ratio < self.low else (ratio - self.high)
        return math.exp(-0.5 * (excess / half) ** 2)

    def describe(self, deltas: dict[str, float]) -> str:
        den = deltas.get(self.denominator, 0.0)
        num = deltas.get(self.numerator, 0.0)
        ratio = "n/a" if abs(den) < 1e-6 else f"{num / den:.2f}"
        return f"Δ{self.numerator}/Δ{self.denominator} = {ratio}（手冊 {self.low:g}~{self.high:g}）"


@dataclass(frozen=True)
class FaultSymptomSpec:
    """一種故障在手冊裡的完整徵兆描述。"""

    fault_id: str
    label: str
    manual_ref: str
    ranges: tuple[SymptomRange, ...]
    differentials: tuple[DifferentialRule, ...] = ()
    # 溫升的時間特性：cooling 是十餘分鐘內急升，bearing 是數小時緩升。
    # 這是手冊裡另一條關鍵鑑別點，用觀測到的變化速率比對。
    temp_rise_per_10min: tuple[float, float] | None = None
    onset_note: str = ""

    def range_of(self, signal: str) -> SymptomRange | None:
        return next((r for r in self.ranges if r.signal == signal), None)


# --------------------------------------------------------------------------------------
# 三種故障的手冊徵兆規格
#
# 對照模擬器的 deltas（Agent 看不到，僅供開發者檢查耦合是否已切斷）：
#   bearing:  vib +6.8  temp +16.0  cur +2.2  rpm -4.0
#   cooling:  temp +26.0  vib +0.5  cur +0.6  rpm -1.5
#   overload: cur +6.0  temp +13.0  rpm -18.0  vib +1.5
#
# 下面的區間中心刻意偏離這些值（見每條的註解），且區間彼此重疊。
# 三種故障都有溫升 —— 分辨靠的是 differentials 的比值，不是絕對值。
# --------------------------------------------------------------------------------------
SYMPTOM_SPECS: dict[str, FaultSymptomSpec] = {
    "bearing_degradation": FaultSymptomSpec(
        fault_id="bearing_degradation",
        label="主軸軸承劣化 (Bearing Degradation)",
        manual_ref="MAN-A-3.2",
        ranges=(
            # 模擬器 +6.8；手冊中心 +5.5（-19%）。手冊是設備商試驗機的數據。
            SymptomRange("vibration", "mm/s", low=1.8, high=9.2, weight=1.0),
            # 模擬器 +16.0；手冊中心 +12.5（-22%）。與 cooling 的區間大幅重疊。
            SymptomRange("temperature", "°C", low=4.0, high=21.0, weight=0.6),
            # 模擬器 +2.2；手冊中心 +1.7（-23%）
            SymptomRange("current", "A", low=0.3, high=3.1, weight=0.5),
            # 模擬器 -4.0；手冊中心 -3.0
            SymptomRange("rpm_pct", "%", low=-6.5, high=0.5, weight=0.4),
        ),
        differentials=(
            DifferentialRule(
                "vibration", "temperature", low=0.28, high=1.60,
                statement="振動上升幅度顯著大於溫升，是軸承劣化與冷卻失效的主要分界。",
                weight=1.4,
            ),
            DifferentialRule(
                "rpm_pct", "current", low=-3.2, high=-0.4,
                statement="電流小幅上升但轉速僅略降；若轉速大幅掉落應優先考慮馬達過載。",
                weight=0.8,
            ),
        ),
        temp_rise_per_10min=(0.8, 6.5),
        onset_note="振動先動，溫度隨摩擦生熱緩慢跟上，典型發展以小時計。",
    ),
    "cooling_failure": FaultSymptomSpec(
        fault_id="cooling_failure",
        label="冷卻系統失效 (Cooling Failure)",
        manual_ref="MAN-A-4.1",
        ranges=(
            # 模擬器 +26.0；手冊中心 +21.0（-19%）
            SymptomRange("temperature", "°C", low=9.0, high=33.0, weight=1.0),
            # 模擬器 +0.5；手冊允許 0~+2.2 —— 冷卻失效時振動「幾乎不動」但不是完全不動
            SymptomRange("vibration", "mm/s", low=-0.3, high=2.2, weight=0.9),
            # 模擬器 +0.6；手冊中心 +0.45（-25%）
            SymptomRange("current", "A", low=-0.5, high=1.4, weight=0.5),
            # 模擬器 -1.5；手冊中心 -1.15（-23%）
            SymptomRange("rpm_pct", "%", low=-3.1, high=0.8, weight=0.4),
        ),
        differentials=(
            DifferentialRule(
                "vibration", "temperature", low=-0.05, high=0.14,
                statement="溫度單獨異常、振動幾乎不動，是冷卻失效的關鍵指標。",
                weight=1.4,
            ),
            DifferentialRule(
                "current", "temperature", low=-0.03, high=0.09,
                statement="電流不隨溫度上升，代表機械負載未增加，熱源來自冷卻不足而非做功。",
                weight=1.0,
            ),
        ),
        temp_rise_per_10min=(7.0, 26.0),
        onset_note="溫度急升，可在十餘分鐘內從正常升到危險區間 —— 上升速率本身就是鑑別點。",
    ),
    "motor_overload": FaultSymptomSpec(
        fault_id="motor_overload",
        label="主軸馬達過載 (Motor Overload)",
        manual_ref="MAN-A-5.3",
        ranges=(
            # 模擬器 +6.0；手冊中心 +4.6（-23%）
            SymptomRange("current", "A", low=1.2, high=8.0, weight=1.0),
            # 模擬器 -18.0；手冊中心 -13.5（-25%）
            SymptomRange("rpm_pct", "%", low=-24.0, high=-3.0, weight=1.0),
            # 模擬器 +13.0；手冊中心 +10.0。與 bearing 區間大幅重疊。
            SymptomRange("temperature", "°C", low=2.0, high=18.0, weight=0.5),
            # 模擬器 +1.5；手冊中心 +1.15（-23%）
            SymptomRange("vibration", "mm/s", low=-0.3, high=2.6, weight=0.4),
        ),
        differentials=(
            DifferentialRule(
                "rpm_pct", "current", low=-6.0, high=-1.1,
                statement="電流上升與轉速下降同時發生，是與軸承劣化最主要的區別。",
                weight=1.5,
            ),
            DifferentialRule(
                "vibration", "current", low=-0.10, high=0.55,
                statement="振動相對電流的上升幅度有限，機械性劣化不是主因。",
                weight=0.9,
            ),
        ),
        temp_rise_per_10min=(2.0, 11.0),
        onset_note="電流與轉速幾乎同時變化（電氣響應快），溫升是後果而非主因。",
    ),
}


def spec_for(fault_id: str) -> FaultSymptomSpec | None:
    return SYMPTOM_SPECS.get(fault_id)


__all__ = [
    "SymptomRange",
    "DifferentialRule",
    "FaultSymptomSpec",
    "SYMPTOM_SPECS",
    "spec_for",
]
