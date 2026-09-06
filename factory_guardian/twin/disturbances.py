"""干擾（Disturbance）與現實落差（Reality Gap）。

這個檔案存在的理由是 Benchmark 的兩個誠實度問題：

1. **只有故障情境，就量不出 False Positive。**
   規格 §10 要求 False Positive Rate，但如果每一個情境都真的有故障，
   那一格永遠是空的 —— 「零誤報」不是量出來的，是因為沒有機會誤報。
   所以這裡定義一組**沒有故障**的干擾：負載切換、換料重啟、暖機、單一感測器尖峰。
   它們的 Ground Truth 是「無故障」，因此任何在這些情境下觸發的告警都是誤報。

2. **指紋來自同一份 FAULTS deltas，診斷正確率必然 100%。**
   Diagnosis Agent 比對的手冊指紋（`fault_signatures()`）和 Simulator 產生訊號用的
   `FaultModel.deltas` 是同一組數字，這是循環論證：手冊怎麼寫，機台就怎麼壞。
   `RealityGap` 打破這個循環 —— 它讓**這一台**機器的物理與手冊描述的典型值有落差
   （`SignatureMismatch`），或讓某個感測器本身出問題（`SensorFault`）。
   故障是真的，只是徵兆和手冊寫的不一樣，於是診斷會先判錯，
   再由 Verification Agent 在真實孿生體上抓出來並觸發重新規劃。

三條紅線在這裡一樣成立：

* 干擾與落差都只改變 Simulator 內部狀態或感測器讀值，**不產生任何給 Agent 的標籤**。
* `fork_as_belief()` 會把這些設定全部清掉 —— 規劃模型用的是**手冊物理**，
  因為 Agent 只知道手冊。它若知道這台機的真實落差，就等於偷看了答案。
* 所有數值都是可重現的（沒有新的亂數來源），合成資料一律標示 synthetic。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Disturbance:
    """一段**沒有故障**的訊號擾動。

    形狀是梯形：``ramp_ticks`` 線性上升 → ``hold_ticks`` 維持 →
    ``decay_ticks`` 線性衰減到 ``residual``。
    ``decay_ticks = 0`` 代表不衰減（例如負載切換到新的穩態，會一直維持下去）。

    ``sensor_only=True`` 時只改變**感測器回報值**，機台的物理狀態完全沒動 ——
    這是「量測異常」而不是「機台異常」，健康度與產出速率都不受影響。
    """

    machine_id: str
    label: str
    deltas: dict[str, float]          # amplitude = 1.0 時，各訊號相對當下目標值的偏移
    start_tick: int
    ramp_ticks: int = 2
    hold_ticks: int = 6
    decay_ticks: int = 0              # 0 = 不衰減，維持到情境結束
    residual: float = 0.0             # 衰減後殘留的比例（暖機後的穩態溫升就是殘留）
    sensor_only: bool = False
    note: str = ""

    def amplitude(self, tick: int) -> float:
        """這一 tick 的擾動強度（0 ~ 1）。

        上升與衰減都用 ``(elapsed + 1) / ticks``，所以
        ``ramp_ticks=1, hold_ticks=0, decay_ticks=1`` 剛好是**一個 tick 的尖峰**。
        兩邊用同一個慣例，讀參數的人才不必記兩套規則。
        """
        elapsed = tick - self.start_tick
        if elapsed < 0:
            return 0.0
        if elapsed < self.ramp_ticks:
            return (elapsed + 1) / max(1, self.ramp_ticks)
        elapsed -= self.ramp_ticks
        if elapsed < self.hold_ticks:
            return 1.0
        elapsed -= self.hold_ticks
        if self.decay_ticks <= 0:
            return 1.0
        frac = (elapsed + 1) / self.decay_ticks
        if frac >= 1.0:
            return self.residual
        return 1.0 + (self.residual - 1.0) * frac

    def to_dict(self) -> dict[str, object]:
        return {
            "machine_id": self.machine_id,
            "label": self.label,
            "deltas": dict(self.deltas),
            "start_tick": self.start_tick,
            "ramp_ticks": self.ramp_ticks,
            "hold_ticks": self.hold_ticks,
            "decay_ticks": self.decay_ticks,
            "residual": self.residual,
            "sensor_only": self.sensor_only,
            "note": self.note,
            "synthetic": True,
        }


@dataclass(frozen=True)
class SignatureMismatch:
    """這一台機器的故障物理，與手冊描述的典型指紋之間的落差。

    ``scale`` 是逐訊號的倍率（1.0 = 和手冊一致）。
    ``escalation_gain`` 則是劣化速度的倍率 —— 有些機台壞得比手冊寫的快，
    這會讓「照手冊投影出來的方案」在真實孿生體上撐不住，正是 Verification 該抓到的東西。
    """

    machine_id: str
    fault_id: str
    scale: dict[str, float] = field(default_factory=dict)
    escalation_gain: float = 1.0
    # 這一台實際要修多久，相對於手冊標準工時的倍率。
    # 工單上的工時是照手冊開的；倍率大於 1 代表照手冊排的時間**不夠**，
    # 技師時間到了把機台交回，故障其實只被處理掉一部分（見 engine._finish_maintenance）。
    repair_gain: float = 1.0
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "machine_id": self.machine_id,
            "fault_id": self.fault_id,
            "scale": dict(self.scale),
            "escalation_gain": self.escalation_gain,
            "repair_gain": self.repair_gain,
            "note": self.note,
        }


@dataclass(frozen=True)
class SensorFault:
    """感測器本身的問題：偏移（drift）或卡值（stuck）。

    只改變**回報值**，機台的物理狀態沒有變。所以真實健康度、產出速率、能耗都不受影響，
    但 Agent 看到的世界會被扭曲 —— 這正是它該被考驗的地方。
    """

    machine_id: str
    signal: str
    mode: str                          # "drift" | "stuck"
    start_tick: int = 0
    drift_per_tick: float = 0.0        # drift：每 tick 累加的偏移量
    drift_max: float = 0.0             # drift：偏移上限
    stuck_value: float | None = None   # stuck：卡在這個讀值（None = 卡在故障發生當下的值）
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "machine_id": self.machine_id,
            "signal": self.signal,
            "mode": self.mode,
            "start_tick": self.start_tick,
            "drift_per_tick": self.drift_per_tick,
            "drift_max": self.drift_max,
            "stuck_value": self.stuck_value,
            "note": self.note,
        }


@dataclass(frozen=True)
class RealityGap:
    """一個情境的「現實落差」設定包。"""

    signature_mismatch: tuple[SignatureMismatch, ...] = ()
    sensor_faults: tuple[SensorFault, ...] = ()
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "signature_mismatch": [m.to_dict() for m in self.signature_mismatch],
            "sensor_faults": [s.to_dict() for s in self.sensor_faults],
            "note": self.note,
        }


__all__ = ["Disturbance", "RealityGap", "SensorFault", "SignatureMismatch"]
