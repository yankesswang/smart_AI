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

from ..acoustics.signatures import INDICATOR_NAMES
from ..acoustics.synthetic import clamp_indicator, target_indicators
from ..domain import (
    AcousticObservation,
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
from .disturbances import Disturbance, RealityGap, SensorFault, SignatureMismatch
from .energy import EnergyLedger
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
# 降速運轉的**預設**工作點：產能保留六成、轉速降到 63%、故障表現被壓到 0.55。
# 這三個數字是同一個工作點的三個面向，所以它們必須跟著 factor 一起動 ——
# 早期版本把 factor 收下來卻不用，於是「降到四成」和「降到八成」跑出完全一樣的結果，
# 方案比較因此少了一個真正的維度。
DERATE_LOAD_FACTOR = 0.60
DERATE_RPM_PCT = 63.0
DERATE_FAULT_RELIEF = 0.55
# factor 的合理範圍：低於 0.2 等於停機（那應該用 STOP_MACHINE 表達），
# 高於 0.95 等於沒降速。
DERATE_FACTOR_MIN = 0.20
DERATE_FACTOR_MAX = 0.95

THROUGHPUT_EMA_ALPHA = 0.45

# --- 麥克風（合成聲學觀測）-------------------------------------------------------------
# ⚠️ 這裡產生的是**合成音訊特徵**，由既有的振動／轉速／電流物理推導，不是真實錄音。
#    真實工業錄音（DCASE2020 / MIMII）只用來驗證偵測器，不參與本迴圈。
#    完整的邊界說明見 acoustics/synthetic.py 與 docs/acoustic_validation.md。
# 只有加工機台裝麥克風：本 Demo 的三個故障模型都發生在主軸／冷卻／馬達上，
# 包裝機沒有 vibration 訊號，也就沒有對應的聲學故障模型可以誠實地渲染。
MIC_SAMPLE_RATE_HZ = 16_000
MIC_WINDOW_S = 10.0
# 一階遲滯：聲音對狀態變化的反應比溫度快得多，和 vibration 同一個量級。
ACOUSTIC_ALPHA = 0.75
# 量測雜訊標準差（麥克風本底 + 現場環境）。
ACOUSTIC_NOISE: dict[str, float] = {
    "spl_db": 0.35,
    "high_band_ratio": 0.006,
    "tonal_ratio": 0.008,
    "crest_factor_db": 0.30,
}

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
    # --- 麥克風（合成）---
    # 與 clean_signals / signals 完全相同的兩層結構，理由也相同。
    # 刻意不併進 signals：那會讓聲學指標流進 MachineSnapshot.readings，
    # 進而改變 worst_band、健康度與感測器指紋向量 —— 那些都是既有設計的一部分，
    # 不該因為多裝一支麥克風而被動搖。
    clean_acoustics: dict[str, float] = field(default_factory=dict)
    acoustics: dict[str, float] = field(default_factory=dict)
    health: float = 100.0
    last_rate_uph: float = 0.0
    repairs_done: int = 0
    secondary_damage: bool = False
    # 這次維修「排了多久」。工單上的工時是依**診斷結果**開出來的，
    # 診斷錯了就可能排得比實際需要的短 —— 見 _advance_timers 的不完整維修。
    maintenance_planned_min: float = 0.0
    # 目前的降速工作點（產能保留比例）。預設值讓沒有指定 factor 的降速行為完全不變。
    derate_factor: float = DERATE_LOAD_FACTOR


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
        # 能源／碳排帳：逐 tick 由感測器電流積分出來（見 twin/energy.py）。
        self.energy = EnergyLedger.for_machines(
            {mid: m.rated_power_kw for mid, m in self.topo.machines.items()}
        )
        self.event_log: list[dict[str, Any]] = []
        self.label = "live"
        # --- 干擾與現實落差（見 twin/disturbances.py）---
        # 這三個容器都是 Simulator 內部設定，和 fault 一樣不會出現在 snapshot() 裡。
        # 它們不在 reset() 裡被清掉：和拓撲一樣，屬於「這座工廠長什麼樣」的設定，
        # 而不是「這一次模擬跑到哪裡」的狀態。
        self.disturbances: list[Disturbance] = []
        self.sensor_faults: list[SensorFault] = []
        self.signature_mismatch: dict[tuple[str, str], SignatureMismatch] = {}
        self._stuck_values: dict[tuple[str, str], float] = {}
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
        self.energy = EnergyLedger.for_machines(
            {mid: m.rated_power_kw for mid, m in self.topo.machines.items()}
        )
        self.event_log = []
        self._stuck_values = {}
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
            if self._has_microphone(machine):
                # 健康機台的名目聲學狀態。與 clean_signals 從 spec.nominal 起跳同一個道理：
                # 期初不該有假的暫態。
                rt.clean_acoustics = target_indicators(None, 0.0)
                rt.acoustics = dict(rt.clean_acoustics)
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

    # ------------------------------------------------------------------ 干擾與現實落差
    def add_disturbance(self, disturbance: Disturbance) -> None:
        """加入一段「沒有故障」的訊號擾動（負載切換、暖機、感測器尖峰…）。

        刻意和 ``inject()`` 分開：注入故障會設定 ``rt.fault``（Ground Truth），
        干擾不會 —— 它的 Ground Truth 就是「無故障」，所以任何因它而起的告警都是誤報。
        """
        self.disturbances.append(disturbance)

    def apply_reality_gap(self, gap: RealityGap | None) -> None:
        """設定這座工廠與「手冊典型值」之間的落差。

        Agent 拿不到這份設定，這是刻意的：手冊寫的是典型機台，
        現場這一台可能不一樣，而**沒有人事先知道差多少**。
        """
        if gap is None:
            return
        for mismatch in gap.signature_mismatch:
            self.signature_mismatch[(mismatch.machine_id, mismatch.fault_id)] = mismatch
        self.sensor_faults.extend(gap.sensor_faults)

    def _mismatch_for(self, machine_id: str, fault_id: str | None) -> SignatureMismatch | None:
        if fault_id is None:
            return None
        return self.signature_mismatch.get((machine_id, fault_id))

    def _disturbance_offset(self, machine_id: str, signal: str, sensor_only: bool) -> float:
        """這一 tick 所有干擾在這個訊號上的合計偏移。"""
        total = 0.0
        for dist in self.disturbances:
            if dist.machine_id != machine_id or dist.sensor_only is not sensor_only:
                continue
            delta = dist.deltas.get(signal)
            if not delta:
                continue
            total += delta * dist.amplitude(self.tick)
        return total

    def _apply_sensor_faults(self, machine_id: str, signal: str, reported: float) -> float:
        """感測器本身的問題：偏移或卡值。只動**回報值**，機台物理狀態不變。"""
        for fault in self.sensor_faults:
            if fault.machine_id != machine_id or fault.signal != signal:
                continue
            if self.tick < fault.start_tick:
                continue
            if fault.mode == "drift":
                elapsed = self.tick - fault.start_tick
                offset = fault.drift_per_tick * elapsed
                if fault.drift_max:
                    offset = (
                        min(offset, fault.drift_max) if fault.drift_per_tick >= 0
                        else max(offset, -abs(fault.drift_max))
                    )
                reported += offset
            elif fault.mode == "stuck":
                key = (machine_id, signal)
                if key not in self._stuck_values:
                    # 沒指定卡在哪個值就卡在「故障發生當下讀到的值」——
                    # 這才是卡值的真實樣子：它會停在一個曾經合理的數字上。
                    self._stuck_values[key] = fault.stuck_value if fault.stuck_value is not None else reported
                reported = self._stuck_values[key]
        return reported

    def fork(self, label: str = "dryrun") -> "FactoryTwin":
        """複製一份完全獨立的孿生體，用於方案乾跑。拓撲是唯讀的所以共用。"""
        clone = copy.copy(self)
        clone.runtime = copy.deepcopy(self.runtime)
        clone.orders = copy.deepcopy(self.orders)
        clone.queue = dict(self.queue)
        clone.pending_injections = list(self.pending_injections)
        # 干擾與現實落差也要各自帶一份，否則乾跑會改到真實孿生體的卡值狀態。
        clone.disturbances = list(self.disturbances)
        clone.sensor_faults = list(self.sensor_faults)
        clone.signature_mismatch = dict(self.signature_mismatch)
        clone._stuck_values = dict(self._stuck_values)
        # 能源帳必須跟著複製一份 —— 少了這行，方案乾跑（一個情境會跑好幾個）
        # 累積的電就會被記到真實孿生體上，KPI 直接失真。
        clone.energy = copy.deepcopy(self.energy)
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

        信念模型也會把**現實落差全部清掉**（指紋偏差、感測器故障、環境干擾）。
        這不是簡化，這是同一條紅線的延伸：Agent 只讀得到手冊，
        它不可能知道「這一台機器的軸承劣化溫升比手冊高四成」。
        規劃因此永遠跑在手冊物理上 —— 而現場物理不照手冊走的時候，
        投影就會偏樂觀，然後由 Verification Agent 在真實孿生體上抓出來。
        """
        clone = self.fork(label=label)
        clone.disturbances = []
        clone.sensor_faults = []
        clone.signature_mismatch = {}
        clone._stuck_values = {}
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
        # 派工／生產都結束後，機台這一 tick 的最終狀態才確定，這時才結算電。
        self._accumulate_energy()
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
                # 現實落差：這一台機器可能壞得比手冊寫的快。手冊值仍是規劃用的那一份，
                # 所以「照手冊投影出來還撐得住」的方案，在這裡就會撐不住。
                mismatch = self._mismatch_for(rt.machine_id, rt.fault)
                if mismatch is not None:
                    gain *= mismatch.escalation_gain
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
                    self._finish_maintenance(rt)

    def _finish_maintenance(self, rt: _MachineRuntime) -> None:
        """維修時間到了 —— 但修不修得好，取決於工單排的工時夠不夠。

        為什麼要有這一段：工單上的工時是依**診斷結果**開出來的
        （冷卻系統 30 分鐘、軸承 40 分鐘、馬達 55 分鐘）。診斷錯了，
        技師就會帶著錯的零件、排著不夠的工時進場，時間到了把機台交回，
        故障其實沒有被排除。少了這一段，「診斷錯」在模擬裡完全沒有代價 ——
        因為不管開什麼工單，維修都會把 fault 清成 None，
        於是 Verification Agent 永遠抓不到任何東西，重試路徑也就永遠跑不到。
        """
        rt.state = MachineState.IDLE
        rt.repairs_done += 1
        required = FAULTS[rt.fault].repair_min if rt.fault else 0.0
        # 現實落差：手冊工時是典型值，這一台可能要更久。
        mismatch = self._mismatch_for(rt.machine_id, rt.fault)
        if mismatch is not None:
            required *= mismatch.repair_gain
        planned = rt.maintenance_planned_min
        if rt.fault and required > 0 and planned + 1e-9 < required:
            # 工時不足：故障只被處理掉一部分（換了能換的、沒換到真正該換的）。
            ratio = max(0.0, min(1.0, planned / required))
            rt.fault_progress *= 1.0 - ratio
            # fault_start_tick 要跟著回推，否則 _advance_faults 的
            # ramp = elapsed / ramp_ticks 會在下一 tick 立刻把 progress 拉回原位，
            # 「修了一半」就會變成完全沒修。
            rt.fault_start_tick = self.tick - int(rt.fault_progress * rt.fault_ramp_ticks)
            self.event_log.append({
                "tick": self.tick, "type": "repair_incomplete", "machine": rt.machine_id,
                "fault": rt.fault, "planned_min": round(planned, 1), "required_min": round(required, 1),
            })
        else:
            rt.fault = None
            rt.fault_progress = 0.0
            rt.secondary_damage = False
            self.event_log.append({"tick": self.tick, "type": "maintenance_done", "machine": rt.machine_id})
        rt.maintenance_planned_min = 0.0

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
    def _target_signal(self, rt: _MachineRuntime, spec: SignalSpec, ignore_fault: bool = False) -> float:
        """這個訊號在目前狀態下的穩態目標值。

        ``ignore_fault=True`` 回傳「同一台機器、同一個控制狀態、但設備健康」時的目標值。
        能源模型用它當名目基線 —— 注意它只讀控制狀態與訊號規格（設備銘牌），
        不讀故障標籤，所以基線本身不含任何 Ground Truth。
        """
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
            # 三個訊號都隨降速幅度縮放，基準點是預設工作點（factor = 0.60）。
            ratio = rt.derate_factor / DERATE_LOAD_FACTOR
            if spec.name == "rpm_pct":
                base = DERATE_RPM_PCT * ratio
            elif spec.name == "current":
                base = spec.nominal * 0.72 * ratio
            elif spec.name == "temperature":
                # 降得越低越涼：溫降幅度和「少掉的那幾成產能」成正比。
                base = spec.nominal - 4.0 * (1.0 - rt.derate_factor) / (1.0 - DERATE_LOAD_FACTOR)

        if rt.fault and not ignore_fault:
            model: FaultModel = FAULTS[rt.fault]
            delta = model.deltas.get(spec.name, 0.0)
            # 現實落差：手冊描述的是典型機台，這一台的表現可以不一樣。
            # 注意這個倍率只在 Simulator 內部生效 —— Diagnosis Agent 比對的仍是手冊指紋，
            # 所以徵兆偏離手冊時，它會判錯或信心不足，這正是要被量出來的事。
            mismatch = self._mismatch_for(rt.machine_id, rt.fault)
            if mismatch is not None:
                delta *= mismatch.scale.get(spec.name, 1.0)
            relief = self._derate_relief(rt)
            base += delta * rt.fault_progress * relief

        # 環境溫度耦合：室溫越高，機台溫度越高。
        if spec.name == "temperature":
            base += (self.ambient_temp_c - 28.0) * 0.6

        # 干擾：負載切換、暖機、換料重啟 —— 機台沒有故障，但物理狀態確實改變了。
        # 放在 ignore_fault 之外是刻意的：能源模型的「健康基線」也該包含這些工況，
        # 否則一次負載切換會被整段記成「劣化多耗的電」。
        base += self._disturbance_offset(rt.machine_id, spec.name, sensor_only=False)
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
                reported = clean + noise
                # 量測層的干擾與感測器故障：只動「感測器回報什麼」，不動機台的物理狀態。
                # 所以真實健康度（_health_of 用 clean_signals）、產出速率與能耗都不受影響 ——
                # 一支壞掉的感測器不會讓機台真的少做幾件，但會讓 Agent 看到一個假的世界。
                reported += self._disturbance_offset(mid, spec.name, sensor_only=True)
                reported = self._apply_sensor_faults(mid, spec.name, reported)
                rt.signals[spec.name] = max(0.0, reported)
            if self._has_microphone(machine):
                self._refresh_acoustics(mid, rt, noise_gain=noise_gain, active=active)
            rt.health = self._health_of(machine, rt)

    @staticmethod
    def _has_microphone(machine: Machine) -> bool:
        """哪些機台裝了麥克風。

        只有加工機台。理由不是「包裝機不會壞」，而是**誠實**：本 Demo 的三個故障模型
        （軸承／冷卻／馬達）都發生在主軸系統上，包裝機連 vibration 訊號都沒有，
        也就沒有任何可以據以推導的聲學物理。硬給它一支麥克風，等於憑空編一組數字。
        """
        return machine.signal("vibration") is not None

    def _refresh_acoustics(
        self, machine_id: str, rt: _MachineRuntime, *, noise_gain: float, active: bool
    ) -> None:
        """更新麥克風觀測。

        ⚠️ **合成音訊特徵**：目標值由 `acoustics/synthetic.py` 從既有的故障物理推導，
        不是真實錄音。真實工業錄音（DCASE2020 / MIMII）只用來驗證偵測器本身有效，
        不參與這個迴圈 —— 見 `docs/acoustic_validation.md`。

        流程與 `_refresh_signals` 的感測器路徑逐字對應，這是刻意的：
        目標值 → 一階遲滯 → 量測雜訊。聲音是「多一個感測器」，不是「另一套規則」。
        Ground Truth 的處理也一樣 —— `rt.fault` 只在這裡（Simulator 內部）被讀到，
        送出去的 `AcousticObservation` 只有四個數字。
        """
        derated = rt.state is MachineState.DERATED
        relief = self._derate_relief(rt)
        # 現實落差也要傳到聲音上，理由和降速一樣：同一個物理事實不能在兩個模態上說不同的話。
        # 一台把振動吸收掉七成的機器，麥克風聽到的也會少七成 ——
        # 若只縮小振動而讓聲音維持手冊值，等於偷偷留了一條「聲音仍然知道答案」的後門。
        mismatch = self._mismatch_for(machine_id, rt.fault)
        if mismatch is not None:
            relief *= mismatch.scale.get("vibration", 1.0)
        target = target_indicators(
            rt.fault,
            rt.fault_progress,
            online=active,
            derated=derated,
            # 降速運轉會減輕故障的表現，聲音也一樣 —— 沿用感測器路徑的同一個係數，
            # 否則同一個動作在振動上有效、在聲音上無效，兩個模態會自相矛盾。
            fault_relief=relief,
        )
        for name in INDICATOR_NAMES:
            current = rt.clean_acoustics.get(name, target[name])
            clean = current + (target[name] - current) * ACOUSTIC_ALPHA
            rt.clean_acoustics[name] = clamp_indicator(name, clean)
            sigma = ACOUSTIC_NOISE.get(name, 0.0)
            # 雜訊種子多一個 "mic" 維度，避免和同名感測器訊號抽到同一個亂數。
            noise = _stable_gauss(self.seed, machine_id, "mic", name, self.tick) * sigma * (
                noise_gain if active else 0.4
            )
            rt.acoustics[name] = clamp_indicator(name, rt.clean_acoustics[name] + noise)

    @staticmethod
    def _derate_relief(rt: _MachineRuntime) -> float:
        """降速對「故障表現」的壓抑係數。

        降得越低，故障在感測器（與麥克風）上表現得越少 —— 但實際故障沒有變好，
        這正是降速方案的危險之處：訊號看起來緩和了，設備仍在劣化。
        """
        if rt.state is not MachineState.DERATED:
            return 1.0
        return DERATE_FAULT_RELIEF * (rt.derate_factor / DERATE_LOAD_FACTOR)

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

    # ------------------------------------------------------------------ 能源／碳排
    def _accumulate_energy(self) -> None:
        """把這一 tick 的用電積分進能源帳。

        沒有新的物理假設：功率直接由**既有的** current 訊號推導，
        名目基線則是同一個狀態下把故障偏移拿掉後的電流（``ignore_fault=True``）。
        設備劣化造成的電流上升本來就在模擬裡，這裡只是把它積出來。
        """
        alpha = SIGNAL_ALPHA.get("current", 0.8)
        for mid, rt in self.runtime.items():
            machine = self.topo.machines[mid]
            spec = machine.signal("current")
            if spec is None:
                continue
            self.energy.accumulate(
                mid,
                # 用 clean_signals 而非帶雜訊的讀值：量測雜訊不該讓機台真的多耗電
                # （與 effective_rate() 用 clean rpm 的理由一致）。
                actual_current_a=rt.clean_signals.get(spec.name, spec.nominal),
                healthy_target_current_a=self._target_signal(rt, spec, ignore_fault=True),
                nominal_current_a=spec.nominal,
                minutes=self.tick_minutes,
                online=rt.state not in (MachineState.STOPPED, MachineState.MAINTENANCE),
                alpha=alpha,
            )

    def energy_kpi(self) -> dict[str, float]:
        """能源／碳排 KPI（永續發展性）。數字是逐 tick 量出來的，不是事後估算。"""
        return self.energy.summary(produced_units=self.completed_units_total)

    # ------------------------------------------------------------------ 生產
    def effective_rate(self, machine_id: str) -> float:
        """機台目前的實際產出速率（件/小時）。"""
        machine = self.topo.machines[machine_id]
        rt = self.runtime[machine_id]
        if rt.state in (MachineState.STOPPED, MachineState.MAINTENANCE, MachineState.IDLE):
            return 0.0
        if rt.changeover_remaining_min > 0:
            return 0.0
        state_factor = rt.derate_factor if rt.state is MachineState.DERATED else 1.0
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
            ppe = _stable_unit(self.seed, "ppe", self.tick) > 0.7
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
            acoustics=self._acoustic_observation(machine_id, rt),
        )

    def _acoustic_observation(self, machine_id: str, rt: _MachineRuntime) -> AcousticObservation | None:
        """把麥克風的內部狀態包成 Agent 看得到的觀測。

        只送四個數字出去。刻意**不**附任何 per-fault 的相似度或分數 ——
        那等於把 Ground Truth 用另一個名字送給 Agent。要比對哪一個故障最像，
        是 Diagnosis Agent 拿手冊知識（`acoustics/signatures.py`）自己算的事。
        """
        if not rt.acoustics:
            return None
        return AcousticObservation(
            machine_id=machine_id,
            sensor_id=f"MIC-{machine_id.split('-')[-1]}",
            sample_rate_hz=MIC_SAMPLE_RATE_HZ,
            window_s=MIC_WINDOW_S,
            spl_db=rt.acoustics["spl_db"],
            high_band_ratio=rt.acoustics["high_band_ratio"],
            tonal_ratio=rt.acoustics["tonal_ratio"],
            crest_factor_db=rt.acoustics["crest_factor_db"],
            synthetic=True,
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
            # 能源／碳排：由感測器電流積分而來，Agent 與 Dashboard 都可以安全讀取。
            **self.energy_kpi(),
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
        # factor = 產能保留比例。沒給就用預設工作點，行為與加入這個參數之前完全相同。
        factor = float(action.params.get("factor", DERATE_LOAD_FACTOR))
        rt.derate_factor = max(DERATE_FACTOR_MIN, min(DERATE_FACTOR_MAX, factor))
        rt.state = MachineState.DERATED
        return ActionEffect(
            True, action.kind.value, action.target,
            f"{action.target} 進入降速運轉模式（保留 {rt.derate_factor:.0%} 產能）",
            {"factor": rt.derate_factor},
        )

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
        # 記下工單排了多久，維修結束時才判得出這次修得完不完整（見 _finish_maintenance）。
        rt.maintenance_planned_min = duration
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
            "energy": self.energy.to_dict(produced_units=self.completed_units_total),
            # 干擾與現實落差和 fault 一樣屬於 Ground Truth：只出現在這裡，不出現在 snapshot()。
            "disturbances": [d.to_dict() for d in self.disturbances],
            "sensor_faults": [s.to_dict() for s in self.sensor_faults],
            "signature_mismatch": [m.to_dict() for m in self.signature_mismatch.values()],
        }


__all__ = ["FactoryTwin", "ActionEffect"]
