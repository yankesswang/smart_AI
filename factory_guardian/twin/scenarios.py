"""Demo 情境（規格 §6.2 / §7.1 表格）。

每個情境都帶著 Ground Truth，但它只用於 Benchmark 評分，
執行時不會流進任何 Agent 的輸入。

情境分三類，理由寫在各自的區塊：

1. **設備故障情境** —— 徵兆與手冊一致，考的是完整閉環的處置品質。
2. **無故障干擾情境**（`fp-` 開頭）—— Ground Truth 是「無故障」。
   沒有這一類，False Positive 這格 KPI 就永遠是空的：不是零誤報，是沒有機會誤報。
3. **現實落差情境** —— 故障是真的，但這一台機器的徵兆和手冊寫的不一樣，
   或某個感測器本身有問題。考的是「診斷錯了之後，系統會不會自己發現」。

第 2、3 類的參數（注入幅度、時間、落差倍率）逐項說明在 `docs/factory_guardian/benchmark_notes.md`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain import FaultInjection, Scenario
from .disturbances import Disturbance, RealityGap, SensorFault, SignatureMismatch
from .faults import HAZARD_EVENT_ID


@dataclass(frozen=True)
class FieldScenario(Scenario):
    """帶「現場條件」的情境：干擾與現實落差。

    為什麼是子類別而不是直接改 `domain.Scenario`：
    `Scenario` 是所有 Agent、API 與前端共用的語彙，而干擾與落差是**孿生體內部**的設定，
    Agent 永遠看不到它們。把它們放進共用的領域模型，等於在 Agent 讀得到的地方
    多開一個描述「答案」的欄位 —— 就算現在沒人讀，那條紅線也已經破了。
    """

    disturbances: tuple[Disturbance, ...] = ()
    reality_gap: RealityGap | None = None

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        # 只輸出「有幾項干擾」這種形狀資訊，不輸出參數 ——
        # 這個 to_dict 會被 /api/scenarios 送到前端，而前端是 Agent 之外的人在看，
        # 但同一份 JSON 也可能被拿去餵給 LLM 敘述器，所以維持一樣的保守標準。
        data["disturbance_count"] = len(self.disturbances)
        data["has_reality_gap"] = self.reality_gap is not None
        return data


def disturbances_of(scenario: Scenario) -> tuple[Disturbance, ...]:
    return tuple(getattr(scenario, "disturbances", ()) or ())


def reality_gap_of(scenario: Scenario) -> RealityGap | None:
    return getattr(scenario, "reality_gap", None)


SCENARIOS: dict[str, Scenario] = {
    # =================================================================================
    # 1. 設備故障情境
    # =================================================================================
    "bearing-degradation": FieldScenario(
        scenario_id="bearing-degradation",
        title="Machine A 軸承劣化",
        description=(
            "決賽主場景。Machine A 主軸軸承逐步劣化：Vibration 明顯上升、Temperature 隨之上升、"
            "Current 小幅上升。訂單 ORD-A001（P1、交期 165 分鐘）原本排在 Machine A。"
        ),
        injections=(FaultInjection(fault_id="bearing_degradation", machine_id="M-A", start_tick=2, ramp_ticks=10),),
        ground_truth={"M-A": "bearing_degradation"},
        horizon_ticks=110,
        expected_narrative="正解為軸承劣化；正確處置為轉單至 Machine B 並對 A 進行維修。",
    ),
    "cooling-failure": FieldScenario(
        scenario_id="cooling-failure",
        title="Machine A 冷卻系統失效",
        description="Temperature 大幅上升而 Vibration / Current 幾乎正常，需與軸承劣化區分。",
        injections=(FaultInjection(fault_id="cooling_failure", machine_id="M-A", start_tick=2, ramp_ticks=8),),
        ground_truth={"M-A": "cooling_failure"},
        horizon_ticks=110,
        expected_narrative="正解為冷卻失效；高溫同時可能觸發煙霧偵測與工安規則。",
    ),
    "motor-overload": FieldScenario(
        scenario_id="motor-overload",
        title="Machine A 主軸馬達過載",
        description="Current 大幅上升且 RPM 明顯下降，Temperature 中度上升。",
        injections=(FaultInjection(fault_id="motor_overload", machine_id="M-A", start_tick=2, ramp_ticks=9),),
        ground_truth={"M-A": "motor_overload"},
        horizon_ticks=110,
        expected_narrative="正解為馬達過載；需電氣技師與較長維修工時。",
    ),
    "hazard-zone": FieldScenario(
        scenario_id="hazard-zone",
        title="工安：人員進入運轉危險區",
        description="Camera 偵測到人員在 Machine A 運轉中進入危險區且護具不全，Safety Agent 必須成為硬限制。",
        injections=(FaultInjection(fault_id=HAZARD_EVENT_ID, machine_id="M-A", start_tick=3, ramp_ticks=1),),
        ground_truth={"SAFETY": HAZARD_EVENT_ID},
        horizon_ticks=90,
        expected_narrative="沒有設備故障，但任何『維持機台運轉』的方案都必須被 Safety Agent 阻擋。",
    ),
    "bearing-with-intrusion": FieldScenario(
        scenario_id="bearing-with-intrusion",
        title="複合情境：軸承劣化 + 人員闖入",
        description="設備劣化與工安事件同時發生，用來展示 Safety Agent 對生產導向方案的否決權。",
        injections=(
            FaultInjection(fault_id="bearing_degradation", machine_id="M-A", start_tick=2, ramp_ticks=10),
            FaultInjection(fault_id=HAZARD_EVENT_ID, machine_id="M-A", start_tick=6, ramp_ticks=1),
        ),
        ground_truth={"M-A": "bearing_degradation", "SAFETY": HAZARD_EVENT_ID},
        horizon_ticks=110,
        expected_narrative="正解為軸承劣化，且因人員闖入，所有讓 A 繼續運轉的方案都必須 BLOCK。",
    ),

    # =================================================================================
    # 2. 無故障干擾情境（False Positive）
    #
    # 這四個情境的 Ground Truth 都是空的 —— 機台完全健康。
    # 所以任何在這裡觸發的告警都是誤報，任何在這裡動到設備的處置都是誤動作。
    # 幅度的選法：讓「固定門檻」這個判準真的會踩線，否則量到的只是「沒事發生」。
    # =================================================================================
    "fp-sensor-spike": FieldScenario(
        scenario_id="fp-sensor-spike",
        title="干擾：振動感測器瞬間尖峰（無故障）",
        description=(
            "M-A 振動感測器出現兩次單一取樣的尖峰（接點抖動／電磁干擾），機台物理狀態完全正常。"
            "固定門檻打在原始讀值上，一個取樣就足以觸發危險告警。"
        ),
        injections=(),
        ground_truth={},
        horizon_ticks=75,
        expected_narrative="無故障。單點尖峰不應觸發任何設備處置。",
        disturbances=(
            Disturbance(
                machine_id="M-A", label="vibration-spike-1",
                deltas={"vibration": 6.8}, start_tick=12,
                ramp_ticks=1, hold_ticks=0, decay_ticks=1, sensor_only=True,
                note="單一取樣尖峰：只有感測器讀值跳動，clean_signals 不動。",
            ),
            Disturbance(
                machine_id="M-A", label="vibration-spike-2",
                deltas={"vibration": 6.2}, start_tick=34,
                ramp_ticks=1, hold_ticks=0, decay_ticks=1, sensor_only=True,
                note="同一支感測器第二次尖峰。",
            ),
        ),
    ),
    "fp-restart-transient": FieldScenario(
        scenario_id="fp-restart-transient",
        title="干擾：換料暫停後重啟暫態（無故障）",
        description=(
            "M-A 換料後重啟：主軸啟動電流突波、轉速尚未跟上、機構短暫振動。"
            "兩三分鐘後回到正常工作點，機台沒有任何劣化。"
        ),
        injections=(),
        ground_truth={},
        horizon_ticks=75,
        expected_narrative="無故障。啟動暫態不應被當成馬達過載處置。",
        disturbances=(
            Disturbance(
                machine_id="M-A", label="restart-inrush",
                deltas={"current": 6.0, "vibration": 3.0, "rpm_pct": -12.0, "temperature": -3.0},
                start_tick=14, ramp_ticks=1, hold_ticks=0, decay_ticks=1,
                note="啟動突波：一個取樣週期內電流踩過危險門檻，隨即回落。",
            ),
        ),
    ),
    "fp-load-step": FieldScenario(
        scenario_id="fp-load-step",
        title="干擾：換規格後負載提高（無故障）",
        description=(
            "M-A 換到高硬度材料的加工程式，切削負載提高：電流穩定上升到新的工作點並停在那裡，"
            "溫度隨之上升。機台完全健康，只是這一段時間本來就該用更大的力。"
        ),
        injections=(),
        ground_truth={},
        horizon_ticks=75,
        expected_narrative="無故障。訊號停在新的穩態，不是進行式劣化，不應停機。",
        disturbances=(
            Disturbance(
                machine_id="M-A", label="load-step-118pct",
                deltas={"current": 4.2, "temperature": 7.0, "vibration": 0.5, "rpm_pct": -1.0},
                start_tick=16, ramp_ticks=3, hold_ticks=0, decay_ticks=0,
                note="負載切換後維持到情境結束（decay_ticks=0 = 不回復）。",
            ),
        ),
    ),
    "fp-warm-up": FieldScenario(
        scenario_id="fp-warm-up",
        title="干擾：冷機暖機溫升過衝（無故障）",
        description=(
            "M-A 冷機啟動後主軸溫度快速爬升並過衝，之後隨潤滑循環穩定回落到正常工作溫度。"
            "峰值確實踩進危險帶 —— 門檻沒有錯，只是那不是故障。"
        ),
        injections=(),
        ground_truth={},
        horizon_ticks=75,
        expected_narrative="無故障。溫度過衝後回落，趨勢反轉本身就是證據。",
        disturbances=(
            Disturbance(
                machine_id="M-A", label="warm-up-overshoot",
                deltas={"temperature": 26.0},
                start_tick=10, ramp_ticks=4, hold_ticks=3, decay_ticks=8, residual=0.42,
                note="過衝後回落到 62 + 26×0.42 ≈ 73°C 的正常工作溫度。",
            ),
        ),
    ),

    # =================================================================================
    # 3. 現實落差情境
    #
    # 故障是真的，但手冊描述的是「典型機台」，這一台不是。
    # 這是為了拆掉一個循環論證：指紋知識與 Simulator 的物理來自同一份 deltas，
    # 診斷正確率因此必然 100%。落差一旦存在，正確率就會掉下來 —— 那才是真的量測。
    # =================================================================================
    "bearing-atypical": FieldScenario(
        scenario_id="bearing-atypical",
        title="現實落差：非典型軸承劣化（徵兆偏離手冊）",
        description=(
            "M-A 的軸承劣化是真的，但這一台去年換過主軸座與避震座，"
            "同樣的劣化在它身上振動只表現出手冊典型值的三成，溫升卻高出四成五，"
            "而且劣化速度比手冊快 1.8 倍。手冊指紋因此指向冷卻系統，而不是軸承。"
        ),
        injections=(FaultInjection(fault_id="bearing_degradation", machine_id="M-A", start_tick=2, ramp_ticks=10),),
        ground_truth={"M-A": "bearing_degradation"},
        horizon_ticks=110,
        expected_narrative=(
            "正解仍是軸承劣化，但診斷會先指向冷卻失效；"
            "依冷卻工時開出的維修不足以修好軸承，必須由驗證抓出來並重新規劃。"
        ),
        reality_gap=RealityGap(
            signature_mismatch=(
                SignatureMismatch(
                    machine_id="M-A", fault_id="bearing_degradation",
                    scale={"vibration": 0.15, "temperature": 1.60, "current": 0.90, "rpm_pct": 0.60},
                    escalation_gain=1.8,
                    note="避震座吸收大部分振動；潤滑脂配方不同使溫升更明顯；此台劣化速度較快。",
                ),
            ),
            note="手冊寫的是典型機台，現場這一台不是 —— 沒有人事先知道差多少。",
        ),
    ),
    "cooling-with-stuck-vibration": FieldScenario(
        scenario_id="cooling-with-stuck-vibration",
        title="現實落差：冷卻失效 + 振動感測器卡值",
        description=(
            "M-A 冷卻系統失效，同時振動感測器卡在上次維修後的殘值 3.8 mm/s（在警戒帶之下，不會自己告警）。"
            "Agent 看到『溫度上升 + 振動偏高』，早期最像的候選是軸承劣化。"
        ),
        injections=(FaultInjection(fault_id="cooling_failure", machine_id="M-A", start_tick=2, ramp_ticks=8),),
        ground_truth={"M-A": "cooling_failure"},
        horizon_ticks=110,
        expected_narrative="正解為冷卻失效；卡值的振動讀值會把早期診斷推向軸承劣化。",
        reality_gap=RealityGap(
            sensor_faults=(
                SensorFault(
                    machine_id="M-A", signal="vibration", mode="stuck",
                    start_tick=0, stuck_value=3.8,
                    note="感測器卡在一個曾經合理的數字上 —— 這正是卡值最難發現的地方。",
                ),
            ),
            note="機台的振動其實正常；不正常的是那支感測器。",
        ),
    ),
}

# 無故障情境（Ground Truth 為空）—— False Positive KPI 只在這一組上有意義。
FALSE_POSITIVE_SCENARIOS: tuple[str, ...] = tuple(
    sid for sid, s in SCENARIOS.items() if not s.ground_truth
)
# 設備故障情境（不含純工安）—— 診斷正確率只在這一組上有意義。
EQUIPMENT_SCENARIOS: tuple[str, ...] = tuple(
    sid for sid, s in SCENARIOS.items() if any(k != "SAFETY" for k in s.ground_truth)
)
# 帶現實落差的情境。
REALITY_GAP_SCENARIOS: tuple[str, ...] = tuple(
    sid for sid, s in SCENARIOS.items() if reality_gap_of(s) is not None
)


def get_scenario(scenario_id: str) -> Scenario:
    if scenario_id not in SCENARIOS:
        raise KeyError(f"未知情境 {scenario_id}；可用：{', '.join(SCENARIOS)}")
    return SCENARIOS[scenario_id]


def list_scenarios() -> list[Scenario]:
    return list(SCENARIOS.values())


__all__ = [
    "SCENARIOS",
    "EQUIPMENT_SCENARIOS",
    "FALSE_POSITIVE_SCENARIOS",
    "REALITY_GAP_SCENARIOS",
    "FieldScenario",
    "disturbances_of",
    "reality_gap_of",
    "get_scenario",
    "list_scenarios",
]
