"""商業案例的假設參數 —— 全部集中在這裡，全部有名字，全部標明依據。

寫這個檔案的規則只有一條，和 ``deployment/budget.py`` 一樣：
**每個數字都要能被評審追問，而且追問得到答案。**

所以每一筆都帶 ``basis``，只有三種值：

* :attr:`Basis.MEASURED` —— 由本 repo 的 Digital Twin 實際跑出來（``run_benchmark()`` 的 KPI）。
* :attr:`Basis.DERIVED` —— 由實測值與假設參數，用寫出來的公式推導。
* :attr:`Basis.ASSUMPTION` —— 工程／商業假設。**沒有可引用的公開統計時就誠實寫「假設」**，
  並在 ``rationale`` 裡寫出量級是怎麼估的，讓人可以直接質疑那個推估過程。

沒有第四種。特別是**沒有「業界平均」這一種** —— 我們沒有取得任何工廠的真實統計，
所以不會有任何一個數字被包裝成產業數據。

> **SYNTHETIC DEMO DATA** —— 本模組所有金額假設皆為競賽用估算值，不代表任何真實工廠的財務資料。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..knowledge.corpus import MAINTENANCE_HISTORY
from ..twin.energy import (
    ELECTRICITY_TARIFF_NTD_PER_KWH,
    ELECTRICITY_TARIFF_SOURCE,
    GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH,
    GRID_EMISSION_FACTOR_SOURCE,
)
from ..twin.faults import FAULTS
from ..twin.topology import UNIT_MARGIN_NTD


class Basis(str, Enum):
    """一個數字的來源類別。"""

    MEASURED = "measured"
    DERIVED = "derived"
    ASSUMPTION = "assumption"


BASIS_LABEL: dict[str, str] = {
    Basis.MEASURED.value: "實測",
    Basis.DERIVED.value: "推導",
    Basis.ASSUMPTION.value: "假設",
}


@dataclass(frozen=True)
class Assumption:
    """一個具名假設參數。"""

    key: str
    label: str
    value: float
    unit: str
    basis: Basis
    source: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": round(self.value, 4),
            "unit": self.unit,
            "basis": self.basis.value,
            "basis_label": BASIS_LABEL[self.basis.value],
            "source": self.source,
            "rationale": self.rationale,
        }


# ======================================================================================
# 一、事件頻率 —— 「一年會發生幾次」是把 110 分鐘的實測換算成年度效益的唯一橋樑，
#     所以它是整份商業案例最該被追問的地方，放在最前面。
# ======================================================================================

#: 合成維修語料的時間跨度（天）與筆數。
#: 這兩個數字直接從 ``knowledge/corpus.py`` 的 30 筆維修紀錄讀出來，不是另外估的。
MAINTENANCE_CORPUS_SPAN_DAYS: float = float(max(c.days_ago for c in MAINTENANCE_HISTORY))
MAINTENANCE_CORPUS_CASES: int = len(MAINTENANCE_HISTORY)

#: 由語料推導的「每年設備異常處置事件數／每條產線」。
#: 30 筆 ÷ (412 天 ÷ 365.25) ≈ 26.6 次/年。
ANNUAL_MAINTENANCE_EVENTS_PER_LINE: float = (
    MAINTENANCE_CORPUS_CASES / (MAINTENANCE_CORPUS_SPAN_DAYS / 365.25)
)

#: 上述事件中，會走到「本 Demo 量測到的那種產能衝擊」的比例。
#: 語料裡有相當比例的備註是「僅補充潤滑脂即改善，屬極早期」「趨勢監控提早三天發現」，
#: 這些在現行流程下就已經被接住，不會產生我們量到的損失。保守取一半。
PRODUCTION_IMPACTING_EVENT_SHARE: float = 0.5

#: 故障類型分布：直接數 ``MAINTENANCE_HISTORY`` 裡各 ``diagnosed_fault`` 的筆數。
#: 這是「從 Demo 語料推導」，不是產業統計 —— 語料本身是合成資料。
def _fault_mix() -> dict[str, float]:
    counts: dict[str, int] = {fid: 0 for fid in FAULTS}
    for case in MAINTENANCE_HISTORY:
        if case.diagnosed_fault in counts:
            counts[case.diagnosed_fault] += 1
    total = sum(counts.values())
    if total == 0:
        # 語料被抽換成完全不同的故障集合時，退回均分而不是拋例外。
        return {fid: 1.0 / len(FAULTS) for fid in FAULTS} if FAULTS else {}
    return {fid: n / total for fid, n in counts.items()}


FAULT_MIX: dict[str, float] = _fault_mix()

#: 設備異常事件中，同時有人員進入運轉危險區的比例（複合情境 ``bearing-with-intrusion``）。
#: 這個比例是用來**從設備事件總數裡切出去**的，不是額外加上去 —— 避免與單純設備情境重複計算。
SIMULTANEOUS_INTRUSION_RATE: float = 0.05

#: 每條產線每年「人員未依規進入運轉中危險區」的虞慮事件次數。
#: 純假設：以單線每月 1 次估算。工安虞慮事件的真實頻率高度依賴廠內管理成熟度，
#: 我們沒有任何一間工廠的紀錄可以引用，所以不假裝有。
ANNUAL_HAZARD_INTRUSION_EVENTS_PER_LINE: float = 12.0

#: 一次典型闖入事件的滯留時間（分鐘）。
#: ``hazard-zone`` 情境注入的是「人員全程滯留 90 分鐘」的壓力測試，
#: 那是為了驗證 Safety Agent 的硬限制行為，不是典型事件長度。
#: 直接年化會同時高估產能代價與高估工安效益，所以用這個參數把量測值等比縮回。
TYPICAL_INTRUSION_MIN: float = 6.0


# ======================================================================================
# 二、現況（incumbent）—— ROI 完全取決於「你拿什麼跟我比」，所以這個假設必須攤開。
# ======================================================================================

#: 現行流程中，告警發出後**沒有被及時接住**、放任劣化走到二次損壞的比例。
#:
#: Benchmark 的兩組對照組都是理想化的極端：
#:   * Baseline A（固定門檻告警、之後什麼都不做）＝ 完全沒接住 → 二次損壞、健康度掉到 0.7。
#:   * Baseline B（偵測即停機、技師立刻進場）＝ 完全接住，而且技師零等待。
#: 真實工廠兩者都不是。我們用一個機率混合來代表現況：
#:   ``現況 KPI = 逃逸率 × Baseline A + (1 − 逃逸率) × Baseline B``
#: 這是兩個**各自自洽**的真實系統的機率混合，不是逐 KPI 挑最好值拼出來的、
#: 物理上不可能存在的「超級對照組」。
#:
#: 0.25 是保守假設：四次告警有一次因為值班人員未及時判讀、或被當成誤報而錯過。
#: **這是整份 ROI 最敏感的單一參數**，破口分析會直接給出它的臨界值。
INCUMBENT_ESCALATION_RATE: float = 0.25

#: 效益實現率：實驗室 KPI 不會 100% 在真實工廠複現。
#: 涵蓋感測器安裝品質、門檻校準期、人員採用率、以及模型在真實訊號上的退化。
BENEFIT_REALIZATION_RATE: float = 0.80


# ======================================================================================
# 三、單價與換算係數
# ======================================================================================

#: 維修技師的全負擔人力成本（NTD/小時）。
#: 推估過程：年薪 60–80 萬 ＋ 勞健保、退休金與管理費用 ≈ 全負擔 100 萬/年，
#: 除以年有效工時 2,000 小時 ≈ 500 NTD/hr。
MAINTENANCE_TECHNICIAN_HOURLY_NTD: float = 500.0

#: 現行流程下「從告警到確認根因」需要的人工工時（分鐘）。
#: 推估過程：技師到場 15 分鐘 ＋ 現場量測與觀察 30 分鐘 ＋ 查手冊／翻歷史工單 30 分鐘
#: ＋ 與生產單位確認影響 15 分鐘 ≈ 90 分鐘。
MANUAL_ROOT_CAUSE_MIN: float = 90.0

#: 現行流程下人工開立的工單完整度（%）。
#: 推估過程：現行工單通常有故障描述與機台，但缺少可引用的手冊條款、
#: 建議零件清單與 SOP 連結；以 ``WorkOrder.completeness()`` 的欄位口徑估約六成。
MANUAL_WORK_ORDER_COMPLETENESS_PCT: float = 60.0

#: 工單資訊不足時，技師到現場才發現缺料而多跑的一趟（小時）。
WORK_ORDER_REWORK_TRIP_HOURS: float = 0.5

#: 交期延遲罰則（NTD / 分鐘 / 張訂單）。
#: 推估過程：代工合約常見延遲罰則約訂單金額 0.5%/日；
#: 一張 240 件的訂單若售價為邊際貢獻的 3 倍（185 × 3 ≈ 555 元/件）→ 訂單金額約 13.3 萬，
#: 0.5%/日 ≈ 666 元/日 ≈ 0.46 元/分鐘。取 0.5 元/分鐘。
ORDER_DELAY_PENALTY_NTD_PER_MIN: float = 0.5

#: 每張延遲訂單觸發的趕工成本（NTD）。
#: 推估過程：補一個班別的加班補產，2 名作業員 × 8 小時 × 250 元/hr × 1.34 倍加班費 ≈ 5,360 元。
EXPEDITE_COST_PER_LATE_ORDER_NTD: float = 5_000.0

#: 一次可記錄工安事故的總期望成本（NTD）。
#: 推估過程：醫療與賠償 30 萬 ＋ 停工調查（產線停 8 小時 × 每小時邊際貢獻 22,200 元 ≈ 17.8 萬）
#: ＋ 主管機關檢查、改善與教育訓練工時 10 萬 ≈ 58 萬。保守下修取 50 萬。
#: 這個估值刻意**不含**重大職災（失能或死亡）情境 —— 那會讓數字瞬間變好看，
#: 但也會讓整份 ROI 變成靠一個極端假設撐起來的。
SAFETY_INCIDENT_COST_NTD: float = 500_000.0

#: 人員在運轉中危險區每曝露 1 小時，發生可記錄工傷的機率。
#:
#: **這一項沒有任何公開統計可以引用，是純假設。**
#: 我們不去把它調到讓 ROI 好看的位置，而是在破口分析裡直接給出臨界值，
#: 讓評審自己判斷這個機率合不合理。
HAZARD_INJURY_PROBABILITY_PER_EXPOSURE_HOUR: float = 0.005

#: 碳費費率（NTD / 公噸 CO2e）。
#: 環境部公告之碳費一般費率為每公噸 300 元（2025 年起計費）。費率本身是公開值，
#: **但適用性是假設**：碳費徵收對象為年排放量達 2.5 萬公噸 CO2e 以上的排放源，
#: 單一產線規模通常未達門檻。因此在本案中它代表的是**內部碳定價／碳權的機會成本**，
#: 而不是必然發生的現金支出。
CARBON_FEE_NTD_PER_TONNE: float = 300.0

#: 折現率（用於三年 NPV）。中小型製造業的資金成本量級假設。
DISCOUNT_RATE: float = 0.08

#: 故障模型的二次損壞成本（NTD），直接讀 ``twin/faults.py`` 既有常數。
DEFAULT_SECONDARY_DAMAGE_COST_NTD: float = 180_000.0


def secondary_damage_cost(fault_id: str | None) -> float:
    """該故障若發展到二次損壞的維修成本（NTD）。來源是 ``twin/faults.py`` 的既有常數。"""
    model = FAULTS.get(fault_id) if fault_id else None
    return float(getattr(model, "secondary_damage_cost_ntd", DEFAULT_SECONDARY_DAMAGE_COST_NTD))


# ======================================================================================
# 假設登錄表 —— CLI 與報表直接印這張表，評審一眼看到我們在假設什麼。
# ======================================================================================
def _fault_mix_text() -> str:
    return "、".join(f"{fid} {pct:.0%}" for fid, pct in sorted(FAULT_MIX.items(), key=lambda kv: -kv[1]))


ASSUMPTIONS: dict[str, Assumption] = {
    a.key: a
    for a in (
        Assumption(
            "maintenance_corpus_cases", "維修語料筆數", MAINTENANCE_CORPUS_CASES, "筆",
            Basis.MEASURED, "factory_guardian/knowledge/corpus.py::MAINTENANCE_HISTORY",
            "直接數語料筆數，不是估的。語料本身是合成資料並已明確標示。",
        ),
        Assumption(
            "maintenance_corpus_span_days", "維修語料時間跨度", MAINTENANCE_CORPUS_SPAN_DAYS, "天",
            Basis.MEASURED, "corpus.py 中 MaintenanceCase.days_ago 的最大值",
            "語料最舊一筆距今 412 天。",
        ),
        Assumption(
            "annual_maintenance_events_per_line", "每線每年設備異常事件數",
            ANNUAL_MAINTENANCE_EVENTS_PER_LINE, "次/年/線",
            Basis.DERIVED, "語料筆數 ÷ (跨度天數 ÷ 365.25)",
            f"{MAINTENANCE_CORPUS_CASES} ÷ ({MAINTENANCE_CORPUS_SPAN_DAYS:.0f}/365.25) "
            f"= {ANNUAL_MAINTENANCE_EVENTS_PER_LINE:.1f}。這是從 Demo 語料推導，不是產業統計。",
        ),
        Assumption(
            "production_impacting_event_share", "會造成產能衝擊的事件比例",
            PRODUCTION_IMPACTING_EVENT_SHARE, "比例",
            Basis.ASSUMPTION, "保守假設",
            "語料中有相當比例備註為『極早期』『趨勢監控提早三天發現』，"
            "這些在現行流程已被接住。保守取一半。",
        ),
        Assumption(
            "fault_mix", "故障類型分布（最高占比）", max(FAULT_MIX.values(), default=0.0), "比例",
            Basis.DERIVED, "corpus.py 中 diagnosed_fault 的筆數占比",
            f"實際分布：{_fault_mix_text()}。",
        ),
        Assumption(
            "simultaneous_intrusion_rate", "設備事件同時發生人員闖入的比例",
            SIMULTANEOUS_INTRUSION_RATE, "比例",
            Basis.ASSUMPTION, "保守假設",
            "複合情境的次數是**從設備事件總數裡切出去**的，不是額外加上去，避免重複計算。",
        ),
        Assumption(
            "annual_hazard_events_per_line", "每線每年工安虞慮事件數",
            ANNUAL_HAZARD_INTRUSION_EVENTS_PER_LINE, "次/年/線",
            Basis.ASSUMPTION, "保守假設（單線每月 1 次）",
            "真實頻率高度依賴廠內管理成熟度，我們沒有任何工廠紀錄可引用。",
        ),
        Assumption(
            "typical_intrusion_min", "一次闖入事件的典型滯留時間", TYPICAL_INTRUSION_MIN, "分鐘",
            Basis.ASSUMPTION, "保守假設",
            "hazard-zone 情境注入的是『全程滯留 90 分鐘』的壓力測試，"
            "直接年化會同時高估產能代價與工安效益，故等比縮回。",
        ),
        Assumption(
            "incumbent_escalation_rate", "現況告警逃逸率（走到二次損壞的比例）",
            INCUMBENT_ESCALATION_RATE, "比例",
            Basis.ASSUMPTION, "保守假設；本案最敏感的單一參數",
            "現況 KPI = 逃逸率 × Baseline A + (1−逃逸率) × Baseline B。"
            "兩個各自自洽的真實系統的機率混合，不是逐 KPI 挑最好值拼出來的假想對照組。",
        ),
        Assumption(
            "benefit_realization_rate", "效益實現率", BENEFIT_REALIZATION_RATE, "比例",
            Basis.ASSUMPTION, "保守假設",
            "涵蓋感測器安裝品質、門檻校準期、人員採用率與模型在真實訊號上的退化。",
        ),
        Assumption(
            "technician_hourly_ntd", "維修技師全負擔人力成本",
            MAINTENANCE_TECHNICIAN_HOURLY_NTD, "NTD/hr",
            Basis.ASSUMPTION, "假設（推估過程列於 rationale）",
            "年薪 60–80 萬 ＋ 勞健保退休金管理費 ≈ 全負擔 100 萬/年 ÷ 2,000 有效工時。",
        ),
        Assumption(
            "manual_root_cause_min", "現行流程人工判定根因所需工時", MANUAL_ROOT_CAUSE_MIN, "分鐘",
            Basis.ASSUMPTION, "假設（推估過程列於 rationale）",
            "到場 15 ＋ 現場量測 30 ＋ 查手冊與歷史工單 30 ＋ 與生產確認 15 ≈ 90 分鐘。",
        ),
        Assumption(
            "manual_work_order_completeness_pct", "現行人工工單完整度",
            MANUAL_WORK_ORDER_COMPLETENESS_PCT, "%",
            Basis.ASSUMPTION, "假設",
            "以 WorkOrder.completeness() 的欄位口徑估：有故障描述與機台，"
            "但缺手冊條款、建議零件與 SOP 連結。",
        ),
        Assumption(
            "work_order_rework_trip_hours", "工單缺料多跑一趟的工時",
            WORK_ORDER_REWORK_TRIP_HOURS, "小時", Basis.ASSUMPTION, "假設", "倉庫往返與重新備料。",
        ),
        Assumption(
            "order_delay_penalty_ntd_per_min", "交期延遲罰則",
            ORDER_DELAY_PENALTY_NTD_PER_MIN, "NTD/分鐘/張",
            Basis.ASSUMPTION, "假設（推估過程列於 rationale）",
            "代工合約常見 0.5%/日；240 件 × 售價（邊際貢獻 3 倍 ≈ 555 元）≈ 13.3 萬，"
            "0.5%/日 ≈ 0.46 元/分鐘，取 0.5。",
        ),
        Assumption(
            "expedite_cost_per_late_order_ntd", "每張延遲訂單的趕工成本",
            EXPEDITE_COST_PER_LATE_ORDER_NTD, "NTD/張",
            Basis.ASSUMPTION, "假設（推估過程列於 rationale）",
            "補一個班別：2 人 × 8 小時 × 250 元/hr × 1.34 倍加班費 ≈ 5,360 元。",
        ),
        Assumption(
            "safety_incident_cost_ntd", "一次可記錄工安事故的期望成本",
            SAFETY_INCIDENT_COST_NTD, "NTD/次",
            Basis.ASSUMPTION, "假設（推估過程列於 rationale）",
            "醫療賠償 30 萬 ＋ 停工調查 8 小時 × 22,200 元/hr ≈ 17.8 萬 ＋ 檢查改善 10 萬 ≈ 58 萬，"
            "保守下修取 50 萬。刻意不含重大職災情境。",
        ),
        Assumption(
            "hazard_injury_probability_per_hour", "危險區每曝露小時的可記錄工傷機率",
            HAZARD_INJURY_PROBABILITY_PER_EXPOSURE_HOUR, "機率/小時",
            Basis.ASSUMPTION, "純假設，無公開統計可引用",
            "不調整到讓 ROI 好看的位置；破口分析直接給臨界值讓評審自行判斷。",
        ),
        Assumption(
            "carbon_fee_ntd_per_tonne", "碳費費率", CARBON_FEE_NTD_PER_TONNE, "NTD/公噸CO2e",
            Basis.ASSUMPTION, "環境部公告碳費一般費率 300 元/公噸（費率為公開值，適用性為假設）",
            "徵收對象為年排放 2.5 萬公噸以上之排放源，單線規模通常未達門檻；"
            "本案視為內部碳定價／碳權的機會成本，非必然現金支出。",
        ),
        Assumption(
            "electricity_tariff_ntd_per_kwh", "工業用電電價",
            ELECTRICITY_TARIFF_NTD_PER_KWH, "NTD/kWh",
            Basis.ASSUMPTION, ELECTRICITY_TARIFF_SOURCE,
            "沿用 twin/energy.py 的既有假設，商業案例不另設一套電價。",
        ),
        Assumption(
            "grid_emission_factor", "電力排碳係數",
            GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH, "kgCO2e/kWh",
            Basis.ASSUMPTION, GRID_EMISSION_FACTOR_SOURCE,
            "沿用 twin/energy.py 的既有假設。",
        ),
        Assumption(
            "unit_margin_ntd", "每單位產品邊際貢獻", UNIT_MARGIN_NTD, "NTD/件",
            Basis.ASSUMPTION, "factory_guardian/twin/topology.py::UNIT_MARGIN_NTD",
            "沿用孿生體既有常數；production_loss_ntd 就是用它換算的。",
        ),
        Assumption(
            "discount_rate", "折現率", DISCOUNT_RATE, "比例",
            Basis.ASSUMPTION, "假設", "中小型製造業資金成本量級，用於三年 NPV。",
        ),
    )
}


def assumptions_to_list() -> list[dict[str, Any]]:
    return [a.to_dict() for a in ASSUMPTIONS.values()]


DISCLAIMER = (
    "SYNTHETIC DEMO DATA：效益數字由 Factory Digital Twin 實際模擬執行量測而得；"
    "事件頻率、單價與成本結構為明確標示的假設參數，不代表任何真實工廠的財務資料。"
)


__all__ = [
    "Basis",
    "BASIS_LABEL",
    "Assumption",
    "ASSUMPTIONS",
    "assumptions_to_list",
    "DISCLAIMER",
    "MAINTENANCE_CORPUS_CASES",
    "MAINTENANCE_CORPUS_SPAN_DAYS",
    "ANNUAL_MAINTENANCE_EVENTS_PER_LINE",
    "PRODUCTION_IMPACTING_EVENT_SHARE",
    "FAULT_MIX",
    "SIMULTANEOUS_INTRUSION_RATE",
    "ANNUAL_HAZARD_INTRUSION_EVENTS_PER_LINE",
    "TYPICAL_INTRUSION_MIN",
    "INCUMBENT_ESCALATION_RATE",
    "BENEFIT_REALIZATION_RATE",
    "MAINTENANCE_TECHNICIAN_HOURLY_NTD",
    "MANUAL_ROOT_CAUSE_MIN",
    "MANUAL_WORK_ORDER_COMPLETENESS_PCT",
    "WORK_ORDER_REWORK_TRIP_HOURS",
    "ORDER_DELAY_PENALTY_NTD_PER_MIN",
    "EXPEDITE_COST_PER_LATE_ORDER_NTD",
    "SAFETY_INCIDENT_COST_NTD",
    "HAZARD_INJURY_PROBABILITY_PER_EXPOSURE_HOUR",
    "CARBON_FEE_NTD_PER_TONNE",
    "DISCOUNT_RATE",
    "secondary_damage_cost",
]
