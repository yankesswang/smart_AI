"""故障模型（Fault Model）。

規格 §6.2：Fault Injection 只控制 Simulator 的狀態與 Sensor 生成規則，
**不把故障標籤傳給 Agent**；如此才能對 Diagnosis Accuracy 做真實評估。

因此本檔案有兩份東西，且刻意分開：

* ``FaultModel.deltas`` —— Simulator 內部用的「物理效果」，Agent 看不到。
* ``fault_signatures()`` —— Diagnosis Agent 可以看到的「感測器指紋知識」，
  來源是 Demo Equipment Manual（等同工程師手上的手冊），而不是模擬器內部狀態。

兩者在 Demo 中一致是合理的（手冊本來就描述故障徵兆），但它是「知識」不是「答案」：
Agent 仍須從實際訊號比對出最像的那一個，並且會因為雜訊、早期訊號微弱而出錯。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import FaultSignature


@dataclass(frozen=True)
class FaultModel:
    """一種故障如何改變機台的感測器與行為。"""

    fault_id: str
    label: str
    # 在 progress = 1.0 時，各訊號相對正常值的絕對偏移量
    deltas: dict[str, float]
    # 額外的雜訊放大倍率（劣化中的機台訊號會更不穩）
    noise_gain: float = 1.0
    # 若不處理，每 tick 額外累積的劣化速度（用於 residual risk 與二次損壞）
    escalation_per_tick: float = 0.012
    # 二次損壞（例如軸承咬死、主軸損傷）的修復成本 NTD
    secondary_damage_cost_ntd: float = 180_000.0
    # 全速運轉時二次損壞的風險係數
    full_speed_risk_gain: float = 1.0
    manual_refs: tuple[str, ...] = ()
    typical_parts: tuple[str, ...] = ()
    required_skill: str = "mechanical-tech"
    repair_min: float = 40.0
    signature_note: str = ""


FAULTS: dict[str, FaultModel] = {
    "bearing_degradation": FaultModel(
        fault_id="bearing_degradation",
        label="主軸軸承劣化 (Bearing Degradation)",
        deltas={"vibration": 6.8, "temperature": 16.0, "current": 2.2, "rpm_pct": -4.0},
        noise_gain=1.9,
        escalation_per_tick=0.014,
        secondary_damage_cost_ntd=210_000.0,
        full_speed_risk_gain=1.25,
        manual_refs=("MAN-A-3.2", "SOP-MT-07"),
        typical_parts=("SPINDLE-BRG-6208", "GREASE-NLGI2", "BRG-SEAL-KIT"),
        required_skill="mechanical-tech (L2)",
        repair_min=40.0,
        signature_note="Vibration 顯著上升、Temperature 隨之上升、Current 小幅上升、RPM 略降。",
    ),
    "cooling_failure": FaultModel(
        fault_id="cooling_failure",
        label="冷卻系統失效 (Cooling Failure)",
        deltas={"temperature": 26.0, "vibration": 0.5, "current": 0.6, "rpm_pct": -1.5},
        noise_gain=1.2,
        escalation_per_tick=0.018,
        secondary_damage_cost_ntd=150_000.0,
        full_speed_risk_gain=1.4,
        manual_refs=("MAN-A-4.1", "SOP-MT-11"),
        typical_parts=("COOLANT-PUMP-CP12", "COOLANT-FILTER", "COOLANT-45L"),
        required_skill="utility-tech (L2)",
        repair_min=30.0,
        signature_note="Temperature 大幅上升，Vibration 與 Current 幾乎維持正常。",
    ),
    "motor_overload": FaultModel(
        fault_id="motor_overload",
        label="主軸馬達過載 (Motor Overload)",
        deltas={"current": 6.0, "temperature": 13.0, "rpm_pct": -18.0, "vibration": 1.5},
        noise_gain=1.4,
        escalation_per_tick=0.020,
        secondary_damage_cost_ntd=260_000.0,
        full_speed_risk_gain=1.5,
        manual_refs=("MAN-A-5.3", "SOP-MT-04"),
        typical_parts=("SERVO-DRV-7K5", "MOTOR-FAN", "POWER-CONTACTOR"),
        required_skill="electrical-tech (L3)",
        repair_min=55.0,
        signature_note="Current 大幅上升且 RPM 明顯下降，Temperature 中度上升。",
    ),
}


# 純工安事件：不改變機台感測器，只改變 Camera / 環境觀測。
HAZARD_EVENT_ID = "hazard_zone_intrusion"


def fault_signatures(scales: dict[str, float]) -> list[FaultSignature]:
    """把故障模型轉成 Diagnosis Agent 使用的「正規化指紋」。

    以每個訊號的 scale 正規化，讓不同單位（°C / mm/s / A / %）可以放在同一個向量空間比較。
    """
    sigs: list[FaultSignature] = []
    for model in FAULTS.values():
        profile = {sig: delta / scales.get(sig, 1.0) for sig, delta in model.deltas.items()}
        sigs.append(
            FaultSignature(
                fault_id=model.fault_id,
                label=model.label,
                profile=profile,
                manual_refs=model.manual_refs,
                typical_parts=model.typical_parts,
                required_skill=model.required_skill,
                repair_min=model.repair_min,
            )
        )
    return sigs


__all__ = ["FaultModel", "FAULTS", "HAZARD_EVENT_ID", "fault_signatures"]
