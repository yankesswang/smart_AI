"""Demo 情境（規格 §6.2 / §7.1 表格）。

每個情境都帶著 Ground Truth，但它只用於 Benchmark 評分，
執行時不會流進任何 Agent 的輸入。
"""

from __future__ import annotations

from ..domain import FaultInjection, Scenario
from .faults import HAZARD_EVENT_ID

SCENARIOS: dict[str, Scenario] = {
    "bearing-degradation": Scenario(
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
    "cooling-failure": Scenario(
        scenario_id="cooling-failure",
        title="Machine A 冷卻系統失效",
        description="Temperature 大幅上升而 Vibration / Current 幾乎正常，需與軸承劣化區分。",
        injections=(FaultInjection(fault_id="cooling_failure", machine_id="M-A", start_tick=2, ramp_ticks=8),),
        ground_truth={"M-A": "cooling_failure"},
        horizon_ticks=110,
        expected_narrative="正解為冷卻失效；高溫同時可能觸發煙霧偵測與工安規則。",
    ),
    "motor-overload": Scenario(
        scenario_id="motor-overload",
        title="Machine A 主軸馬達過載",
        description="Current 大幅上升且 RPM 明顯下降，Temperature 中度上升。",
        injections=(FaultInjection(fault_id="motor_overload", machine_id="M-A", start_tick=2, ramp_ticks=9),),
        ground_truth={"M-A": "motor_overload"},
        horizon_ticks=110,
        expected_narrative="正解為馬達過載；需電氣技師與較長維修工時。",
    ),
    "hazard-zone": Scenario(
        scenario_id="hazard-zone",
        title="工安：人員進入運轉危險區",
        description="Camera 偵測到人員在 Machine A 運轉中進入危險區且護具不全，Safety Agent 必須成為硬限制。",
        injections=(FaultInjection(fault_id=HAZARD_EVENT_ID, machine_id="M-A", start_tick=3, ramp_ticks=1),),
        ground_truth={"SAFETY": HAZARD_EVENT_ID},
        horizon_ticks=90,
        expected_narrative="沒有設備故障，但任何『維持機台運轉』的方案都必須被 Safety Agent 阻擋。",
    ),
    "bearing-with-intrusion": Scenario(
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
}


def get_scenario(scenario_id: str) -> Scenario:
    if scenario_id not in SCENARIOS:
        raise KeyError(f"未知情境 {scenario_id}；可用：{', '.join(SCENARIOS)}")
    return SCENARIOS[scenario_id]


def list_scenarios() -> list[Scenario]:
    return list(SCENARIOS.values())


__all__ = ["SCENARIOS", "get_scenario", "list_scenarios"]
