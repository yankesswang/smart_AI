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

from ..domain import FaultPrototype, FaultSignature


@dataclass(frozen=True)
class AltSignature:
    """同一個故障的**另一個**徵兆方向（手冊語意，不是模擬器行為）。

    ## 為什麼需要它

    `docs/external_validation.md` §8.2 在 AI4I 2020 上量到「一個故障一個原型」的結構性代價：
    雙側故障（功率過低**或**過高）的單一質心會落在兩簇中間，方向失去意義。
    改成每模式 2 個原型後 Top-1 從 0.724 升到 0.821。

    ## 為什麼它放在這裡，而且不影響 Simulator

    `FaultModel.deltas` 是**模擬器怎麼演**（Agent 看不到），`alt_signatures` 是
    **手冊怎麼寫**（Agent 看得到）。Demo 的注入情境只會走 `deltas` 那條主方向，
    所以多這些原型不會讓 Simulator 產生任何新的訊號，也不會有任何標籤流向 Agent ——
    它純粹是「工程師手冊上還寫了另一種表現」這件領域知識。

    每一項都必須寫 `rationale`：多一個原型就是多一個可以誤命中的方向，
    沒有手冊理由的原型等於在替方法開後門。
    """

    name: str
    deltas: dict[str, float]
    rationale: str


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
    # 手冊上同一個故障的其他徵兆方向。只影響 `fault_signatures()`（Agent 側的知識），
    # 完全不影響 `deltas`（Simulator 側的物理效果）。
    alt_signatures: tuple[AltSignature, ...] = ()


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
        alt_signatures=(
            AltSignature(
                name="overcooling",
                # 冷卻過度：溫度低於正常帶、電流略升、振動略升、轉速達成率小幅下降。
                deltas={"temperature": -12.0, "current": 0.8, "vibration": 0.4, "rpm_pct": -0.8},
                rationale=(
                    "MAN-A-4.1 把這個故障定義為「冷卻迴路失去調節能力」，而不是「冷卻不足」。"
                    "調節閥卡在全開、或冷卻液流量控制失效時，機台會被過度冷卻："
                    "主軸與床台熱變形量偏離熱平衡設計點，切削阻力上升 → 電流略升、振動略升，"
                    "溫度則掉到正常帶以下。方向與「冷卻不足」幾乎相反，"
                    "單一原型的餘弦在這種工況下會指向負值，等於整個故障被排除掉。"
                ),
            ),
        ),
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
        alt_signatures=(
            AltSignature(
                name="under_load",
                # 失載：電流大幅下降、轉速衝過額定、溫度略降、振動因失去阻尼而略升。
                deltas={"current": -4.0, "rpm_pct": 6.0, "temperature": -4.0, "vibration": 1.2},
                rationale=(
                    "MAN-A-5.3 的鑑別段落把「主軸動力異常」拆成過載與**失載**兩側："
                    "皮帶斷裂、聯軸器鬆脫、刀具脫落時，馬達失去負載 —— "
                    "電流大幅下降、轉速達成率反而衝過 100%、溫度略降，"
                    "振動則因為旋轉件失去阻尼與殘餘不平衡而略升。"
                    "這與過載是方向相反的兩個簇，正是 AI4I 的 PWF（功率過低**或**過高）"
                    "把單一原型打成 recall 0.388 的同一個結構。"
                ),
            ),
        ),
    ),
}


# 純工安事件：不改變機台感測器，只改變 Camera / 環境觀測。
HAZARD_EVENT_ID = "hazard_zone_intrusion"


def fault_signatures(scales: dict[str, float]) -> list[FaultSignature]:
    """把故障模型轉成 Diagnosis Agent 使用的「正規化指紋」。

    以每個訊號的 scale 正規化，讓不同單位（°C / mm/s / A / %）可以放在同一個向量空間比較。

    `alt_signatures` 走同一條正規化路徑掛成 `FaultSignature.alt_prototypes`；
    沒有宣告 `alt_signatures` 的故障（例如 bearing_degradation）拿到的仍然是
    「只有主原型」的指紋，行為與多原型改動前逐位元相同。
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
                alt_prototypes=tuple(
                    FaultPrototype(
                        name=alt.name,
                        profile={s: d / scales.get(s, 1.0) for s, d in alt.deltas.items()},
                        rationale=alt.rationale,
                    )
                    for alt in model.alt_signatures
                ),
            )
        )
    return sigs


__all__ = ["AltSignature", "FaultModel", "FAULTS", "HAZARD_EVENT_ID", "fault_signatures"]
