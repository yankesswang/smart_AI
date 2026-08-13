"""Factory Digital Twin 模擬引擎。

這是整個系統唯一「知道真相」的地方：

* 感測器數值由故障模型 + 一階遲滯 + 雜訊實際算出來，不是預錄的。
* 動作（停機、降速、轉單、維修）會真的改變狀態，之後的 tick 會反映出來。
* Ground Truth（哪一台機器得了什麼病）只存在 ``_MachineRuntime.fault``，
  ``snapshot()`` 不會輸出它，Agent 也拿不到。

``fork()`` 讓 Production Agent 可以對每個候選方案做乾跑（dry-run），
所以方案的產能 / 交期 / 復原時間是模擬出來的，不是 LLM 猜的。
"""

from __future__ import annotations

import copy
import hashlib
import math
import struct
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..domain import (
    Action,
    ActionKind,
    CameraObservation,
    FactorySnapshot,
    FaultInjection,
    Machine,
    MachineKind,
    MachineSnapshot,
    MachineState,
    Order,
    SensorReading,
    SignalBand,
    SignalSpec,
)
from .faults import FAULTS, HAZARD_EVENT_ID, FaultModel
from .topology import (
    FactoryTopology,
    HEALTH_FULL_SCALE,
    HEALTH_WEIGHTS,
    build_factory,
)

# 各訊號的一階遲滯係數：溫度慢、電流與轉速快。
SIGNAL_ALPHA: dict[str, float] = {
    "temperature": 0.30,
    "vibration": 0.75,
    "current": 0.80,
    "rpm_pct": 0.85,
}
# 各訊號的量測雜訊標準差。
SIGNAL_NOISE: dict[str, float] = {
    "temperature": 0.55,
    "vibration": 0.09,
    "current": 0.14,
    "rpm_pct": 0.35,
}
# 降速運轉時：故障造成的偏移與轉速的縮放。
DERATE_LOAD_FACTOR = 0.60
DERATE_RPM_PCT = 63.0
DERATE_FAULT_RELIEF = 0.55

THROUGHPUT_EMA_ALPHA = 0.45

# 產線達成率回到這個水準以上，視為已從事故中復原。
# 80% 的理由：主力機台 M-A 停機時，替代機台 M-B 的滿載產出約為名目產能的 85%，
# 門檻必須低於那個物理上限，否則「轉單」在定義上就永遠不可能算復原。
RECOVERY_THRESHOLD_PCT = 80.0


def _stable_unit(*parts: Any) -> float:
    """由字串／數字組合出可重現的 [0, 1) 亂數（跨程序穩定，不用 hash()）。"""
    payload = "|".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    (value,) = struct.unpack("<Q", digest)
    return value / float(1 << 64)


def _stable_gauss(*parts: Any) -> float:
    """Box–Muller，從穩定亂數產生標準常態值。"""
    u1 = max(_stable_unit("g1", *parts), 1e-12)
    u2 = _stable_unit("g2", *parts)
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


@dataclass
class _MachineRuntime:
    """機台的內部狀態。``fault`` / ``fault_progress`` 是 Ground Truth，不對外輸出。"""

    machine_id: str
    state: MachineState
    assigned_order: str | None = None
    load_pct: float = 1.0
    changeover_remaining_min: float = 0.0
    maintenance_remaining_min: float = 0.0
    # --- Ground Truth（僅 Simulator 內部）---
    fault: str | None = None
    fault_progress: float = 0.0
    fault_start_tick: int = 0
    fault_ramp_ticks: int = 10
    fault_max_progress: float = 1.0
    # --- 訊號 ---
    # clean_signals：機台的真實物理狀態（一階遲滯後、未加量測雜訊）。
    #   生產速率與真實健康度由它決定 —— 量測雜訊不該讓機台真的少做幾件。
    # signals：感測器實際回報的值（clean + 量測雜訊），這才是 Agent 看得到的東西。
    clean_signals: dict[str, float] = field(default_factory=dict)
    signals: dict[str, float] = field(default_factory=dict)
    health: float = 100.0
    last_rate_uph: float = 0.0
    repairs_done: int = 0
    secondary_damage: bool = False


@dataclass
class ActionEffect:
    ok: bool
    kind: str
    target: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "kind": self.kind, "target": self.target, "message": self.message, "details": self.details}


class FactoryTwin:
    """可控制、可注入故障、可執行動作、可量化驗證的工廠數位孿生。"""

    def __init__(
        self,
        topology: FactoryTopology | None = None,
        seed: int = 20260809,
        tick_minutes: float = 1.0,
        ambient_temp_c: float = 28.0,
    ) -> None:
        self.topo = topology or build_factory()
        self.seed = seed
        self.tick_minutes = tick_minutes
        self.ambient_temp_c = ambient_temp_c
        self.tick = 0
        self.sim_minutes = 0.0
        self.runtime: dict[str, _MachineRuntime] = {}
        self.orders: dict[str, Order] = {}
        self.queue: dict[str, float] = {}          # M-C 前的在製品：order_id -> units
        self.completed_units_total: float = 0.0
        self.throughput_uph: float = 0.0
        self.throughput_ema_uph: float = 0.0
        self.nominal_output_uph: float = 0.0
        self.pending_injections: list[FaultInjection] = []
        self.hazard_from_tick: int | None = None
        self.hazard_machine_id: str | None = None
        # 人員實際暴露在「運轉中危險區」的分鐘數 —— 工安 KPI 的核心指標，
        # 它量的是實際風險曝露，而不是「系統有沒有發出告警」。
        self.hazard_exposure_min: float = 0.0
        self.event_log: list[dict[str, Any]] = []
        self.label = "live"
        self.reset()

    # ------------------------------------------------------------------ 生命週期
    def reset(self) -> None:
        self.tick = 0
        self.sim_minutes = 0.0
        self.orders = {o.order_id: copy.deepcopy(o) for o in self.topo.initial_orders}
        self.queue = {}
        self.completed_units_total = 0.0
        self.pending_injections = []
        self.hazard_from_tick = None
        self.hazard_machine_id = None
        self.hazard_exposure_min = 0.0
        self.event_log = []
        self.runtime = {}
        for mid, machine in self.topo.machines.items():
            assigned = next(
                (o.order_id for o in self.orders.values() if o.assigned_machine == mid),
                None,
            )
            rt = _MachineRuntime(
                machine_id=mid,
                state=MachineState.RUNNING,
                assigned_order=assigned,
                load_pct=self._initial_load_pct(mid, assigned),
            )
            rt.clean_signals = {spec.name: spec.nominal for spec in machine.signals}
            rt.signals = dict(rt.clean_signals)
            rt.health = 100.0
            self.runtime[mid] = rt
        self.nominal_output_uph = self._nominal_output()
        self.throughput_uph = self.nominal_output_uph
        self.throughput_ema_uph = self.nominal_output_uph
        self._refresh_signals()

    def _initial_load_pct(self, machine_id: str, assigned: str | None) -> float:
        machine = self.topo.machines[machine_id]
        if machine.kind is MachineKind.PACKAGING:
            return 1.0
        if assigned is None:
            return 0.0
        # Machine B 是替代機台：平時只承接部分負載，保留備援產能。
        return 1.0 if machine_id == "M-A" else 0.32

    def _nominal_output(self) -> float:
        """名目產出（uph）：上游承諾產能與包裝瓶頸取小。"""
        upstream = sum(
            self.topo.machines[mid].rated_rate_uph * rt.load_pct
            for mid, rt in self.runtime.items()
            if self.topo.machines[mid].kind is MachineKind.MACHINING
        )
        packaging = sum(
            self.topo.machines[mid].rated_rate_uph
            for mid in self.runtime
            if self.topo.machines[mid].kind is MachineKind.PACKAGING
        )
        return min(upstream, packaging)

    def fork(self, label: str = "dryrun") -> "FactoryTwin":
        """複製一份完全獨立的孿生體，用於方案乾跑。拓撲是唯讀的所以共用。"""
        clone = copy.copy(self)
        clone.runtime = copy.deepcopy(self.runtime)
        clone.orders = copy.deepcopy(self.orders)
        clone.queue = dict(self.queue)
        clone.pending_injections = list(self.pending_injections)
        clone.event_log = []
        clone.label = label
        return clone

    def fork_as_belief(
        self,
        machine_id: str,
        believed_fault_id: str | None,
        observed_health: float,
        label: str = "belief",
    ) -> "FactoryTwin":
        """依「Agent 相信的診斷」建立規劃用模型，而不是依真實故障。

        這是本專案最重要的一條分界線。方案投影如果直接跑在帶有 Ground Truth 的孿生體上，
        等於讓 Production Agent 偷看答案；診斷正確與否就不會影響結果，Demo 也就沒有意義。

        所以規劃模型是這樣建的：拿掉真實故障標籤，換上 Diagnosis Agent 推論出來的故障，
        並用「觀測到的健康度」反推它的嚴重程度。診斷錯了，投影就會錯 ——
        然後由 Verification Agent 在真實孿生體上抓出來並觸發重試。
        """
        clone = self.fork(label=label)
        rt = clone.runtime[machine_id]
        rt.fault = believed_fault_id
        if believed_fault_id is None:
            rt.fault_progress = 0.0
            return clone
        rt.fault_progress = self.estimate_progress_for(machine_id, believed_fault_id, observed_health)
        rt.fault_start_tick = clone.tick - int(rt.fault_progress * rt.fault_ramp_ticks)
        # 讓訊號直接跳到該信念下的穩態，避免規劃模型一開始就有假的暫態。
        machine = clone.topo.machines[machine_id]
        model = FAULTS[believed_fault_id]
        for spec in machine.signals:
            rt.clean_signals[spec.name] = max(
                0.0, spec.nominal + model.deltas.get(spec.name, 0.0) * rt.fault_progress
            )
        clone._refresh_signals()
        return clone

    def estimate_progress_for(self, machine_id: str, fault_id: str, target_health: float) -> float:
        """反推：這個故障要走到多嚴重，才會讓健康度掉到觀測值？（二分搜尋）"""
        machine = self.topo.machines[machine_id]
        model = FAULTS[fault_id]

        def health_at(progress: float) -> float:
            severity = 0.0
            for spec in machine.signals:
                value = spec.nominal + model.deltas.get(spec.name, 0.0) * progress
                severity += HEALTH_WEIGHTS.get(spec.name, 0.0) * min(2.0, spec.deviation(value))
            return max(0.0, min(100.0, 100.0 * (1.0 - severity / HEALTH_FULL_SCALE)))

        lo, hi = 0.0, 2.5
        if health_at(hi) > target_health:
            return hi
        for _ in range(40):
            mid = (lo + hi) / 2
            if health_at(mid) > target_health:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    def project(
        self,
        actions: list[Action],
        ticks: int,
        target_machine: str | None = None,
        recovery_threshold_pct: float = RECOVERY_THRESHOLD_PCT,
    ) -> dict[str, float]:
        """在這個（規劃用）孿生體上乾跑一組動作，只回傳**快照看得到**的指標。

        刻意不回傳 fault / fault_progress —— 規劃階段拿不到 Ground Truth。

        除了產能與交期，也回報整段期間的**訊號峰值**與**最低健康度**。
        Safety Agent 需要這些：一個方案安不安全，看的不是「現在的數值」，
        而是「這個方案會把設備帶到哪裡」。
        """
        if target_machine is None:
            target_machine = next((a.target for a in actions if a.target in self.runtime), None)
        for action in actions:
            self.apply(action)

        before = self.snapshot()
        produced_before = self.completed_units_total
        production_samples: list[float] = []
        worst_delay = 0.0
        peak_vibration = 0.0
        peak_temperature = 0.0
        min_health = 100.0
        hazard_while_running = 0.0
        last_below_threshold = 0

        for step_idx in range(1, max(1, ticks) + 1):
            snap = self.step()
            production_samples.append(snap.production_pct)
            if snap.production_pct < recovery_threshold_pct:
                last_below_threshold = step_idx
            worst_delay = max(worst_delay, self.kpi()["max_order_delay_min"])
            if target_machine:
                machine = snap.machines[target_machine]
                peak_vibration = max(peak_vibration, machine.value("vibration") or 0.0)
                peak_temperature = max(peak_temperature, machine.value("temperature") or 0.0)
                min_health = min(min_health, machine.health)
                running = machine.state in (MachineState.RUNNING, MachineState.DERATED)
                if running and any(c.person_in_hazard_zone or c.smoke_detected for c in snap.cameras):
                    hazard_while_running = 1.0

        after = self.snapshot()
        nominal_units = self.nominal_output_uph * (ticks * self.tick_minutes) / 60.0
        produced = self.completed_units_total - produced_before
        health_after = after.machines[target_machine].health if target_machine else after.factory_health

        # 復原時間 = 產線達成率「最後一次低於門檻」之後的時間點。
        # 用「最後一次」而不是「第一次高於門檻」：EMA 有遲滯，剛執行時達成率可能還沒掉下來，
        # 那不算已經復原。整段都沒掉下去 → 0；到最後都還沒回來 → 視為未復原（= 整個 horizon）。
        recovery_min = last_below_threshold * self.tick_minutes
        recovered = 1.0 if last_below_threshold < ticks else 0.0

        return {
            "production_pct": sum(production_samples) / len(production_samples) if production_samples else 0.0,
            "production_pct_final": after.production_pct,
            "production_loss_units": max(0.0, nominal_units - produced),
            "max_order_delay_min": worst_delay,
            "final_order_delay_min": self.kpi()["max_order_delay_min"],
            "recovery_min": recovery_min,
            "recovered": recovered,
            "machine_health_after": health_after,
            "min_health": min_health,
            "peak_vibration": peak_vibration,
            "peak_temperature": peak_temperature,
            "hazard_while_running": hazard_while_running,
            "factory_health_after": after.factory_health,
            "health_before": before.factory_health,
        }

    # ------------------------------------------------------------------ 故障注入
    def inject(self, injection: FaultInjection) -> None:
        """注入故障：只設定 Simulator 內部狀態，不產生任何給 Agent 的標籤。"""
        if injection.fault_id == HAZARD_EVENT_ID:
            self.hazard_from_tick = injection.start_tick
            self.hazard_machine_id = injection.machine_id
            self.event_log.append({"tick": self.tick, "type": "inject", "fault": injection.fault_id, "machine": injection.machine_id})
            return
        if injection.fault_id not in FAULTS:
            raise KeyError(f"未知的故障模型：{injection.fault_id}")
        rt = self.runtime[injection.machine_id]
        rt.fault = injection.fault_id
        rt.fault_progress = 0.0
        rt.fault_start_tick = max(injection.start_tick, self.tick)
        rt.fault_ramp_ticks = max(1, injection.ramp_ticks)
        rt.fault_max_progress = injection.max_progress
        self.event_log.append({"tick": self.tick, "type": "inject", "fault": injection.fault_id, "machine": injection.machine_id})

    def schedule(self, injections: Iterable[FaultInjection]) -> None:
        for inj in injections:
            if inj.start_tick <= self.tick:
                self.inject(inj)
            else:
                self.pending_injections.append(inj)

    @property
    def ground_truth(self) -> dict[str, str]:
        """僅供評分／Benchmark 使用；任何 Agent 都不得呼叫這個屬性。"""
        gt = {mid: rt.fault for mid, rt in self.runtime.items() if rt.fault}
        if self.hazard_from_tick is not None and self.tick >= self.hazard_from_tick:
            gt["SAFETY"] = HAZARD_EVENT_ID
        return gt  # type: ignore[return-value]

    # ------------------------------------------------------------------ 模擬推進
    def step(self) -> FactorySnapshot:
        self.tick += 1
        self.sim_minutes += self.tick_minutes

        for inj in [i for i in self.pending_injections if i.start_tick <= self.tick]:
            self.pending_injections.remove(inj)
            self.inject(inj)

        self._advance_faults()
        self._advance_timers()
        self._resolve_hazard()
        self._refresh_signals()
        self._dispatch_orders()
        self._produce()
        self._age_orders()
        snapshot = self.snapshot()
        if any(c.person_in_hazard_zone for c in snapshot.cameras):
            self.hazard_exposure_min += self.tick_minutes
        return snapshot

    def run(self, ticks: int) -> FactorySnapshot:
        snap = self.snapshot()
        for _ in range(max(0, ticks)):
            snap = self.step()
        return snap

    def _advance_faults(self) -> None:
        for rt in self.runtime.values():
            if not rt.fault:
                continue
            model = FAULTS[rt.fault]
            if rt.state in (MachineState.MAINTENANCE, MachineState.STOPPED):
                continue  # 停機期間劣化不再前進
            elapsed = max(0, self.tick - rt.fault_start_tick)
            ramp = min(rt.fault_max_progress, elapsed / rt.fault_ramp_ticks)
            rt.fault_progress = max(rt.fault_progress, ramp)
            if rt.fault_progress >= rt.fault_max_progress - 1e-9:
                # 已經到頂還繼續跑 → 持續惡化，全速運轉惡化更快
                gain = model.full_speed_risk_gain if rt.state is MachineState.RUNNING else 0.35
                rt.fault_progress += model.escalation_per_tick * gain * self.tick_minutes
            if rt.fault_progress > 1.9 and not rt.secondary_damage:
                rt.secondary_damage = True
                rt.state = MachineState.STOPPED
                self.event_log.append(
                    {"tick": self.tick, "type": "secondary_damage", "machine": rt.machine_id, "fault": rt.fault}
                )

    def _advance_timers(self) -> None:
        for rt in self.runtime.values():
            if rt.changeover_remaining_min > 0:
                rt.changeover_remaining_min = max(0.0, rt.changeover_remaining_min - self.tick_minutes)
            if rt.state is MachineState.MAINTENANCE:
                rt.maintenance_remaining_min = max(0.0, rt.maintenance_remaining_min - self.tick_minutes)
                if rt.maintenance_remaining_min <= 0:
                    rt.fault = None
                    rt.fault_progress = 0.0
                    rt.secondary_damage = False
                    rt.repairs_done += 1
                    rt.state = MachineState.IDLE
                    self.event_log.append({"tick": self.tick, "type": "maintenance_done", "machine": rt.machine_id})

    def _resolve_hazard(self) -> None:
        """機台一停下來，現場就會被淨空 —— 工安事件到此結束。

        少了這一步，危險區闖入會永遠掛在那裡：機台維修完復機之後，
        同一個人又「重新」出現在危險區裡，曝露時間會一路累加到情境結束。
        """
        if self.hazard_from_tick is None or self.hazard_machine_id is None:
            return
        rt = self.runtime.get(self.hazard_machine_id)
        if rt is not None and rt.state in (MachineState.STOPPED, MachineState.MAINTENANCE):
            self.event_log.append(
                {"tick": self.tick, "type": "hazard_cleared", "machine": self.hazard_machine_id}
            )
            self.hazard_from_tick = None
            self.hazard_machine_id = None

    # ------------------------------------------------------------------ 感測器
    def _target_signal(self, rt: _MachineRuntime, spec: SignalSpec) -> float:
        offline = rt.state in (MachineState.STOPPED, MachineState.MAINTENANCE)
        if offline:
            if spec.name == "temperature":
                return self.ambient_temp_c + 6.0
            if spec.name == "rpm_pct":
                return 0.0
            if spec.name == "vibration":
                return 0.12
            if spec.name == "current":
                return 0.4
            return spec.nominal

        base = spec.nominal
        if rt.state is MachineState.DERATED:
            if spec.name == "rpm_pct":
                base = DERATE_RPM_PCT
            elif spec.name == "current":
                base = spec.nominal * 0.72
            elif spec.name == "temperature":
                base = spec.nominal - 4.0

        if rt.fault:
            model: FaultModel = FAULTS[rt.fault]
            delta = model.deltas.get(spec.name, 0.0)
            relief = DERATE_FAULT_RELIEF if rt.state is MachineState.DERATED else 1.0
            base += delta * rt.fault_progress * relief

        # 環境溫度耦合：室溫越高，機台溫度越高。
        if spec.name == "temperature":
            base += (self.ambient_temp_c - 28.0) * 0.6
        return base

    def _refresh_signals(self) -> None:
        for mid, rt in self.runtime.items():
            machine = self.topo.machines[mid]
            noise_gain = FAULTS[rt.fault].noise_gain if rt.fault else 1.0
            active = rt.state not in (MachineState.STOPPED, MachineState.MAINTENANCE)
            for spec in machine.signals:
                target = self._target_signal(rt, spec)
                alpha = SIGNAL_ALPHA.get(spec.name, 0.6)
                current = rt.clean_signals.get(spec.name, spec.nominal)
                clean = current + (target - current) * alpha
                rt.clean_signals[spec.name] = max(0.0, clean)
                sigma = SIGNAL_NOISE.get(spec.name, 0.1)
                noise = _stable_gauss(self.seed, mid, spec.name, self.tick) * sigma * (noise_gain if active else 0.2)
                rt.signals[spec.name] = max(0.0, clean + noise)
            rt.health = self._health_of(machine, rt)

    def _health_of(self, machine: Machine, rt: _MachineRuntime) -> float:
        """真實健康度：由未加雜訊的物理狀態算出。

        注意 Monitoring Agent **不會**用這個值 —— 它只能從帶雜訊的感測器讀值自己估。
        這裡的值是 Simulator 的真相，用於 KPI 驗證與 Dashboard。
        """
        if rt.state in (MachineState.STOPPED, MachineState.MAINTENANCE):
            # 停機時感測器歸零，沒有東西可以評估設備狀況。
            # 這時健康度**凍結在停機前最後一次的評估值**，直到維修完成後歸零重算。
            # （早期版本在這裡改用另一條公式，結果同一個物理狀態在停機前後會得到
            #   兩個不同的健康度，KPI 也就跟著跳動。）
            return 100.0 if rt.fault is None else rt.health
        severity = 0.0
        for spec in machine.signals:
            value = rt.clean_signals.get(spec.name, spec.nominal)
            severity += HEALTH_WEIGHTS.get(spec.name, 0.0) * min(2.0, spec.deviation(value))
        return max(0.0, min(100.0, 100.0 * (1.0 - severity / HEALTH_FULL_SCALE)))

    @staticmethod
    def _scale(machine: Machine, signal: str) -> float:
        spec = machine.signal(signal)
        return spec.scale if spec else 1.0

    # ------------------------------------------------------------------ 生產
    def effective_rate(self, machine_id: str) -> float:
        """機台目前的實際產出速率（件/小時）。"""
        machine = self.topo.machines[machine_id]
        rt = self.runtime[machine_id]
        if rt.state in (MachineState.STOPPED, MachineState.MAINTENANCE, MachineState.IDLE):
            return 0.0
        if rt.changeover_remaining_min > 0:
            return 0.0
        state_factor = DERATE_LOAD_FACTOR if rt.state is MachineState.DERATED else 1.0
        # 用未加雜訊的真實轉速：量測雜訊不該讓機台真的少做幾件。
        rpm = rt.clean_signals.get("rpm_pct", 100.0) / 100.0
        # 品質係數：健康度下降代表加工精度下降、重工與廢品增加。
        quality = max(0.05, min(1.0, rt.health / 100.0))
        return machine.rated_rate_uph * rt.load_pct * state_factor * max(0.0, rpm) * quality

    def _dispatch_orders(self) -> None:
        """簡易派工：機台完成訂單後自動接下一張它做得動的、最急的訂單。"""
        assigned_ids = {rt.assigned_order for rt in self.runtime.values() if rt.assigned_order}
        for mid, rt in self.runtime.items():
            machine = self.topo.machines[mid]
            if machine.kind is not MachineKind.MACHINING:
                continue
            if rt.state in (MachineState.STOPPED, MachineState.MAINTENANCE):
                continue
            current = self.orders.get(rt.assigned_order) if rt.assigned_order else None
            if current is not None and not current.done:
                # 維修完成後機台會回到 IDLE，但手上那張訂單還沒做完 —— 要讓它復工，
                # 否則機台會抱著未完成的訂單永遠停在 IDLE。
                if rt.state is MachineState.IDLE:
                    rt.state = MachineState.RUNNING
                    rt.load_pct = rt.load_pct or 1.0
                continue
            if current is not None and current.done:
                assigned_ids.discard(current.order_id)
                rt.assigned_order = None
            candidates = [
                o
                for o in self.orders.values()
                if not o.done and o.order_id not in assigned_ids and self.topo.can_produce(mid, o.product_id)
            ]
            if not candidates:
                rt.state = MachineState.IDLE if rt.state is MachineState.RUNNING else rt.state
                continue
            candidates.sort(key=lambda o: (o.priority, o.due_in_min, o.order_id))
            picked = candidates[0]
            previous_product = self.orders[current.order_id].product_id if current else None
            rt.assigned_order = picked.order_id
            picked.assigned_machine = mid
            # 接下新訂單就是全力生產這一張。load_pct 是「這次派工承諾的產能比例」，
            # 初始的部分負載（M-B 0.32）只描述期初那張低量訂單，不該一直跟著機台走 ——
            # 少了這行，被轉走訂單的機台會停在 load_pct=0，修好之後永遠算不出產出。
            rt.load_pct = 1.0
            assigned_ids.add(picked.order_id)
            if previous_product and previous_product != picked.product_id:
                rt.changeover_remaining_min = max(rt.changeover_remaining_min, machine.changeover_min)
            if rt.state is MachineState.IDLE:
                rt.state = MachineState.RUNNING

    def _produce(self) -> None:
        hours = self.tick_minutes / 60.0
        # 1) 上游加工 → 推進 M-C 之前的在製品佇列
        for mid, rt in self.runtime.items():
            machine = self.topo.machines[mid]
            if machine.kind is not MachineKind.MACHINING:
                continue
            rate = self.effective_rate(mid)
            rt.last_rate_uph = rate
            order = self.orders.get(rt.assigned_order) if rt.assigned_order else None
            if order is None or rate <= 0:
                continue
            outstanding = order.remaining - self.queue.get(order.order_id, 0.0)
            units = min(rate * hours, max(0.0, outstanding))
            if units > 0:
                self.queue[order.order_id] = self.queue.get(order.order_id, 0.0) + units

        # 2) 包裝機從佇列取料完成（訂單優先序決定取料順序）
        completed = 0.0
        for mid, rt in self.runtime.items():
            machine = self.topo.machines[mid]
            if machine.kind is not MachineKind.PACKAGING:
                continue
            rate = self.effective_rate(mid)
            rt.last_rate_uph = rate
            capacity = rate * hours
            queued_ids = sorted(
                [oid for oid, qty in self.queue.items() if qty > 0],
                key=lambda oid: (self.orders[oid].priority, self.orders[oid].due_in_min, oid),
            )
            for oid in queued_ids:
                if capacity <= 1e-9:
                    break
                take = min(capacity, self.queue[oid])
                self.queue[oid] -= take
                if self.queue[oid] <= 1e-9:
                    self.queue.pop(oid, None)
                capacity -= take
                self.orders[oid].produced = min(self.orders[oid].quantity, self.orders[oid].produced + take)  # type: ignore[assignment]
                completed += take

        self.completed_units_total += completed
        self.throughput_uph = completed / hours if hours > 0 else 0.0
        self.throughput_ema_uph = (
            THROUGHPUT_EMA_ALPHA * self.throughput_uph + (1 - THROUGHPUT_EMA_ALPHA) * self.throughput_ema_uph
        )

    def _age_orders(self) -> None:
        for order in self.orders.values():
            if not order.done:
                order.due_in_min -= self.tick_minutes

    # ------------------------------------------------------------------ 觀測
    def _cameras(self) -> list[CameraObservation]:
        hazard_active = self.hazard_from_tick is not None and self.tick >= self.hazard_from_tick
        rt_a = self.runtime.get("M-A")
        machine_running = rt_a is not None and rt_a.state in (MachineState.RUNNING, MachineState.DERATED)
        # 維修中會有技師合法進入區域（機台已停），這不是違規。
        maintenance = rt_a is not None and rt_a.state is MachineState.MAINTENANCE
        temp_a = rt_a.signals.get("temperature", 0.0) if rt_a else 0.0
        smoke = temp_a > 92.0

        if hazard_active:
            person_count = 1
            in_zone = True
            # 情境定義與 caption 都明確指定「護具不全」；這裡不能再隨機變成合規，
            # 否則 CameraObservation、Safety UI 與示範影片會互相矛盾。
            ppe = False
            caption = "偵測到 1 名人員進入 Machine A 運轉危險區，未偵測到完整護具。"
            confidence = 0.86 + 0.08 * _stable_unit(self.seed, "conf", self.tick)
        elif maintenance:
            person_count = 2
            in_zone = True
            ppe = True
            caption = "2 名維修技師於 Machine A 作業區，機台已停機，護具齊全。"
            confidence = 0.91
        else:
            person_count = 1 if _stable_unit(self.seed, "person", self.tick) > 0.55 else 0
            in_zone = False
            ppe = True
            caption = "作業區無人員進入危險範圍。" if person_count == 0 else "1 名人員於安全走道，未進入危險區。"
            confidence = 0.93

        return [
            CameraObservation(
                camera_id="CAM-01",
                zone_id="Z-A",
                machine_id="M-A",
                person_count=person_count,
                person_in_hazard_zone=in_zone and machine_running,
                ppe_compliant=ppe,
                fall_detected=False,
                smoke_detected=smoke,
                confidence=round(confidence, 3),
                caption=caption,
            )
        ]

    def machine_snapshot(self, machine_id: str) -> MachineSnapshot:
        machine = self.topo.machines[machine_id]
        rt = self.runtime[machine_id]
        online = rt.state not in (MachineState.STOPPED, MachineState.MAINTENANCE)
        readings = {}
        for spec in machine.signals:
            value = rt.signals.get(spec.name, spec.nominal)
            # 停機／維修中的機台，感測器讀值會掉到接近零。那是「沒在跑」，不是異常：
            # 用運轉門檻判讀會讓正在維修的機台一直回報 CURRENT 偏低、RPM CRITICAL，
            # 既污染 Dashboard，也會讓 Monitoring Agent 對修理中的機器不斷告警。
            band = spec.band(value) if online else SignalBand.NORMAL
            readings[spec.name] = SensorReading(spec.name, value, spec.unit, band)
        rate = rt.last_rate_uph if self.tick > 0 else self.effective_rate(machine_id)
        util = 100.0 * rate / machine.rated_rate_uph if machine.rated_rate_uph else 0.0
        queue = int(round(sum(self.queue.values()))) if machine.kind is MachineKind.PACKAGING else 0
        return MachineSnapshot(
            machine_id=machine_id,
            name=machine.name,
            state=rt.state,
            readings=readings,
            health=rt.health,
            production_rate_uph=rate,
            utilization_pct=util,
            queue=queue,
            maintenance_remaining_min=rt.maintenance_remaining_min,
            online=online,
        )

    def factory_health(self) -> float:
        total_weight = sum(m.rated_rate_uph for m in self.topo.machines.values())
        if not total_weight:
            return 100.0
        return sum(
            self.topo.machines[mid].rated_rate_uph * rt.health for mid, rt in self.runtime.items()
        ) / total_weight

    def production_pct(self) -> float:
        if self.nominal_output_uph <= 0:
            return 0.0
        return 100.0 * self.throughput_ema_uph / self.nominal_output_uph

    def snapshot(self) -> FactorySnapshot:
        """Agent 唯一可見的世界。這裡沒有 fault / fault_progress。"""
        return FactorySnapshot(
            tick=self.tick,
            sim_minutes=self.sim_minutes,
            machines={mid: self.machine_snapshot(mid) for mid in self.topo.machines},
            orders={oid: copy.deepcopy(o) for oid, o in self.orders.items()},
            cameras=self._cameras(),
            factory_health=self.factory_health(),
            production_pct=self.production_pct(),
            ambient_temp_c=self.ambient_temp_c,
            label=self.label,
        )

    def kpi(self) -> dict[str, float]:
        """驗證階段使用的即時 KPI。

        交期延遲只計算**已排程**的訂單（已派工到某台機器）。
        尚未派工的 backlog 不算「延遲」，它只是還沒排進去 —— 這是 MES 的標準語意，
        也避免十幾張遠期訂單的排隊時間淹掉真正有風險的那一張。
        """
        worst_delay = 0.0
        late = 0
        scheduled = 0
        backlog = 0
        for order in self.orders.values():
            if order.done:
                continue
            if order.assigned_machine is None:
                backlog += 1
                continue
            scheduled += 1
            delay = self.estimate_finish_min(order.order_id) - order.due_in_min
            if delay > 0:
                late += 1
            worst_delay = max(worst_delay, delay)
        return {
            "production_pct": self.production_pct(),
            "throughput_uph": self.throughput_uph,
            "factory_health": self.factory_health(),
            "max_order_delay_min": max(0.0, worst_delay),
            "late_orders": float(late),
            "scheduled_orders": float(scheduled),
            "backlog_orders": float(backlog),
            "completed_units": self.completed_units_total,
        }

    def estimate_finish_min(self, order_id: str) -> float:
        """以目前速率估算訂單完成還需幾分鐘（含換線與維修等待）。"""
        order = self.orders[order_id]
        if order.done:
            return 0.0
        machine_id = order.assigned_machine
        if machine_id is None or machine_id not in self.runtime:
            # 未派工：估計等到某台可做這個產品的機台空出來
            candidates = self.topo.machines_for_product(order.product_id)
            best = math.inf
            for mid in candidates:
                rate = max(self.effective_rate(mid), self.topo.machines[mid].rated_rate_uph * 0.25)
                wait = self._machine_free_in_min(mid)
                best = min(best, wait + 60.0 * order.remaining / rate)
            return best if best < math.inf else 24 * 60.0
        rt = self.runtime[machine_id]
        rate = self.effective_rate(machine_id)
        wait = rt.changeover_remaining_min + rt.maintenance_remaining_min
        if rate <= 1e-6:
            machine = self.topo.machines[machine_id]
            if rt.state is MachineState.MAINTENANCE:
                rate = machine.rated_rate_uph * rt.load_pct
            elif rt.changeover_remaining_min > 0:
                rate = machine.rated_rate_uph * rt.load_pct * max(0.05, rt.health / 100.0)
            else:
                return 24 * 60.0  # 停機且沒有復機計畫
        return wait + 60.0 * order.remaining / rate

    def _machine_free_in_min(self, machine_id: str) -> float:
        rt = self.runtime[machine_id]
        busy = 0.0
        if rt.assigned_order and rt.assigned_order in self.orders:
            busy = self.estimate_finish_min(rt.assigned_order) if not self.orders[rt.assigned_order].done else 0.0
        return busy + rt.changeover_remaining_min + rt.maintenance_remaining_min

    # ------------------------------------------------------------------ 動作
    def apply(self, action: Action) -> ActionEffect:
        """執行一個動作，真的改變孿生體狀態。"""
        handler = {
            ActionKind.RAISE_ALERT: self._act_alert,
            ActionKind.CREATE_WORK_ORDER: self._act_work_order,
            ActionKind.UPDATE_SCHEDULE: self._act_update_schedule,
            ActionKind.TRANSFER_ORDER: self._act_transfer_order,
            ActionKind.DERATE_MACHINE: self._act_derate,
            ActionKind.STOP_MACHINE: self._act_stop,
            ActionKind.START_MAINTENANCE: self._act_maintenance,
            ActionKind.SAFETY_OVERRIDE: self._act_safety_override,
        }.get(action.kind)
        if handler is None:
            return ActionEffect(False, action.kind.value, action.target, "不支援的動作")
        effect = handler(action)
        self.event_log.append({"tick": self.tick, "type": "action", **effect.to_dict()})
        return effect

    def _act_alert(self, action: Action) -> ActionEffect:
        return ActionEffect(True, action.kind.value, action.target, f"已發出告警：{action.rationale or action.target}")

    def _act_work_order(self, action: Action) -> ActionEffect:
        return ActionEffect(True, action.kind.value, action.target, f"已建立維修工單：{action.params.get('work_order_id', '-')}")

    def _act_update_schedule(self, action: Action) -> ActionEffect:
        defer = action.params.get("defer_order")
        if defer and defer in self.orders:
            order = self.orders[defer]
            order.assigned_machine = None
            for rt in self.runtime.values():
                if rt.assigned_order == defer:
                    rt.assigned_order = None
            return ActionEffect(True, action.kind.value, action.target, f"訂單 {defer} 已延後排程")
        return ActionEffect(True, action.kind.value, action.target, "排程已更新")

    def _act_transfer_order(self, action: Action) -> ActionEffect:
        order_id = action.params.get("order_id") or action.target
        to_machine = action.params.get("to_machine")
        if order_id not in self.orders:
            return ActionEffect(False, action.kind.value, action.target, f"找不到訂單 {order_id}")
        if to_machine not in self.runtime:
            return ActionEffect(False, action.kind.value, action.target, f"找不到機台 {to_machine}")
        order = self.orders[order_id]
        if not self.topo.can_produce(to_machine, order.product_id):
            return ActionEffect(False, action.kind.value, action.target, f"{to_machine} 無法生產 {order.product_id}")
        # 「做得出這個產品」還不夠 —— 目標機台必須位在同一個製程階段。
        # 包裝機也「碰得到」P-100，但把加工訂單轉給它並不是一個有意義的排程動作。
        if self.topo.machines[to_machine].kind is not MachineKind.MACHINING:
            return ActionEffect(False, action.kind.value, action.target, f"{to_machine} 不是加工階段機台，無法承接加工訂單")
        source = order.assigned_machine
        if source and source in self.topo.machines:
            if self.topo.stage_index(to_machine) != self.topo.stage_index(source):
                return ActionEffect(
                    False, action.kind.value, action.target,
                    f"{to_machine} 與 {source} 不在同一製程階段，無法互相替代",
                )
        target_rt = self.runtime[to_machine]
        machine = self.topo.machines[to_machine]

        displaced = target_rt.assigned_order if target_rt.assigned_order != order_id else None
        if displaced and displaced in self.orders:
            self.orders[displaced].assigned_machine = None

        for mid, rt in self.runtime.items():
            if rt.assigned_order == order_id and mid != to_machine:
                rt.assigned_order = None
                rt.load_pct = 0.0

        target_rt.assigned_order = order_id
        target_rt.load_pct = float(action.params.get("load_pct", 1.0))
        target_rt.changeover_remaining_min = max(target_rt.changeover_remaining_min, machine.changeover_min)
        if target_rt.state is MachineState.IDLE:
            target_rt.state = MachineState.RUNNING
        order.assigned_machine = to_machine
        # 名目基準（nominal_output_uph）刻意維持不變 —— 恢復率必須和事故前的基準比較。
        return ActionEffect(
            True,
            action.kind.value,
            to_machine,
            f"訂單 {order_id} 轉移至 {to_machine}（換線 {machine.changeover_min:g} 分鐘）",
            {"displaced_order": displaced, "changeover_min": machine.changeover_min},
        )

    def _act_derate(self, action: Action) -> ActionEffect:
        rt = self.runtime.get(action.target)
        if rt is None:
            return ActionEffect(False, action.kind.value, action.target, "找不到機台")
        rt.state = MachineState.DERATED
        return ActionEffect(True, action.kind.value, action.target, f"{action.target} 進入降速運轉模式")

    def _act_stop(self, action: Action) -> ActionEffect:
        rt = self.runtime.get(action.target)
        if rt is None:
            return ActionEffect(False, action.kind.value, action.target, "找不到機台")
        rt.state = MachineState.STOPPED
        return ActionEffect(True, action.kind.value, action.target, f"{action.target} 已停機")

    def _act_maintenance(self, action: Action) -> ActionEffect:
        rt = self.runtime.get(action.target)
        if rt is None:
            return ActionEffect(False, action.kind.value, action.target, "找不到機台")
        machine = self.topo.machines[action.target]
        default_repair = FAULTS[rt.fault].repair_min if rt.fault else machine.repair_min
        duration = float(action.params.get("duration_min", default_repair))
        rt.state = MachineState.MAINTENANCE
        rt.maintenance_remaining_min = duration
        return ActionEffect(
            True, action.kind.value, action.target, f"{action.target} 進入維修（預估 {duration:g} 分鐘）", {"duration_min": duration}
        )

    def _act_safety_override(self, action: Action) -> ActionEffect:
        # Policy 層本來就會擋下來；這裡再擋一次，確保 Simulator 不可能被繞過。
        return ActionEffect(False, action.kind.value, action.target, "Safety Override 為系統禁止動作，已拒絕執行")

    # ------------------------------------------------------------------ 除錯／評分
    def debug_state(self) -> dict[str, Any]:
        """含 Ground Truth 的完整狀態；只給 Benchmark 與測試使用。"""
        return {
            "tick": self.tick,
            "sim_minutes": self.sim_minutes,
            "ground_truth": self.ground_truth,
            "nominal_output_uph": round(self.nominal_output_uph, 2),
            "throughput_uph": round(self.throughput_uph, 2),
            "machines": {
                mid: {
                    "state": rt.state.value,
                    "fault": rt.fault,
                    "fault_progress": round(rt.fault_progress, 3),
                    "health": round(rt.health, 1),
                    "assigned_order": rt.assigned_order,
                    "load_pct": rt.load_pct,
                    "signals": {k: round(v, 2) for k, v in rt.signals.items()},
                }
                for mid, rt in self.runtime.items()
            },
            "orders": {oid: o.to_dict() for oid, o in self.orders.items()},
            "queue": {k: round(v, 1) for k, v in self.queue.items()},
        }


__all__ = ["FactoryTwin", "ActionEffect"]
