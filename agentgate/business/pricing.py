"""收費方式與級距 —— 把規格 §8.1 的一句話變成數字。

規格 §8.1 的「收費」欄只有一行:

> 平台年費 + 依受治理 Agent 數或裁決次數計價;高保證等級(不可否認稽核)另計

這個模組給出級距。**所有價格都是假設**(``Basis.ASSUMPTION``)——
我們沒有報過價、沒有簽過約,所以不會把它們寫成「市場行情」。
每一條的 ``rationale`` 說明級距是怎麼抓的,讓評審可以直接質疑那個推估。

五塊:

1. **平台年費** —— Dashboard、政策管理、角色權限、稽核保存的基礎費。
2. **席位費** —— 每一個受治理的 AI Agent 席位。席位數直接數 ``console.SEATS``。
3. **裁決計價** —— 每千次裁決。成本結構在 ``rationale`` 裡寫清楚(實測 4.1 筆稽核/裁決)。
4. **高保證等級(不可否認稽核)** —— 規格 §8.1 明列「另計」的那一項:
   核准者身分綁定、雜湊鏈與外部錨定、7 年保存與稽核調閱 SLA。
5. **中華電信可辨識收入** —— hicloud 主權雲託管 + 門號級身分驗證服務呼叫量。
   這兩項**不是我們的收入**(除非由中華電信轉售整包),但它們是客戶 TCO 的一部分,
   也是本方案的硬需求(規格 §9:核准者身分綁定是電信不可取代的接入點),
   所以必須進 ROI 的分母。這一塊同時就是「業務連結性」那一項的具體金額。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .assumptions import Basis


def default_agent_seats() -> int:
    """受治理的 AI Agent 席位數。直接數 ``console.SEATS``,不寫死。"""
    try:
        from ..console import SEATS

        return sum(len(v) for v in SEATS.values()) or 7
    except Exception:  # pragma: no cover - console 不可用時不該讓商業案例整個炸掉
        return 7


@dataclass(frozen=True)
class PriceBook:
    """三段式定價 + 高保證加購 + 中華電信項目。全部為假設值(NTD)。"""

    # 1. 平台年費
    platform_annual_ntd: float = 2_400_000.0
    # 2. 席位費(每個受治理 Agent 席位)
    seat_annual_ntd: float = 240_000.0
    # 3. 裁決計價(每千次)
    decision_per_1k_ntd: float = 300.0
    # 4. 高保證等級:不可否認稽核(另計)
    assurance_annual_ntd: float = 1_200_000.0
    # 5. 中華電信可辨識收入
    cht_hicloud_annual_ntd: float = 720_000.0
    cht_identity_verify_ntd: float = 3.0          # 每次核准者門號級身分驗證
    # 一次性導入整合
    integration_one_time_ntd: float = 2_800_000.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": Basis.ASSUMPTION.value,
            "platform_annual_ntd": self.platform_annual_ntd,
            "seat_annual_ntd": self.seat_annual_ntd,
            "decision_per_1k_ntd": self.decision_per_1k_ntd,
            "assurance_annual_ntd": self.assurance_annual_ntd,
            "cht_hicloud_annual_ntd": self.cht_hicloud_annual_ntd,
            "cht_identity_verify_ntd": self.cht_identity_verify_ntd,
            "integration_one_time_ntd": self.integration_one_time_ntd,
        }

    def rationales(self) -> list[dict[str, str]]:
        """每一條級距是怎麼抓的。評審可以逐條質疑。"""
        return [
            {
                "item": "平台年費",
                "tier": f"{self.platform_annual_ntd:,.0f} NTD/年",
                "rationale": "Dashboard、13 條政策規則的管理與版本、角色權限、"
                             "稽核鏈保存與查詢。企業級單租戶,不含席位與裁決量。",
            },
            {
                "item": "受治理 Agent 席位",
                "tier": f"{self.seat_annual_ntd:,.0f} NTD/席/年",
                "rationale": "一個席位 = 一個有獨立身分、獨立政策綁定與獨立稽核軌跡的 "
                             "AI Agent。席位數直接數 console.SEATS(目前 "
                             f"{default_agent_seats()} 席:App 2、Web 2、語音 1、門市 1、營運 1)。",
            },
            {
                "item": "裁決計價",
                "tier": f"{self.decision_per_1k_ntd:,.0f} NTD/千次裁決",
                "rationale": "成本結構可被追問:一次裁決的決策延遲量級為 0.1 ms(實測),"
                             "算力可忽略;主要成本在稽核保存 —— 每次裁決平均寫入 4.1 筆"
                             "稽核紀錄(實測),需保存 7 年並可隨時調閱。",
            },
            {
                "item": "高保證等級(不可否認稽核)",
                "tier": f"{self.assurance_annual_ntd:,.0f} NTD/年(另計)",
                "rationale": "規格 §8.1 明列另計。內容:核准者門號級身分綁定、"
                             "雜湊鏈外部錨定、7 年保存與監理調閱 SLA、稽核完整率報表。"
                             "受監理產業才需要,所以不併入平台年費。",
            },
            {
                "item": "中華電信 hicloud 主權雲託管",
                "tier": f"{self.cht_hicloud_annual_ntd:,.0f} NTD/年",
                "rationale": "稽核資料留在境內是受監理產業的採購前提(規格 §9)。"
                             "這是中華電信可辨識收入,不是本方案收入,但進 ROI 分母。",
            },
            {
                "item": "中華電信門號級身分驗證",
                "tier": f"{self.cht_identity_verify_ntd:,.1f} NTD/次",
                "rationale": "只在 G4 核准時呼叫一次(核准是法律責任行為,必須確認"
                             "「按下核准的是本人」)。呼叫量 = 送核准件數,由實測的"
                             "送核准比例推導。中華電信可辨識收入。",
            },
            {
                "item": "導入整合(一次性)",
                "tier": f"{self.integration_one_time_ntd:,.0f} NTD",
                "rationale": "Agent runtime 接管、後端系統動作對應、影子環境建置、"
                             "身分驗證串接、治理事件併入既有 SOC、政策校準與情境測試集客製。",
            },
        ]


DEFAULT_PRICES = PriceBook()


@dataclass(frozen=True)
class PlatformCost:
    """一年的方案成本。``cht_*`` 兩項單獨留著,因為它們要單獨報。"""

    seats: int
    annual_decisions: float
    annual_approvals: float
    platform: float
    seat: float
    decision: float
    assurance: float
    cht_hicloud: float
    cht_identity: float
    one_time: float

    @property
    def annual_recurring_ntd(self) -> float:
        return (self.platform + self.seat + self.decision + self.assurance
                + self.cht_hicloud + self.cht_identity)

    @property
    def cht_recognisable_annual_ntd(self) -> float:
        """中華電信可辨識的年度收入(不含轉售平台本身)。"""
        return self.cht_hicloud + self.cht_identity

    @property
    def year_one_ntd(self) -> float:
        return self.annual_recurring_ntd + self.one_time

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": Basis.DERIVED.value,
            "seats": self.seats,
            "annual_decisions": round(self.annual_decisions, 0),
            "annual_approvals": round(self.annual_approvals, 0),
            "platform_ntd": round(self.platform, 0),
            "seat_ntd": round(self.seat, 0),
            "decision_ntd": round(self.decision, 0),
            "assurance_ntd": round(self.assurance, 0),
            "cht_hicloud_ntd": round(self.cht_hicloud, 0),
            "cht_identity_ntd": round(self.cht_identity, 0),
            "annual_recurring_ntd": round(self.annual_recurring_ntd, 0),
            "cht_recognisable_annual_ntd": round(self.cht_recognisable_annual_ntd, 0),
            "one_time_ntd": round(self.one_time, 0),
            "year_one_ntd": round(self.year_one_ntd, 0),
        }


def build_platform_cost(
    annual_decisions: float,
    annual_approvals: float,
    seats: int | None = None,
    prices: PriceBook = DEFAULT_PRICES,
    cost_multiplier: float = 1.0,
) -> PlatformCost:
    """依裁決量與核准量算出年度成本。成本會隨量成長,所以它進得了破口分析。"""
    seats = default_agent_seats() if seats is None else seats
    m = cost_multiplier
    return PlatformCost(
        seats=seats,
        annual_decisions=annual_decisions,
        annual_approvals=annual_approvals,
        platform=prices.platform_annual_ntd * m,
        seat=prices.seat_annual_ntd * seats * m,
        decision=prices.decision_per_1k_ntd * (annual_decisions / 1000.0) * m,
        assurance=prices.assurance_annual_ntd * m,
        cht_hicloud=prices.cht_hicloud_annual_ntd * m,
        cht_identity=prices.cht_identity_verify_ntd * annual_approvals * m,
        one_time=prices.integration_one_time_ntd * m,
    )


__all__ = [
    "DEFAULT_PRICES",
    "PlatformCost",
    "PriceBook",
    "build_platform_cost",
    "default_agent_seats",
]
