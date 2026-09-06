"""AgentGate 商業案例的假設參數 —— 全部集中在這裡,全部有名字,全部標明依據。

規則和 ``factory_guardian/business/assumptions.py`` 一樣,只有一條:
**每個數字都要能被評審追問,而且追問得到答案。**

所以每一筆都帶 ``basis``,只有三種值:

* :attr:`Basis.MEASURED` —— 由本 repo 跑出來(``console.OpsSimulator`` 回填的當班資料、
  ``validation.run_validation()`` 的 baseline 指標)。
* :attr:`Basis.DERIVED` —— 由實測值與具名假設,用寫出來的公式推導。
* :attr:`Basis.ASSUMPTION` —— 工程或商業假設。沒有可引用的公開統計時就誠實寫「假設」,
  並在 ``rationale`` 裡寫出量級是怎麼估的,讓人可以直接質疑那個推估過程。

沒有第四種。特別是**沒有「業界平均」這一種** —— 我們沒有取得任何電信業者或企業的
真實營運統計,所以不會有任何一個數字被包裝成產業數據。

> **SYNTHETIC DEMO DATA** —— 本模組所有金額假設皆為競賽用估算值,
> 不代表任何真實企業的財務資料。定價未經報價或簽約驗證。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Basis(str, Enum):
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
    """一個具名參數。``source`` 指得到 repo 裡的位置或公開來源,``rationale`` 寫推估過程。"""

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
            "value": round(self.value, 6),
            "unit": self.unit,
            "basis": self.basis.value,
            "basis_label": BASIS_LABEL[self.basis.value],
            "source": self.source,
            "rationale": self.rationale,
        }


# ======================================================================================
# 一、動作量 —— 把 45 分鐘的當班回填換算成年度的唯一橋樑,所以放最前面
# ======================================================================================
#: 一天有幾小時受治理。直接數 ``console.SHIFTS`` 三個班別的時數(夜/早/中,各 8 小時)。
#: 這是實測值:班表改成兩班,這裡就跟著變。
def governed_hours_per_day() -> float:
    from ..console import SHIFTS

    return float(sum(end - start for _, start, end, _ in SHIFTS))


#: 一年營運幾天。客服治理層沒有停機日。
OPERATING_DAYS_PER_YEAR: float = 365.0


# ======================================================================================
# 二、人力與覆核工時
# ======================================================================================
#: 覆核者(帳務／客服值班主管)的全負擔人力成本。
REVIEWER_HOURLY_COST_NTD: float = 480.0

#: 沒有證據包時,一次動作覆核要花的時間。
MANUAL_REVIEW_MINUTES: float = 3.0

#: 有證據包時的目標覆核時間(規格 §4.5「主管必須能在 15 秒內做出有依據的決定」)。
EVIDENCE_REVIEW_MINUTES: float = 0.25


# ======================================================================================
# 三、現況(這是整份 ROI 最敏感的一根軸,見 docs 的「拿什麼當現況」一節)
# ======================================================================================
#: 現況下真的會被人工覆核的動作比例。三種現況假設:
#:
#: * ``1.0`` —— 規格 §8.2 的字面值:沒有治理層時 100% 人工覆核。
#: * ``= 實測的「風險 ≥ medium 動作比例」`` —— 分層覆核:企業只讓 Agent 自動做唯讀查詢,
#:   任何會改動狀態的動作退回人工。**基準情境採用這一種**,由 console 回填資料實測。
#: * ``0.0`` —— 完全不導入:Agent 降級成問答機器人,覆核節省歸零。
#:
#: 基準情境刻意不採用 §8.2 的字面值 —— 沒有企業會請幾十個人逐件覆核幾百萬筆動作,
#: 拿那個當現況會讓效益虛胖 2.6 倍。
INCUMBENT_REVIEW_COVERAGE_MODE: str = "tiered"

#: 效益實現率:涵蓋導入初期的信任建立(主管仍會抽查自動放行的動作)、
#: 政策校準期的保守設定、以及真實覆核工時可能比估的短。
BENEFIT_REALIZATION: float = 0.8


# ======================================================================================
# 四、誤動作損失 —— 全部是假設,罰則上限是公開值但適用性是假設
# ======================================================================================
#: 一次達到裁罰門檻的個資外洩事件的期望成本(NTD)。
#:
#: 推估過程:《個人資料保護法》第 48 條(2023 年 5 月修正)對非公務機關違反
#: 第 27 條安全維護義務者,得處 **2 萬–200 萬元**罰鍰;**情節重大者 15 萬–1,500 萬元**。
#: 罰鍰級距是公開值,可以查證;**適用與否、落在級距哪裡,是我們的假設**。
#: 取「情節重大下限 150 萬 × 適用機率 0.5 = 75 萬」+「當事人通知、客服增量與
#: 信用監控 45 萬」= 120 萬。刻意不取 1,500 萬上限 —— 那會讓這一項主導整份 ROI。
PII_BREACH_COST_NTD: float = 1_200_000.0

#: 一次錯誤金流(退費金額錯誤、入帳對象錯誤)的期望成本(NTD)。
#: 推估:誤付金額量級 3 萬 × 追回失敗率 0.6 = 1.8 萬,加上帳務更正與客訴處理 0.7 萬。
WRONG_PAYMENT_COST_NTD: float = 25_000.0

#: 一次 SIM swap 導致帳號接管的期望成本(NTD)。
#: 推估:受害人金融損失量級 50 萬 × 電信業者責任分攤假設 0.5 = 25 萬,
#: 加上調查、客訴與補救 10 萬 = 35 萬。不計商譽與監理檢查成本。
SIM_SWAP_COST_NTD: float = 350_000.0

#: 一年會發生幾次(整個企業,不是每席位)。三個都是純假設。
ANNUAL_PII_BREACH_EVENTS: float = 0.5
ANNUAL_WRONG_PAYMENT_EVENTS: float = 24.0
ANNUAL_SIM_SWAP_EVENTS: float = 2.0


# ======================================================================================
# 五、財務
# ======================================================================================
DISCOUNT_RATE: float = 0.08
NPV_YEARS: int = 3

#: 三種來源的規則,報表與文件都引用同一句話。
ASSUMPTION_DOC: str = (
    "每個數字只有三種來源:實測(由本 repo 的 console 回填當班資料或 validation "
    "跑出來)、推導(由實測值與具名假設用寫出來的公式算出)、假設(工程或商業估算,"
    "rationale 裡寫出推估過程)。沒有第四種,特別是沒有「業界平均」。"
)

DISCLAIMER: str = (
    "SYNTHETIC DEMO DATA —— 動作量、風險分佈與自動放行率由 agentgate.console 的"
    "合成當班流量實測而得,不是任何電信業者的真實話務資料;金額、事件頻率與定價"
    "為明確標示的假設參數。個資法罰鍰級距為公開值,其適用性為假設。"
    "所有數字未經任何企業實地驗證,亦不宣稱與中華電信有任何既有合作關係。"
)


def static_assumptions() -> list[Assumption]:
    """不依賴實測的那些參數。實測與推導的部分由 :mod:`.model` 在跑完量測後補上。"""
    return [
        Assumption(
            "governed_hours_per_day", "每日受治理時數", governed_hours_per_day(), "小時/日",
            Basis.MEASURED, "console.SHIFTS(夜/早/中三班,各 8 小時)",
            "直接數班表的時數總和,不是另外估的;班表改成兩班,這個數字就跟著變。",
        ),
        Assumption(
            "operating_days_per_year", "年營運天數", OPERATING_DAYS_PER_YEAR, "天/年",
            Basis.ASSUMPTION, "—", "客服治理層 7×24 運轉,無停機日。",
        ),
        Assumption(
            "reviewer_hourly_cost", "覆核者全負擔人力成本", REVIEWER_HOURLY_COST_NTD, "NTD/小時",
            Basis.ASSUMPTION, "—",
            "帳務／客服值班主管年薪 70–90 萬 + 勞健保、退休金與管理費 ≈ 全負擔 96 萬/年,"
            "÷ 2,000 有效工時 = 480 NTD/小時。",
        ),
        Assumption(
            "manual_review_minutes", "無證據包時的單次覆核工時", MANUAL_REVIEW_MINUTES, "分鐘/次",
            Basis.ASSUMPTION, "—",
            "主管要自己開帳戶頁面(1 分)、翻對話與附件確認指令出處(1.5 分)、"
            "確認身分與權限(0.5 分)。**這是保守下限** —— 真實情況常常更久,"
            "但估高了會讓效益虛胖,所以取 3 分鐘。",
        ),
        Assumption(
            "evidence_review_minutes", "有證據包時的單次覆核工時", EVIDENCE_REVIEW_MINUTES,
            "分鐘/次", Basis.ASSUMPTION, "規格 §4.5「15 秒內做出有依據的決定」",
            "這是**設計目標,不是實測值** —— 尚未做過真人計時。"
            "用它算出來的那一條效益(證據包加速)因此標為假設,並在破口分析裡單獨測試。",
        ),
        Assumption(
            "benefit_realization", "效益實現率", BENEFIT_REALIZATION, "比例",
            Basis.ASSUMPTION, "—",
            "涵蓋導入初期主管仍會抽查自動放行的動作、政策校準期的保守設定、"
            "以及真實覆核工時可能比估的短。",
        ),
        Assumption(
            "pii_breach_cost", "一次個資外洩事件的期望成本", PII_BREACH_COST_NTD, "NTD/次",
            Basis.ASSUMPTION, "《個人資料保護法》第 48 條(2023-05 修正)罰鍰級距為公開值",
            "非公務機關違反安全維護義務可處 2 萬–200 萬;情節重大者 15 萬–1,500 萬。"
            "**級距是公開值,適用性是假設。** 取情節重大下限 150 萬 × 適用機率 0.5 = 75 萬,"
            "加當事人通知、客服增量與信用監控 45 萬 = 120 萬。刻意不取 1,500 萬上限。",
        ),
        Assumption(
            "wrong_payment_cost", "一次錯誤金流的期望成本", WRONG_PAYMENT_COST_NTD, "NTD/次",
            Basis.ASSUMPTION, "—",
            "誤付金額量級 3 萬 × 追回失敗率 0.6 = 1.8 萬,加帳務更正與客訴處理 0.7 萬。",
        ),
        Assumption(
            "sim_swap_cost", "一次 SIM swap 帳號接管的期望成本", SIM_SWAP_COST_NTD, "NTD/次",
            Basis.ASSUMPTION, "—",
            "受害人金融損失量級 50 萬 × 電信業者責任分攤假設 0.5 = 25 萬,"
            "加調查、客訴與補救 10 萬。不計商譽與監理檢查成本。",
        ),
        Assumption(
            "annual_pii_breach_events", "年度個資外洩事件數", ANNUAL_PII_BREACH_EVENTS, "次/年",
            Basis.ASSUMPTION, "—", "以兩年一次估算。**純假設**:我們沒有任何企業的事故統計可引用。",
        ),
        Assumption(
            "annual_wrong_payment_events", "年度錯誤金流事件數", ANNUAL_WRONG_PAYMENT_EVENTS,
            "次/年", Basis.ASSUMPTION, "—", "以每月 2 次估算。**純假設**。",
        ),
        Assumption(
            "annual_sim_swap_events", "年度 SIM swap 事件數", ANNUAL_SIM_SWAP_EVENTS, "次/年",
            Basis.ASSUMPTION, "—", "以半年一次估算。**純假設**。",
        ),
        Assumption(
            "discount_rate", "折現率", DISCOUNT_RATE, "比例",
            Basis.ASSUMPTION, "—", "中大型企業資金成本量級,用於三年 NPV。",
        ),
    ]


__all__ = [
    "ANNUAL_PII_BREACH_EVENTS",
    "ASSUMPTION_DOC",
    "ANNUAL_SIM_SWAP_EVENTS",
    "ANNUAL_WRONG_PAYMENT_EVENTS",
    "BASIS_LABEL",
    "BENEFIT_REALIZATION",
    "DISCLAIMER",
    "DISCOUNT_RATE",
    "EVIDENCE_REVIEW_MINUTES",
    "INCUMBENT_REVIEW_COVERAGE_MODE",
    "MANUAL_REVIEW_MINUTES",
    "NPV_YEARS",
    "OPERATING_DAYS_PER_YEAR",
    "PII_BREACH_COST_NTD",
    "REVIEWER_HOURLY_COST_NTD",
    "SIM_SWAP_COST_NTD",
    "WRONG_PAYMENT_COST_NTD",
    "Assumption",
    "Basis",
    "governed_hours_per_day",
    "static_assumptions",
]
