"""Monitoring Agent（規格 §4.1）。

職責：即時監控、Equipment Health Score、異常趨勢與 Failure Risk，並觸發 Anomaly Event。

重要：這個 Agent 只看得到**帶量測雜訊的感測器讀值**。
它自己維護滑動視窗、自己做平滑、自己算健康度 —— 不會去讀 Simulator 的真實健康度或故障標籤。
所以它算出來的 health 會和孿生體的真實 health 有小幅落差，這是正確的行為。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque

from ..domain import (
    AnomalyEvent,
    FactorySnapshot,
    MachineSnapshot,
    Severity,
    SignalBand,
)
from ..twin.topology import HEALTH_FULL_SCALE, HEALTH_WEIGHTS
from .base import Agent

WINDOW = 6                    # 滑動視窗長度（tick）
TREND_MIN_SLOPE = 0.06        # 正規化偏離量 / tick，超過視為明顯上升趨勢
HEALTH_WARN = 88.0
HEALTH_ALARM = 75.0
HEALTH_TREND_GATE = 97.0      # 趨勢告警的健康度前提

# --- 預測式告警 -------------------------------------------------------------
# 趨勢告警看的是「現在正在變壞」，預測告警看的是「照這個走勢，未來會壞到哪」。
# 兩者都只在健康度已經開始下滑時才採信（HEALTH_TREND_GATE），避免健康機台的
# 量測雜訊被外推成假警報 —— 這是 False Positive KPI 的主要防線。
FORECAST_MIN_POINTS = 8       # 特徵表要有 lag_3 / rolling_6，少於 8 點外推不可靠
FORECAST_HORIZON = 12         # 往前看幾個 tick
FORECAST_ALARM = HEALTH_ALARM  # 預測健康度低於此值就提前示警
# 閉環用 Ridge 而非 TabFM：TabFM 單步準確，但遞迴 12 步時會回歸 context 均值，
# 在「剛開始劣化」的序列上預測機台自己好轉——而那正是預測告警唯一有價值的時間窗。
# Ridge 的「趨勢會持續」假設反而符合劣化的物理行為。見 docs/forecast-model-evaluation.md。
FORECAST_MODEL = "ridge"
# Ridge 一次 12 步約 4 ms，每 tick 重算不影響閉環（TabFM 則需 0.5~7.7 秒而必須降頻）。
FORECAST_EVERY = 1


@dataclass
class SignalWindow:
    values: Deque[float] = field(default_factory=lambda: deque(maxlen=WINDOW))

    def push(self, value: float) -> None:
        self.values.append(value)

    @property
    def smoothed(self) -> float:
        """視窗內的加權平均（越新權重越高），用來壓掉量測雜訊。"""
        if not self.values:
            return 0.0
        weights = [i + 1 for i in range(len(self.values))]
        return sum(v * w for v, w in zip(self.values, weights)) / sum(weights)

    def slope(self) -> float:
        """最小平方法斜率（單位：值/tick）。"""
        n = len(self.values)
        if n < 3:
            return 0.0
        xs = list(range(n))
        mean_x = sum(xs) / n
        mean_y = sum(self.values) / n
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom < 1e-9:
            return 0.0
        return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, self.values)) / denom


class MonitoringAgent(Agent):
    """異常偵測與健康度評分。

    ``mode="threshold_only"`` 會退化成 Baseline A：只有固定門檻告警，
    沒有趨勢、沒有健康度、沒有跨訊號綜合判斷。
    """

    name = "monitoring-agent"
    role = "監測 / 異常觸發"

    def __init__(self, ctx=None, mode: str = "full", forecaster=None) -> None:
        super().__init__(ctx)
        self.mode = mode
        self.windows: dict[str, dict[str, SignalWindow]] = {}
        self.event_seq = 0
        self.fired: set[str] = set()
        self.events: list[AnomalyEvent] = []   # 全部觸發過的事件，供 False Positive KPI 統計
        # 預測式告警的輸入：Agent 自己算出來的健康度序列，不是模擬器的真實健康度。
        # 沒有注入 forecaster 就完全不做預測（Baseline 與既有測試維持原行為）。
        self.forecaster = forecaster
        self.health_history: dict[str, list[float]] = {}
        self.last_forecast: dict[str, dict] = {}

    # ------------------------------------------------------------------ 觀測
    def observe(self, snapshot: FactorySnapshot) -> None:
        for mid, machine in snapshot.machines.items():
            per_machine = self.windows.setdefault(mid, {})
            for name, reading in machine.readings.items():
                per_machine.setdefault(name, SignalWindow()).push(reading.value)

    def smoothed_readings(self, machine: MachineSnapshot) -> dict[str, float]:
        windows = self.windows.get(machine.machine_id, {})
        return {
            name: windows[name].smoothed if name in windows and windows[name].values else reading.value
            for name, reading in machine.readings.items()
        }

    # ------------------------------------------------------------------ 健康度
    def health_estimate(self, machine: MachineSnapshot, specs) -> float:
        """Agent 自己從觀測值算出的 Equipment Health Score（不是讀模擬器的）。"""
        smoothed = self.smoothed_readings(machine)
        severity = 0.0
        for spec in specs:
            value = smoothed.get(spec.name)
            if value is None:
                continue
            severity += HEALTH_WEIGHTS.get(spec.name, 0.0) * min(2.0, spec.deviation(value))
        return max(0.0, min(100.0, 100.0 * (1.0 - severity / HEALTH_FULL_SCALE)))

    def failure_risk(self, machine: MachineSnapshot, specs) -> float:
        """Failure Risk：綜合目前健康度與惡化速度，估 0~1 的短期故障風險。"""
        health = self.health_estimate(machine, specs)
        trend = self.worst_trend(machine, specs)
        base = max(0.0, (100.0 - health) / 100.0)
        return max(0.0, min(1.0, base + max(0.0, trend) * 1.6))

    # ------------------------------------------------------------------ 預測
    def forecast_health(self, machine_id: str) -> dict | None:
        """外推自算健康度，回傳未來視野內的最低點。

        預測是輔助證據，不是控制路徑：任何失敗都只是讓這次沒有預測可用，
        絕不能讓偵測本身中斷，所以這裡把例外整個吞掉並記錄原因。

        每 ``FORECAST_EVERY`` tick 才真的重算一次，其餘 tick 沿用快取。
        Ridge 夠快所以目前設為 1；換成推論昂貴的模型時調大此值即可降頻。
        """
        if self.forecaster is None:
            return None
        series = self.health_history.get(machine_id, [])
        if len(series) < FORECAST_MIN_POINTS:
            return None
        cached = self.last_forecast.get(machine_id)
        if cached and len(series) - cached.get("at_point", -99) < FORECAST_EVERY:
            # 沿用上次結果；error 快取同樣要沿用，否則失敗會每 tick 重試。
            return None if "error" in cached else cached
        try:
            # 閉環固定用 Ridge，不走 auto：TabFM 在「剛開始劣化」的序列上會均值回歸、
            # 預測機台自己好轉，正好是預測告警唯一有價值的時間窗。
            # 完整實驗數據見 docs/forecast-model-evaluation.md。
            result = self.forecaster.forecast_series(
                series, horizon=FORECAST_HORIZON, model=FORECAST_MODEL
            )
        except Exception as exc:  # noqa: BLE001 - 預測失敗不得影響偵測
            self.last_forecast[machine_id] = {
                "error": f"{type(exc).__name__}: {exc}", "at_point": len(series),
            }
            return None
        predicted = result.get("predicted") or []
        if not predicted:
            self.last_forecast[machine_id] = {"error": "empty forecast", "at_point": len(series)}
            return None
        info = {
            "predicted_min": min(predicted),
            "predicted_end": predicted[-1],
            "horizon": FORECAST_HORIZON,
            "runtime": result.get("runtime", "unknown"),
            "fallback": result.get("fallback", False),
            "at_point": len(series),
        }
        self.last_forecast[machine_id] = info
        return info

    def worst_trend(self, machine: MachineSnapshot, specs) -> float:
        """所有訊號中最強的『往壞的方向走』的正規化斜率。"""
        windows = self.windows.get(machine.machine_id, {})
        worst = 0.0
        for spec in specs:
            window = windows.get(spec.name)
            if window is None or len(window.values) < 3:
                continue
            slope = window.slope() / spec.scale
            # rpm 是「越低越糟」，方向要反過來
            if spec.name == "rpm_pct":
                slope = -slope
            worst = max(worst, slope)
        return worst

    # ------------------------------------------------------------------ 觸發
    def detect(self, snapshot: FactorySnapshot, topology) -> list[AnomalyEvent]:
        self.observe(snapshot)
        events: list[AnomalyEvent] = []
        with self.timed():
            for mid, machine in snapshot.machines.items():
                # 停機／維修中的機台不做異常判斷：它的訊號歸零是因為沒在跑，
                # 不是因為壞掉。少了這道判斷，一台正在被修的機器會持續觸發告警。
                if not machine.online:
                    continue
                specs = topology.machines[mid].signals
                triggers: list[str] = []

                critical = [n for n, r in machine.readings.items() if r.band is SignalBand.CRITICAL]
                warning = [n for n, r in machine.readings.items() if r.band is SignalBand.WARNING]
                for name in critical:
                    triggers.append(f"threshold:{name}=CRITICAL")

                health = self.health_estimate(machine, specs)
                self.health_history.setdefault(mid, []).append(health)
                if self.mode == "full":
                    trend = self.worst_trend(machine, specs)
                    for name in warning:
                        triggers.append(f"threshold:{name}=WARNING")
                    # 趨勢單獨不足以構成事件：健康的機台也會因量測雜訊出現短暫斜率。
                    # 要求健康度已經開始下滑才採信，這是 False Positive KPI 的主要防線。
                    if trend >= TREND_MIN_SLOPE and health < HEALTH_TREND_GATE:
                        triggers.append(f"trend:+{trend:.3f}/tick")
                    if health < HEALTH_ALARM:
                        triggers.append(f"health:{health:.0f}<{HEALTH_ALARM:.0f}")
                    elif health < HEALTH_WARN:
                        triggers.append(f"health:{health:.0f}<{HEALTH_WARN:.0f}")
                    # 預測式告警：目前還沒破門檻，但外推顯示未來會破。
                    # 沿用趨勢告警的健康度前提，避免把雜訊外推成假警報。
                    if health < HEALTH_TREND_GATE:
                        forecast = self.forecast_health(mid)
                        if forecast and forecast["predicted_min"] < FORECAST_ALARM:
                            # service 的 runtime 名稱是給工作台看的（Ridge 在那裡確實是
                            # 降級選項）；閉環是刻意選用，標成 fallback 會誤導稽核。
                            runtime = forecast["runtime"].replace("ridge-fallback", "ridge")
                            triggers.append(
                                f"forecast:health→{forecast['predicted_min']:.0f}"
                                f"@T+{forecast['horizon']}[{runtime}]"
                            )

                if not triggers:
                    continue

                severity = self._severity(critical, warning, health)
                # 同一台機器只在嚴重度升級時再次觸發，避免洗版
                key = f"{mid}:{severity.value}"
                if key in self.fired:
                    continue
                self.fired.add(key)

                self.event_seq += 1
                events.append(
                    AnomalyEvent(
                        event_id=f"EVT-{self.event_seq:03d}",
                        machine_id=mid,
                        tick=snapshot.tick,
                        sim_minutes=snapshot.sim_minutes,
                        severity=severity,
                        triggers=triggers,
                        health=health,
                        readings=dict(machine.readings),
                        detector=f"{self.name}[{self.mode}]",
                    )
                )

        self.events.extend(events)
        for event in events:
            self.log(
                "detect",
                event_id=event.event_id,
                machine_id=event.machine_id,
                tick=event.tick,
                severity=event.severity.value,
                triggers=event.triggers,
                health_estimate=round(event.health, 1),
                mode=self.mode,
            )
        return events

    @staticmethod
    def _severity(critical: list[str], warning: list[str], health: float) -> Severity:
        if critical or health < HEALTH_ALARM:
            return Severity.CRITICAL
        if warning or health < HEALTH_WARN:
            return Severity.WARNING
        return Severity.INFO

    def snapshot_summary(self, snapshot: FactorySnapshot, topology) -> dict[str, dict[str, float]]:
        """給 Dashboard 用的每台機器監測摘要。"""
        self.observe(snapshot)
        out: dict[str, dict[str, float]] = {}
        for mid, machine in snapshot.machines.items():
            specs = topology.machines[mid].signals
            if not machine.online:
                # 離線機台沒有可評估的訊號，回報孿生體記錄的健康度並標記為離線。
                out[mid] = {"health_estimate": round(machine.health, 1), "failure_risk": 0.0,
                            "worst_trend": 0.0, "online": 0.0}
                continue
            out[mid] = {
                "health_estimate": round(self.health_estimate(machine, specs), 1),
                "failure_risk": round(self.failure_risk(machine, specs), 3),
                "worst_trend": round(self.worst_trend(machine, specs), 4),
                "online": 1.0,
            }
        return out


__all__ = ["MonitoringAgent", "SignalWindow", "WINDOW"]
