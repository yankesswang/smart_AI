"""Demo Equipment Manual / SOP / Maintenance History。

⚠ 全部為競賽用合成資料（Synthetic），不代表任何真實設備商規格或真實維修紀錄。
規格 §7.2：Manual/SOP 3–5 份、Maintenance History 20–50 筆。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ManualDoc:
    ref: str
    title: str
    doc_type: str          # manual / sop
    machine_ids: tuple[str, ...]
    fault_ids: tuple[str, ...]
    body: str
    synthetic: bool = True


@dataclass(frozen=True)
class MaintenanceCase:
    case_id: str
    machine_id: str
    days_ago: int
    symptoms: str
    diagnosed_fault: str
    parts_used: tuple[str, ...]
    repair_min: float
    technician: str
    note: str = ""
    synthetic: bool = True

    def as_text(self) -> str:
        return (
            f"[{self.case_id}] {self.machine_id}｜{self.days_ago} 天前｜徵兆：{self.symptoms}｜"
            f"判定：{self.diagnosed_fault}｜更換零件：{'、'.join(self.parts_used) or '無'}｜"
            f"工時 {self.repair_min:g} 分鐘｜技師 {self.technician}。{self.note}"
        )


# --------------------------------------------------------------------------------------
# Equipment Manual / SOP
# --------------------------------------------------------------------------------------
MANUALS: tuple[ManualDoc, ...] = (
    ManualDoc(
        ref="MAN-GEN-1.1",
        title="加工機台感測器門檻與健康度定義",
        doc_type="manual",
        machine_ids=("M-A", "M-B", "M-C"),
        fault_ids=(),
        body=(
            "本節定義 CNC 加工機台的四項基本監測訊號與判讀區間。\n"
            "Temperature 主軸溫度：正常低於 70°C；70–80°C 為警告；超過 80°C 為危險，"
            "須立即評估降速或停機。\n"
            "Vibration 主軸振動速度 (RMS)：正常低於 4 mm/s；4–7 mm/s 為警告；"
            "超過 7 mm/s 為危險，代表旋轉件已有明顯機械劣化。\n"
            "Current 主軸電流：正常 8–12 A；12–14 A 為警告；超過 14 A 為危險。\n"
            "RPM 轉速達成率：正常為目標轉速的 95–100%；85–95% 為警告；低於 85% 為危險。\n"
            "健康度 Health Score 由上述訊號偏離正常區間的加權程度換算，"
            "權重為 Vibration 0.35、Temperature 0.30、Current 0.20、RPM 0.15。\n"
            "Health 低於 70 應開立預防性維修工單；低於 50 應評估停機。"
        ),
    ),
    ManualDoc(
        ref="MAN-A-3.2",
        title="主軸軸承劣化：徵兆、判讀與處置",
        doc_type="manual",
        machine_ids=("M-A", "M-B"),
        fault_ids=("bearing_degradation",),
        body=(
            "主軸軸承劣化 (Bearing Degradation) 是加工機台最常見的機械性故障。\n"
            "典型徵兆順序為：Vibration 先明顯上升，接著因摩擦生熱使 Temperature 緩慢上升，"
            "Current 因阻力增加而小幅上升，RPM 略微下降但通常仍在 95% 附近。\n"
            "關鍵鑑別點：Vibration 的上升幅度顯著大於 Temperature 的上升幅度；"
            "若 Temperature 大幅上升而 Vibration 幾乎不動，應優先考慮冷卻系統失效而非軸承。\n"
            "風險：軸承持續劣化會導致滾珠剝離、保持器破裂，最終主軸咬死並損傷主軸軸頸，"
            "屆時維修成本與停機時間將是預防性更換的數倍。\n"
            "處置建議：Vibration 進入警告區間即應安排轉單與預防性更換；"
            "進入危險區間（>7 mm/s）不建議繼續全速運轉。"
            "若必須維持產出，僅能以降速（不超過額定轉速 65%）短時間過渡，"
            "且須持續監控 Vibration 是否繼續上升。\n"
            "標準更換工時約 40 分鐘，需 L2 以上機械技師，參照 SOP-MT-07。"
        ),
    ),
    ManualDoc(
        ref="MAN-A-4.1",
        title="冷卻系統失效：徵兆、判讀與處置",
        doc_type="manual",
        machine_ids=("M-A", "M-B"),
        fault_ids=("cooling_failure",),
        body=(
            "冷卻系統失效 (Cooling Failure) 常見原因為冷卻泵故障、濾網堵塞或冷卻液不足。\n"
            "典型徵兆：Temperature 快速且大幅上升，可在十餘分鐘內從正常值升至危險區間；"
            "Vibration 維持正常；Current 幾乎不變或僅微幅上升；RPM 基本不受影響。\n"
            "關鍵鑑別點：溫度單獨異常。若同時看到 Vibration 明顯上升，則不是單純冷卻問題。\n"
            "風險：主軸熱膨脹造成加工精度劣化、刀具異常磨耗；"
            "溫度超過 90°C 有冷卻液氣化與冒煙風險，可能觸發工安事件。\n"
            "處置建議：溫度進入危險區間應立即降速或停機，不可為了產量維持全速。\n"
            "標準處理工時約 30 分鐘，需 L2 以上公用設備技師，參照 SOP-MT-11。"
        ),
    ),
    ManualDoc(
        ref="MAN-A-5.3",
        title="主軸馬達過載：徵兆、判讀與處置",
        doc_type="manual",
        machine_ids=("M-A", "M-B"),
        fault_ids=("motor_overload",),
        body=(
            "主軸馬達過載 (Motor Overload) 常見原因為驅動器異常、進給參數過激、"
            "刀具鈍化造成切削阻力上升，或馬達散熱風扇失效。\n"
            "典型徵兆：Current 大幅上升並可能超過 14 A；RPM 明顯掉落（可能低於 85% 目標值）；"
            "Temperature 中度上升；Vibration 僅輕微上升。\n"
            "關鍵鑑別點：電流上升與轉速下降同時發生，是與軸承劣化最主要的區別；"
            "軸承劣化的 RPM 通常仍維持在 95% 附近。\n"
            "風險：驅動器過熱保護跳脫、馬達繞組絕緣劣化，嚴重時燒毀馬達。\n"
            "處置建議：不可持續全速運轉。應停機檢查驅動器與刀具狀態。\n"
            "標準處理工時約 55 分鐘，需 L3 電氣技師，參照 SOP-MT-04。"
        ),
    ),
    ManualDoc(
        ref="SOP-MT-07",
        title="主軸軸承更換標準作業程序",
        doc_type="sop",
        machine_ids=("M-A", "M-B"),
        fault_ids=("bearing_degradation",),
        body=(
            "步驟 1：於 MES 開立維修工單並取得生產主管核准，確認該機台訂單已轉移或延後。\n"
            "步驟 2：執行 LOTO 上鎖掛牌（Lockout / Tagout），切斷主電源與氣源並掛牌，"
            "由執行技師保管唯一鑰匙。未完成 LOTO 不得進入機台內部。\n"
            "步驟 3：等待主軸溫度降至 40°C 以下始可拆卸，避免燙傷與熱變形。\n"
            "步驟 4：拆卸主軸護罩，取出舊軸承 SPINDLE-BRG-6208，檢查軸頸是否已有磨損。\n"
            "步驟 5：更換新軸承與密封件 BRG-SEAL-KIT，補充 NLGI2 潤滑脂。\n"
            "步驟 6：復位護罩，解除 LOTO，空跑 5 分鐘確認 Vibration 回到 4 mm/s 以下。\n"
            "步驟 7：回填工單，記錄更換零件與實際工時。\n"
            "所需人員：L2 以上機械技師 1 名、協作人員 1 名。標準工時 40 分鐘。"
        ),
    ),
    ManualDoc(
        ref="SOP-MT-11",
        title="冷卻系統檢修標準作業程序",
        doc_type="sop",
        machine_ids=("M-A", "M-B"),
        fault_ids=("cooling_failure",),
        body=(
            "步驟 1：開立維修工單，確認機台已停機或降速。\n"
            "步驟 2：執行 LOTO 上鎖掛牌，並確認冷卻迴路已洩壓。\n"
            "步驟 3：等待主軸溫度降至 45°C 以下，避免冷卻液噴濺燙傷；"
            "若已有冒煙或氣化跡象，須先確認無起火風險並通報工安。\n"
            "步驟 4：檢查冷卻泵 COOLANT-PUMP-CP12 是否運轉、濾網 COOLANT-FILTER 是否堵塞、"
            "冷卻液液位是否低於下限。\n"
            "步驟 5：更換故障件並補充冷卻液，重新啟動冷卻迴路。\n"
            "步驟 6：解除 LOTO，運轉 10 分鐘確認 Temperature 回到 70°C 以下。\n"
            "所需人員：L2 以上公用設備技師 1 名。標準工時 30 分鐘。"
        ),
    ),
    ManualDoc(
        ref="SOP-MT-04",
        title="主軸驅動與馬達電氣檢修標準作業程序",
        doc_type="sop",
        machine_ids=("M-A", "M-B"),
        fault_ids=("motor_overload",),
        body=(
            "步驟 1：開立維修工單並取得生產主管核准，機台必須完全停機，不得以降速方式進行電氣作業。\n"
            "步驟 2：執行 LOTO 上鎖掛牌，並以驗電筆確認無殘電，等待電容放電至少 5 分鐘。\n"
            "步驟 3：檢查伺服驅動器 SERVO-DRV-7K5 的過載紀錄與散熱風扇 MOTOR-FAN 運轉狀況。\n"
            "步驟 4：檢查刀具磨耗與進給參數，排除切削條件造成的假性過載。\n"
            "步驟 5：更換故障件，量測馬達繞組絕緣阻抗，記錄數值。\n"
            "步驟 6：解除 LOTO，空載試車確認 Current 回到 12 A 以下且 RPM 達成率回到 95% 以上。\n"
            "所需人員：L3 電氣技師 1 名。標準工時 55 分鐘。"
        ),
    ),
    ManualDoc(
        ref="SOP-SF-01",
        title="運轉設備危險區域與人員安全規範",
        doc_type="sop",
        machine_ids=("M-A", "M-B", "M-C"),
        fault_ids=(),
        body=(
            "規範 1：機台處於 RUNNING 或 DERATED 狀態時，危險區域（黃線內）禁止任何人員進入。"
            "偵測到人員進入運轉中危險區，系統必須立即發出警示並要求停機，"
            "不得以『維持產量』為理由延後。\n"
            "規範 2：進入作業區必須配戴完整個人防護具（PPE）：安全帽、防護眼鏡、安全鞋。"
            "偵測到 PPE 不完整時，該人員不得進行任何機台作業。\n"
            "規範 3：任何維修作業前必須完成 LOTO 上鎖掛牌，並確認機台為 STOPPED 或 MAINTENANCE 狀態。\n"
            "規範 4：偵測到煙霧、異常高溫（>90°C）或人員跌倒，屬立即停機事件，"
            "任何自動化系統不得覆寫此判定。\n"
            "規範 5：Safety Override（安全覆寫）為系統禁止動作，任何角色皆不得執行。"
        ),
    ),
)

_MANUAL_INDEX = {doc.ref: doc for doc in MANUALS}


def manual_by_ref(ref: str) -> ManualDoc | None:
    return _MANUAL_INDEX.get(ref)


# --------------------------------------------------------------------------------------
# Maintenance History（30 筆合成案例）
# --------------------------------------------------------------------------------------
def _cases() -> tuple[MaintenanceCase, ...]:
    raw = [
        ("MH-001", "M-A", 412, "振動由 2.5 升到 8.1 mm/s，溫度升至 79°C，電流 12.6 A", "bearing_degradation",
         ("SPINDLE-BRG-6208", "GREASE-NLGI2"), 44, "陳志明", "更換後振動回到 2.6 mm/s。"),
        ("MH-002", "M-A", 366, "溫度 10 分鐘內由 63°C 升到 88°C，振動維持 2.6 mm/s", "cooling_failure",
         ("COOLANT-PUMP-CP12",), 33, "林佩芸", "冷卻泵軸封滲漏導致流量不足。"),
        ("MH-003", "M-B", 351, "電流升到 15.2 A，轉速達成率掉到 82%", "motor_overload",
         ("SERVO-DRV-7K5",), 61, "王建良", "驅動器過載保護跳脫，換新後正常。"),
        ("MH-004", "M-A", 330, "振動 7.4 mm/s、溫度 76°C、電流 12.4 A", "bearing_degradation",
         ("SPINDLE-BRG-6208", "BRG-SEAL-KIT"), 41, "陳志明", "軸頸未見磨損，屬早期發現。"),
        ("MH-005", "M-A", 305, "溫度 91°C 並出現冷卻液氣化冒煙，振動正常", "cooling_failure",
         ("COOLANT-FILTER", "COOLANT-45L"), 36, "林佩芸", "濾網嚴重堵塞，已同步通報工安。"),
        ("MH-006", "M-B", 288, "振動 6.9 mm/s、溫度 74°C", "bearing_degradation",
         ("SPINDLE-BRG-6208",), 47, "張美惠", "B 機台軸承壽命偏短，已調整潤滑週期。"),
        ("MH-007", "M-A", 270, "電流 14.8 A、RPM 達成率 80%、溫度 75°C", "motor_overload",
         ("MOTOR-FAN", "SERVO-DRV-7K5"), 58, "王建良", "散熱風扇卡死造成驅動器過熱。"),
        ("MH-008", "M-A", 251, "振動 9.2 mm/s 未即時處理，主軸異音", "bearing_degradation",
         ("SPINDLE-BRG-6208", "SPINDLE-SHAFT"), 186, "陳志明", "延遲處理造成主軸軸頸損傷，工時大幅增加。"),
        ("MH-009", "M-C", 240, "包裝機馬達電流 13.9 A、產速下降", "motor_overload",
         ("POWER-CONTACTOR",), 42, "王建良", "接觸器接點氧化。"),
        ("MH-010", "M-A", 226, "溫度 84°C、振動 2.8 mm/s、電流 10.4 A", "cooling_failure",
         ("COOLANT-PUMP-CP12", "COOLANT-45L"), 31, "林佩芸", "典型的溫度單獨異常。"),
        ("MH-011", "M-B", 210, "振動 5.8 mm/s 緩慢上升三天", "bearing_degradation",
         ("SPINDLE-BRG-6208", "GREASE-NLGI2"), 40, "張美惠", "趨勢監控提早三天發現。"),
        ("MH-012", "M-A", 195, "電流 15.6 A、RPM 78%", "motor_overload",
         ("SERVO-DRV-7K5",), 55, "王建良", ""),
        ("MH-013", "M-A", 181, "振動 7.8 mm/s、溫度 78°C、電流 12.9 A", "bearing_degradation",
         ("SPINDLE-BRG-6208", "BRG-SEAL-KIT", "GREASE-NLGI2"), 43, "陳志明", ""),
        ("MH-014", "M-A", 166, "溫度 86°C，冷卻液液位低於下限", "cooling_failure",
         ("COOLANT-45L",), 22, "林佩芸", "僅補充冷卻液即恢復。"),
        ("MH-015", "M-B", 152, "振動 6.4 mm/s、溫度 73°C", "bearing_degradation",
         ("SPINDLE-BRG-6208",), 45, "張美惠", ""),
        ("MH-016", "M-C", 141, "包裝機溫度 72°C、電流 11.8 A", "cooling_failure",
         ("COOLANT-FILTER",), 26, "林佩芸", ""),
        ("MH-017", "M-A", 128, "振動 8.4 mm/s、溫度 80°C、電流 13.1 A、RPM 94%", "bearing_degradation",
         ("SPINDLE-BRG-6208", "GREASE-NLGI2"), 46, "陳志明", "已進入危險區間才處理，產能損失較大。"),
        ("MH-018", "M-A", 117, "電流 14.2 A、RPM 84%、刀具異常磨耗", "motor_overload",
         ("SERVO-DRV-7K5", "MOTOR-FAN"), 57, "王建良", "同時調整進給參數。"),
        ("MH-019", "M-B", 104, "溫度 83°C、振動正常", "cooling_failure",
         ("COOLANT-PUMP-CP12",), 34, "林佩芸", ""),
        ("MH-020", "M-A", 96, "振動 5.1 mm/s、溫度 71°C", "bearing_degradation",
         ("GREASE-NLGI2",), 18, "陳志明", "僅補充潤滑脂即改善，屬極早期。"),
        ("MH-021", "M-A", 85, "溫度 89°C、電流 10.8 A、振動 2.7 mm/s", "cooling_failure",
         ("COOLANT-PUMP-CP12", "COOLANT-FILTER"), 35, "林佩芸", ""),
        ("MH-022", "M-B", 74, "電流 15.0 A、RPM 81%", "motor_overload",
         ("SERVO-DRV-7K5",), 60, "王建良", ""),
        ("MH-023", "M-A", 63, "振動 7.1 mm/s、溫度 77°C、電流 12.5 A", "bearing_degradation",
         ("SPINDLE-BRG-6208", "BRG-SEAL-KIT"), 42, "陳志明", "轉單至 Machine B 後維修，訂單未延誤。"),
        ("MH-024", "M-C", 55, "包裝佇列堆積、產速下降 30%", "motor_overload",
         ("POWER-CONTACTOR", "MOTOR-FAN"), 39, "王建良", ""),
        ("MH-025", "M-A", 44, "溫度 82°C、振動 2.9 mm/s、電流 10.6 A", "cooling_failure",
         ("COOLANT-FILTER", "COOLANT-45L"), 29, "林佩芸", ""),
        ("MH-026", "M-B", 36, "振動 6.7 mm/s、溫度 75°C、電流 12.2 A", "bearing_degradation",
         ("SPINDLE-BRG-6208", "GREASE-NLGI2"), 44, "張美惠", ""),
        ("MH-027", "M-A", 27, "電流 14.6 A、RPM 82%、溫度 76°C", "motor_overload",
         ("SERVO-DRV-7K5", "MOTOR-FAN"), 56, "王建良", ""),
        ("MH-028", "M-A", 19, "振動 4.6 mm/s 持續上升、溫度 71°C", "bearing_degradation",
         ("GREASE-NLGI2", "BRG-SEAL-KIT"), 25, "陳志明", "趨勢告警提早發現，僅需潤滑與密封件更換。"),
        ("MH-029", "M-B", 11, "溫度 85°C、振動 2.5 mm/s", "cooling_failure",
         ("COOLANT-PUMP-CP12",), 32, "林佩芸", ""),
        ("MH-030", "M-A", 5, "振動 5.4 mm/s、溫度 72°C、電流 11.6 A", "bearing_degradation",
         ("SPINDLE-BRG-6208",), 41, "陳志明", "上一次預防性更換，距今 5 天。"),
    ]
    return tuple(
        MaintenanceCase(
            case_id=c[0], machine_id=c[1], days_ago=c[2], symptoms=c[3], diagnosed_fault=c[4],
            parts_used=c[5], repair_min=float(c[6]), technician=c[7], note=c[8],
        )
        for c in raw
    )


MAINTENANCE_HISTORY: tuple[MaintenanceCase, ...] = _cases()


__all__ = ["ManualDoc", "MaintenanceCase", "MANUALS", "MAINTENANCE_HISTORY", "manual_by_ref"]
