"""到達危險門檻的預估剩餘時間（time-to-threshold，TTT）。

Monitoring Agent 原本只看**瞬時斜率**：「振動每 tick 上升 0.08」這種數字對現場沒有意義，
因為它回答不了唯一重要的那個問題 —— **還有多久會踩線**。這個模組就是把斜率換成分鐘。

三條紅線和其他地方一樣：

* 只吃 **Agent 可見的觀測歷史**（和 ``/api/state.history`` 同一份資料），
  不讀孿生體的 fault / fault_progress / 真實健康度。
* 純計算，不經過 LLM。
* 用了哪個 runtime 一定標示出來：優先呼叫 ``prediction.service`` 的時序模型
  （TabFM，不可用時它自己會降級成 Ridge 基線），拿不到結果才退回線性外推。
  兩者都會寫進 ``runtime`` 欄位，畫面上看得到這個數字是誰算的。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Iterable

from .service import SUPPORTED_TARGETS, ForecastRequest, ForecastService

# 只看未來這麼久：再遠的外推沒有意義（劣化模型會變、方案也會介入）。
# 超過這個範圍就回報 None ——「很久以後可能會踩線」不是一個可以行動的資訊。
DEFAULT_HORIZON_MIN = 60.0
# 擬合斜率用的視窗長度（tick）。太短會被量測雜訊主導，太長會對剛開始的劣化不敏感。
FIT_WINDOW = 12
# 最少要幾個點才敢外推。點數太少時，量測雜訊本身就足以擬合出一條很有說服力的斜線。
MIN_POINTS = 6
# 雜訊閘門：整個擬合視窗的上升量，至少要是視窗內標準差的這個倍數，
# 才算「真的在往上走」。少了這道閘門，健康機台的量測雜訊會被外推成一個假的踩線時間。
NOISE_GATE = 2.5
# 交給時序模型細算的門檻：線性外推認為 N 分鐘內會踩線時才呼叫（呼叫一次要跑好幾次迴歸）。
FORECAST_TRIGGER_MIN = 45.0
FORECAST_MAX_HORIZON = 12

LINEAR_RUNTIME = "linear-extrapolation"


@dataclass(frozen=True)
class ThresholdEstimate:
    """某台機器「最早會踩到 CRITICAL 門檻」的估計。"""

    minutes: float | None          # None = 依目前趨勢外推不會在 horizon 內踩線
    signal: str = ""               # 最早踩線的訊號
    runtime: str = ""              # forecast:<runtime> 或 linear-extrapolation
    threshold: float | None = None
    current: float | None = None
    slope_per_min: float = 0.0
    horizon_min: float = DEFAULT_HORIZON_MIN
    per_signal: dict[str, float] = None  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_to_threshold_min": round(self.minutes, 1) if self.minutes is not None else None,
            "signal": self.signal,
            "runtime": self.runtime,
            "threshold": self.threshold,
            "current": round(self.current, 3) if self.current is not None else None,
            "slope_per_min": round(self.slope_per_min, 5),
            "horizon_min": self.horizon_min,
            "per_signal": {k: round(v, 1) for k, v in (self.per_signal or {}).items()},
        }

    def describe(self) -> str:
        if self.minutes is None:
            return f"依可觀測趨勢外推，{self.horizon_min:.0f} 分鐘內不會踩到危險門檻。"
        return (
            f"{self.signal} 目前 {self.current:.2f}，依可觀測歷史預估約 {self.minutes:.0f} 分鐘後"
            f"踩到危險門檻 {self.threshold:g}（runtime: {self.runtime}）。"
        )


def _series(history: list[dict[str, Any]], key: str) -> tuple[list[float], list[float]]:
    ticks: list[float] = []
    values: list[float] = []
    for point in history:
        if key in point and "tick" in point:
            ticks.append(float(point["tick"]))
            values.append(float(point[key]))
    return ticks, values


def _slope(values: list[float]) -> float:
    """最小平方法斜率（單位：值 / tick）。"""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom < 1e-9:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)) / denom


def _linear_minutes(values: list[float], threshold: float, rising: bool, tick_minutes: float) -> tuple[float | None, float]:
    """線性外推：還有幾分鐘會踩到門檻。回傳 (分鐘, 每分鐘斜率)。"""
    window = values[-FIT_WINDOW:]
    if len(window) < MIN_POINTS:
        return None, 0.0
    slope_per_tick = _slope(window)
    slope_per_min = slope_per_tick / tick_minutes if tick_minutes > 0 else 0.0
    current = window[-1]
    gap = (threshold - current) if rising else (current - threshold)
    if gap <= 0:
        # 已經在危險區裡。這一步必須排在趨勢判斷之前：訊號踩線之後偶爾會回檔一格，
        # 那時候斜率是負的，若先看斜率就會得到「不會踩線」—— 對一台已經超標的機器
        # 回報「安全」是這裡最不能犯的錯。
        return 0.0, slope_per_min
    move = slope_per_tick if rising else -slope_per_tick
    if move <= 0:
        return None, slope_per_min          # 沒有往門檻的方向走
    # 雜訊閘門：視窗內的總變化量要明顯大於視窗自己的雜訊，才算真的有趨勢。
    # 少了這道閘門，健康機台的量測雜訊會被外推成一個看起來很嚇人的假踩線時間。
    spread = statistics.pstdev(window) if len(window) > 1 else 0.0
    if move * (len(window) - 1) < NOISE_GATE * spread:
        return None, slope_per_min
    return gap / move * tick_minutes, slope_per_min


def _forecast_minutes(
    history: list[dict[str, Any]],
    machine_id: str,
    signal: str,
    threshold: float,
    rising: bool,
    tick_minutes: float,
    horizon_ticks: int,
    service: ForecastService,
) -> tuple[float | None, str] | None:
    """交給時序模型細算。回傳 (分鐘, runtime)；模型不可用或資料不足時回傳 None。"""
    if signal not in SUPPORTED_TARGETS:
        return None
    try:
        result = service.forecast(
            history,
            ForecastRequest(machine_id=machine_id, target=signal, horizon=horizon_ticks, model="auto"),
        )
    except Exception:
        # 預測失敗不是致命錯誤：TTT 會退回線性外推，而且 runtime 欄位會誠實標示。
        return None
    runtime = f"forecast:{result.get('runtime', 'unknown')}"
    last_tick = None
    previous = None
    for point in reversed(history):
        if f"{machine_id}.{signal}" in point:
            last_tick = float(point["tick"])
            previous = float(point[f"{machine_id}.{signal}"])
            break
    if last_tick is None or previous is None:
        return None
    for row in result.get("forecast", []):
        value = float(row["value"])
        if (rising and value >= threshold) or (not rising and value <= threshold):
            # 預測是逐 tick 的，直接回報「第幾個 tick 踩線」會讓 TTT 卡在整數分鐘上跳不下來。
            # 在踩線的那一段做線性內插，分鐘數才會隨著 tick 平順遞減。
            span = value - previous
            fraction = (threshold - previous) / span if abs(span) > 1e-9 else 1.0
            fraction = min(1.0, max(0.0, fraction))
            crossing_tick = float(row["tick"]) - 1.0 + fraction
            return max(0.0, (crossing_tick - last_tick) * tick_minutes), runtime
        previous = value
    return None, runtime


def estimate_time_to_threshold(
    history: list[dict[str, Any]],
    machine_id: str,
    specs: Iterable[Any],
    tick_minutes: float = 1.0,
    horizon_min: float = DEFAULT_HORIZON_MIN,
    service: ForecastService | None = None,
) -> ThresholdEstimate:
    """算出這台機器**最早**會踩到 CRITICAL 門檻的分鐘數。

    ``specs`` 是 ``SignalSpec``（含 critical_high / critical_low）。
    每個訊號各自外推，取最早的那一個當成這台機器的 TTT ——
    因為「還有多久要處理」由最急的那條訊號決定，不是平均值。
    """
    per_signal: dict[str, float] = {}
    candidates: list[tuple[float, str, float, float | None, float, bool]] = []

    # 第一輪：每個訊號各做一次線性外推（便宜）。
    for spec in specs:
        rising = spec.critical_high is not None
        threshold = spec.critical_high if rising else spec.critical_low
        if threshold is None:
            continue
        _, values = _series(history, f"{machine_id}.{spec.name}")
        minutes, slope_per_min = _linear_minutes(values, float(threshold), rising, tick_minutes)
        if minutes is None or minutes > horizon_min:
            continue
        per_signal[spec.name] = minutes
        candidates.append(
            (minutes, spec.name, float(threshold), values[-1] if values else None, slope_per_min, rising)
        )

    if not candidates:
        return ThresholdEstimate(minutes=None, horizon_min=horizon_min, per_signal={})

    # 第二輪：只有**最早踩線的那個訊號**才交給時序模型細算。
    # TTT 這個數字由最急的那條訊號決定，替其他訊號各跑一次迴歸只是白花時間 ——
    # Dashboard 每個 tick 都會問一次，這裡的成本是會被乘上 tick 數的。
    minutes, signal, threshold, current, slope_per_min, rising = min(candidates)
    runtime = LINEAR_RUNTIME
    # 已經踩線（minutes = 0）就不必問模型：那不是預測，是事實。
    if service is not None and 0.0 < minutes <= FORECAST_TRIGGER_MIN:
        horizon_ticks = max(4, min(FORECAST_MAX_HORIZON,
                                   int(math.ceil(minutes * 1.5 / max(tick_minutes, 1e-6)))))
        refined = _forecast_minutes(
            history, machine_id, signal, threshold, rising, tick_minutes, horizon_ticks, service,
        )
        if refined is not None and refined[0] is not None:
            minutes, runtime = refined[0], refined[1]
            per_signal[signal] = minutes

    return ThresholdEstimate(
        minutes=minutes,
        signal=signal,
        runtime=runtime,
        threshold=threshold,
        current=current,
        slope_per_min=slope_per_min,
        horizon_min=horizon_min,
        per_signal=per_signal,
    )


__all__ = [
    "DEFAULT_HORIZON_MIN",
    "FIT_WINDOW",
    "LINEAR_RUNTIME",
    "NOISE_GATE",
    "ThresholdEstimate",
    "estimate_time_to_threshold",
]
