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
from ..prediction import ForecastService
from ..prediction.threshold import DEFAULT_HORIZON_MIN, ThresholdEstimate, estimate_time_to_threshold
from ..twin.topology import HEALTH_FULL_SCALE, HEALTH_WEIGHTS
from .base import Agent

WINDOW = 6                    # 滑動視窗長度（tick）
TREND_MIN_SLOPE = 0.06        # 正規化偏離量 / tick，超過視為明顯上升趨勢
HEALTH_WARN = 88.0
HEALTH_ALARM = 75.0
HEALTH_TREND_GATE = 97.0      # 趨勢告警的健康度前提

# --- 門檻確認（N-of-M）：False Positive 的第一道防線 -----------------------------------
# 一個訊號要越界，必須在最近 ``CONFIRM_SAMPLES`` 個取樣裡至少出現 ``CONFIRM_HITS`` 次。
#
# 為什麼要有這條：接點抖動、電磁干擾、啟動突波都會讓**單一取樣**跳過危險門檻，
# 而機台完全沒事。固定門檻打在原始讀值上（Baseline A 的定義）就會當場報警；
# 2-of-3 確認則要求它至少撐過一個取樣週期。這是 DCS/SCADA 常見的 N-of-M 去彈跳，
# 不是我們發明的東西。
#
# 為什麼不用平滑值判帶：加權平均會被一個尖峰拉高好幾個 tick（尖峰退出視窗才會消失），
# 反而讓一次干擾變成一串告警；而且平滑值本身有遲滯，真實故障會被延後偵測。
# 代價寫清楚：真實故障的偵測會晚 **至多 1 個 tick**（要等第二個越界取樣）。
CONFIRM_SAMPLES = 3
CONFIRM_HITS = 2
# 剛從停機／維修回到線上的機台，訊號正在從零爬回來（轉速還沒到、電流還沒穩）。
# 那不是異常，那是啟動。3 個 tick 足夠讓 rpm（alpha 0.85）與 current（0.80）回到工作點。
# 這是真實告警系統的 start-up inhibit，不是為了讓數字好看：
# 沒有它，任何「維修完成」都會立刻被自己的復機暫態再告警一次。
RESTART_GRACE_TICKS = 3

# --- 進行式判定：動設備之前的自我節制 -------------------------------------------------
# 一個完整視窗內健康度估計至少要掉這麼多分，才算「還在惡化」。
# 3.0 的來由：健康度估計本身受量測雜訊影響，典型波動約 ±1 分；取三倍當門檻。
HEALTH_DROP_MIN = 3.0

# TTT（time-to-threshold）用的觀測歷史長度。這份歷史和 /api/state.history 是同一種東西：
# 只有 snapshot 看得到的讀值，沒有 fault、fault_progress 或真實健康度。
HISTORY_LEN = 120
# failure_risk 裡 TTT 最多能加多少。TTT 只是「還有多久」，不該蓋過「現在有多壞」，
# 所以它是加權項而不是主導項；踩線在眼前（0 分鐘）才吃滿。
TTT_RISK_WEIGHT = 0.35
TTT_RISK_HORIZON_MIN = DEFAULT_HORIZON_MIN


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

    def __init__(self, ctx=None, mode: str = "full", forecast: ForecastService | None = None) -> None:
        super().__init__(ctx)
        self.mode = mode
        self.windows: dict[str, dict[str, SignalWindow]] = {}
        self.event_seq = 0
        self.fired: set[str] = set()
        # 每台機台最後一次「離線」是哪個 tick（見 in_restart_grace）。
        self.offline_since: dict[str, int] = {}
        self.events: list[AnomalyEvent] = []   # 全部觸發過的事件，供 False Positive KPI 統計
        # TTT 用的可觀測歷史（Agent 自己累積的，不是跟孿生體要的）。
        self.history: list[dict[str, float]] = []
        # TTT 的單位是分鐘，所以要知道一個 tick 有多長。孿生體用的是同一個設定值
        # （twin.tick_minutes = settings.tick_seconds / 60），兩邊必須一致，
        # 否則「還有 4 分鐘」會變成一個和時間軸對不起來的數字。
        self.tick_minutes = max(1e-6, self.ctx.settings.tick_seconds / 60.0)
        # 時序模型：優先用它算 TTT，拿不到結果才退回線性外推。
        # ``threshold_only`` 模式（Baseline A）刻意不給模型 —— 它就是「只有固定門檻」的對照組。
        self.forecast_service = forecast if forecast is not None else (
            ForecastService() if mode == "full" else None
        )
        self._ttt_cache: dict[tuple[str, int], ThresholdEstimate] = {}
        # 每台機器最近一次算出來的 TTT（tick, estimate）。
        # Safety Agent 會讀這份當**證據**，所以要留著；帶 tick 是為了讓讀的人知道它有多新。
        self.last_ttt: dict[str, tuple[int, ThresholdEstimate]] = {}

    # ------------------------------------------------------------------ 觀測
    def observe(self, snapshot: FactorySnapshot) -> None:
        for mid, machine in snapshot.machines.items():
            per_machine = self.windows.setdefault(mid, {})
            for name, reading in machine.readings.items():
                per_machine.setdefault(name, SignalWindow()).push(reading.value)
            if not machine.online:
                self.offline_since[mid] = snapshot.tick
        self._record_history(snapshot)

    def in_restart_grace(self, machine_id: str, tick: int) -> bool:
        """這台機台是不是剛復機、還在啟動暫態裡？"""
        last_offline = self.offline_since.get(machine_id)
        return last_offline is not None and tick - last_offline <= RESTART_GRACE_TICKS

    def _record_history(self, snapshot: FactorySnapshot) -> None:
        """把這個 tick 的可觀測讀值留下來給 TTT 用。

        ``detect()`` 和 ``snapshot_summary()`` 都會呼叫 ``observe()``，同一個 tick 會進來兩次，
        所以這裡以 tick 去重 —— 否則時間軸會被壓縮，外推出來的分鐘數會直接少一半。
        """
        if self.history and self.history[-1]["tick"] == snapshot.tick:
            return
        point: dict[str, float] = {
            "tick": snapshot.tick,
            "production_pct": round(snapshot.production_pct, 2),
            "factory_health": round(snapshot.factory_health, 2),
        }
        for mid, machine in snapshot.machines.items():
            point[f"{mid}.health"] = round(machine.health, 2)
            for name, reading in machine.readings.items():
                point[f"{mid}.{name}"] = round(reading.value, 3)
        self.history.append(point)
        if len(self.history) > HISTORY_LEN:
            del self.history[: len(self.history) - HISTORY_LEN]

    # ------------------------------------------------------------------ 到達門檻的剩餘時間
    def time_to_threshold(self, machine: MachineSnapshot, specs) -> ThresholdEstimate:
        """預估這台機器還有幾分鐘會踩到 CRITICAL 門檻。

        為什麼需要它：瞬時斜率（0.08/tick）回答不了現場唯一在意的問題 ——「還有多久」。
        TTT 把同一份可觀測歷史換算成分鐘，於是「要不要現在停機」才有得談。

        只吃 Agent 可見的歷史，不讀孿生體的故障進度；用了哪個 runtime 一律標示在結果裡。
        """
        key = (machine.machine_id, self.history[-1]["tick"] if self.history else -1)
        cached = self._ttt_cache.get(key)
        if cached is not None:
            return cached
        if not machine.online:
            # 停機／維修中的機台訊號歸零是因為沒在跑，外推它等於憑空製造一個安心的數字。
            estimate = ThresholdEstimate(minutes=None, runtime="offline", per_signal={})
        else:
            estimate = estimate_time_to_threshold(
                self.history, machine.machine_id, specs,
                tick_minutes=self.tick_minutes,
                service=self.forecast_service,
            )
        self._ttt_cache = {key: estimate}    # 只留當下這個 tick，避免無限長大
        self.last_ttt[machine.machine_id] = (key[1], estimate)
        return estimate

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
        """Failure Risk：綜合目前健康度、惡化速度與**到達門檻的剩餘時間**，估 0~1 的短期故障風險。

        加進 TTT 的理由：健康度與斜率都只描述「現在」，兩台同樣掉到 85 分的機器，
        一台 12 分鐘後踩線、一台 50 分鐘後才踩線，短期風險顯然不一樣。
        TTT 是加權項不是主導項（最多 +0.35），因為它是外推值，不該壓過已經量到的事實。
        """
        health = self.health_estimate(machine, specs)
        trend = self.worst_trend(machine, specs)
        base = max(0.0, (100.0 - health) / 100.0)
        risk = base + max(0.0, trend) * 1.6
        if self.mode == "full":
            ttt = self.time_to_threshold(machine, specs).minutes
            if ttt is not None:
                risk += TTT_RISK_WEIGHT * max(0.0, 1.0 - ttt / TTT_RISK_HORIZON_MIN)
        return max(0.0, min(1.0, risk))

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

    def acknowledge(self, machine_id: str) -> None:
        """把一台機台的告警狀態重置（值班台上的 acknowledge）。

        `fired` 的用途是避免同一個異常洗版：同一台機器在同一個嚴重度上只觸發一次。
        代價是處置完成之後它再也不會告警 —— 如果那次處置其實沒修好，復發就永遠看不見。
        所以處置告一段落時要 acknowledge，讓下一次真的異常能重新叫人。
        """
        self.fired = {key for key in self.fired if not key.startswith(f"{machine_id}:")}

    # ------------------------------------------------------------------ 門檻確認
    def confirmed_bands(self, machine_id: str, machine: MachineSnapshot, specs) -> tuple[list[str], list[str]]:
        """回傳「確認過」的越界訊號清單（CRITICAL, WARNING）。

        確認的定義見 CONFIRM_SAMPLES / CONFIRM_HITS：最近 3 個取樣裡至少 2 個越界。
        視窗還不滿 3 筆時退回瞬時判斷 —— 剛開機的前兩分鐘沒有足夠證據可以要求，
        這時候寧可誤報也不要漏報。
        """
        windows = self.windows.get(machine_id, {})
        critical: list[str] = []
        warning: list[str] = []
        for spec in specs:
            window = windows.get(spec.name)
            reading = machine.readings.get(spec.name)
            if reading is None:
                continue
            if window is None or len(window.values) < CONFIRM_SAMPLES:
                if reading.band is SignalBand.CRITICAL:
                    critical.append(spec.name)
                elif reading.band is SignalBand.WARNING:
                    warning.append(spec.name)
                continue
            recent = list(window.values)[-CONFIRM_SAMPLES:]
            bands = [spec.band(v) for v in recent]
            if sum(1 for b in bands if b is SignalBand.CRITICAL) >= CONFIRM_HITS:
                critical.append(spec.name)
            elif sum(1 for b in bands if b is not SignalBand.NORMAL) >= CONFIRM_HITS:
                warning.append(spec.name)
        return critical, warning

    # ------------------------------------------------------------------ 進行式判定
    def health_series(self, machine_id: str, specs) -> list[float]:
        """把視窗裡的每一筆讀值各算一次健康度，得到一小段健康度歷史。

        刻意不另外存一份健康度歷史：同一個視窗算出來的東西，多存一份就多一個
        會和它不同步的地方。這裡只是換一個角度讀既有的視窗。
        """
        windows = self.windows.get(machine_id, {})
        usable = [(spec, windows[spec.name]) for spec in specs if spec.name in windows and windows[spec.name].values]
        if not usable:
            return []
        length = min(len(w.values) for _, w in usable)
        series: list[float] = []
        for idx in range(length):
            severity = 0.0
            for spec, window in usable:
                value = list(window.values)[-length:][idx]
                severity += HEALTH_WEIGHTS.get(spec.name, 0.0) * min(2.0, spec.deviation(value))
            series.append(max(0.0, min(100.0, 100.0 * (1.0 - severity / HEALTH_FULL_SCALE))))
        return series

    def degradation(self, machine: MachineSnapshot, specs) -> dict[str, float | bool]:
        """這個異常「還在惡化」，還是「已經停在一個新的穩態」？

        為什麼要問這個問題：設備劣化是**進行式**的。換規格提高負載、冷機暖機過衝、
        換料後重啟 —— 這些都會讓訊號跳到一個新的位置然後停在那裡。
        訊號的**位置**分不出兩者（新穩態的電流和早期過載的電流可以一模一樣），
        分得出來的是**它還動不動**。

        而要回答「還動不動」，數學上就必須看滿一個完整視窗：只要視窗裡還留著那個階躍的邊緣，
        「一次跳到新穩態」和「才剛開始的斜坡」就是同一條線。這也是 Orchestrator
        在動設備之前要多觀察 WINDOW 個 tick 的原因 —— 那幾分鐘不是保守，是資訊還不存在。
        """
        series = self.health_series(machine.machine_id, specs)
        trend = self.worst_trend(machine, specs)
        if len(series) < 4:
            # 樣本不足以判斷 —— 回報「還在惡化」，因為此時漏報的代價高於誤動作。
            return {"progressive": True, "health_drop": 0.0, "trend": trend, "samples": len(series)}
        half = len(series) // 2
        drop = (sum(series[:half]) / half) - (sum(series[half:]) / (len(series) - half))
        progressive = drop >= HEALTH_DROP_MIN or trend >= TREND_MIN_SLOPE
        return {
            "progressive": bool(progressive),
            "health_drop": round(drop, 2),
            "trend": round(trend, 4),
            "samples": len(series),
        }

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
                # 剛復機的機台在啟動暫態裡：轉速與電流還在從零爬回來，
                # 這時候的越界是啟動，不是故障。
                if self.in_restart_grace(mid, snapshot.tick):
                    continue
                specs = topology.machines[mid].signals
                triggers: list[str] = []

                # Baseline A（threshold_only）看的是**原始瞬時讀值**——那正是「固定門檻告警」
                # 這個對照組的定義。full 模式則要求越界在最近幾個取樣裡重複出現（N-of-M）。
                if self.mode == "full":
                    critical, warning = self.confirmed_bands(mid, machine, specs)
                else:
                    critical = [n for n, r in machine.readings.items() if r.band is SignalBand.CRITICAL]
                    warning = [n for n, r in machine.readings.items() if r.band is SignalBand.WARNING]
                for name in critical:
                    triggers.append(f"threshold:{name}=CRITICAL")

                health = self.health_estimate(machine, specs)
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

                if not triggers:
                    continue

                severity = self._severity(critical, warning, health)
                # 同一台機器只在嚴重度升級時再次觸發，避免洗版
                key = f"{mid}:{severity.value}"
                if key in self.fired:
                    continue
                self.fired.add(key)

                # TTT 不是觸發條件（它是外推值，不該自己製造事件），
                # 但事件一旦成立，「還有多久踩線」就是下游最需要的那個數字。
                ttt = self.time_to_threshold(machine, specs) if self.mode == "full" else None
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
                        time_to_threshold_min=ttt.minutes if ttt else None,
                        time_to_threshold_detail=ttt.to_dict() if ttt else {},
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
                time_to_threshold_min=(
                    round(event.time_to_threshold_min, 1) if event.time_to_threshold_min is not None else None
                ),
                time_to_threshold=event.time_to_threshold_detail or None,
            )
        return events

    @staticmethod
    def _severity(critical: list[str], warning: list[str], health: float) -> Severity:
        if critical or health < HEALTH_ALARM:
            return Severity.CRITICAL
        if warning or health < HEALTH_WARN:
            return Severity.WARNING
        return Severity.INFO

    def snapshot_summary(self, snapshot: FactorySnapshot, topology) -> dict[str, dict[str, object]]:
        """給 Dashboard 用的每台機器監測摘要。"""
        self.observe(snapshot)
        out: dict[str, dict[str, object]] = {}
        for mid, machine in snapshot.machines.items():
            specs = topology.machines[mid].signals
            if not machine.online:
                # 離線機台沒有可評估的訊號，回報孿生體記錄的健康度並標記為離線。
                out[mid] = {"health_estimate": round(machine.health, 1), "failure_risk": 0.0,
                            "worst_trend": 0.0, "online": 0.0,
                            "time_to_threshold_min": None, "ttt_signal": "", "ttt_runtime": "offline"}
                continue
            ttt = self.time_to_threshold(machine, specs)
            out[mid] = {
                "health_estimate": round(self.health_estimate(machine, specs), 1),
                "failure_risk": round(self.failure_risk(machine, specs), 3),
                "worst_trend": round(self.worst_trend(machine, specs), 4),
                "online": 1.0,
                # 「還有多久踩線」比「斜率多少」更接近現場要的答案，所以它上得了遙測卡。
                "time_to_threshold_min": round(ttt.minutes, 1) if ttt.minutes is not None else None,
                "ttt_signal": ttt.signal,
                "ttt_runtime": ttt.runtime,
                "ttt_horizon_min": ttt.horizon_min,
            }
        return out


__all__ = [
    "MonitoringAgent", "SignalWindow", "WINDOW", "HISTORY_LEN", "TTT_RISK_WEIGHT",
    "CONFIRM_HITS", "CONFIRM_SAMPLES", "HEALTH_DROP_MIN",
]
