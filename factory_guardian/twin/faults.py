"""故障模型（Fault Model）。

規格 §6.2：Fault Injection 只控制 Simulator 的狀態與 Sensor 生成規則，
**不把故障標籤傳給 Agent**；如此才能對 Diagnosis Accuracy 做真實評估。

⚠ **``FaultModel.deltas`` 是 Simulator 的內部參數，Diagnosis Agent 不得讀取。**

早期版本有一個 ``fault_signatures()``，把 ``deltas`` 除以 scale 當成給 Agent 的
「感測器指紋」。當時的理由是「手冊本來就描述故障徵兆，兩者一致很合理」——
但那在數學上站不住腳：模擬器產生訊號用的是 ``deltas × progress``，
指紋是 ``deltas ÷ scale``，兩者共線。餘弦相似度對純量免疫，
所以正確答案的餘弦**恆等於 1.0**，診斷退化成查表。

現在 Agent 的徵兆知識改由 ``knowledge/symptom_spec.py`` 提供：
手冊寫的是**區間**、中心刻意偏離這裡的 deltas、且各故障區間彼此重疊，
分辨要靠鑑別診斷規則的**比值**。判讀基準則來自
``knowledge/commissioning.py`` 的各機台交機驗收值。
Agent 因此會因為雜訊、早期訊號微弱、徵兆重疊而真的出錯 —— 那才是可信的評估。

``FaultModel`` 的其餘欄位（維修工時、零件、風險係數）不是診斷答案，
是維修規劃用的共通知識，Agent 在**確定根因之後**才會用到。
"""

from __future__ import annotations

from dataclasses import dataclass


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


__all__ = ["FaultModel", "FAULTS", "HAZARD_EVENT_ID"]
