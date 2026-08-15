"""成本結構與定價 —— 把提案 §12.1 的四種收費方式從名詞變成數字。

提案書 §12.1 列了四種收費方式，但只有名詞沒有級距：

1. **平台 SaaS**：依 Factory / Production Line / Machine 計費。
2. **AI Usage**：依 Camera、Agent、Inference 使用量計費。
3. **System Integration**：MES / PLC / IoT / Camera 串接。
4. **Managed Service**：7×24 Monitoring、AI Operations、Security 與維運分析。

這個模組給出級距。**所有價格都是假設**（``Basis.ASSUMPTION``）——
我們沒有報過價、沒有簽過約，所以不會把它們寫成「市場行情」。
每一條的 ``rationale`` 說明級距是怎麼抓的，讓評審可以直接質疑那個推估。

第五塊是**中華電信網路與邊緣**（5G 企業專網 ＋ MEC 節點代管 ＋ 邊緣硬體）。
它不是我們的收入，但它是客戶 TCO 的一部分，也是本架構的硬需求
（見 ``deployment/tiers.py``：控制關鍵路徑上的每一個元件都必須在 EDGE），
所以它必須進 ROI 的分母。它同時也是「業務連結性」這一項的具體金額。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .assumptions import Basis

# --------------------------------------------------------------------------------------
# 規模預設值：機台數從孿生體拓撲動態讀，不寫死。
# --------------------------------------------------------------------------------------
_FALLBACK_MACHINES_PER_LINE = 3


def default_machines_per_line() -> int:
    """一條產線的機台數。直接數 ``build_factory()`` 的機台，孿生體改了這裡就跟著改。"""
    try:
        from ..twin.topology import build_factory

        return len(build_factory().machines) or _FALLBACK_MACHINES_PER_LINE
    except Exception:  # pragma: no cover - 拓撲不可用時不該讓商業案例整個炸掉
        return _FALLBACK_MACHINES_PER_LINE


@dataclass(frozen=True)
class PriceBook:
    """四種收費方式的級距。全部為假設值（NTD）。"""

    # 1. 平台 SaaS（月費）
    platform_site_monthly_ntd: float = 12_000.0
    platform_line_monthly_ntd: float = 5_000.0
    platform_machine_monthly_ntd: float = 900.0

    # 2. AI Usage（用量）
    ai_camera_monthly_ntd: float = 2_200.0          # 每路工安 Camera，邊緣 VLM 7×24 推論
    ai_agent_run_ntd: float = 15.0                  # 每次完整 Agent 閉環（含敘述與稽核保存）
    ai_forecast_per_1k_ntd: float = 300.0           # 時序預測（TabFM / Ridge）每千次推論

    # 3. System Integration（一次性）
    integration_small_ntd: float = 280_000.0        # 1 線、≤5 機台
    integration_medium_ntd: float = 650_000.0       # 2–6 線
    integration_large_ntd: float = 1_500_000.0      # 7 線以上或多廠

    # 4. Managed Service（月費）
    managed_standard_monthly_ntd: float = 9_000.0   # 8×5 監控 + 月報
    managed_advanced_monthly_ntd: float = 22_000.0  # 7×24 + OT SOC + SLA 99.9%

    # 5. 中華電信網路與邊緣（通路綁售；客戶 TCO，非本方案收入）
    cht_private_5g_monthly_ntd: float = 28_000.0
    cht_mec_hosting_monthly_ntd: float = 18_000.0
    edge_hardware_one_time_ntd: float = 450_000.0

    def integration_fee(self, lines: int) -> tuple[float, str]:
        if lines <= 1:
            return self.integration_small_ntd, "小型級距（1 線、≤5 機台）"
        if lines <= 6:
            return self.integration_medium_ntd, "中型級距（2–6 線）"
        return self.integration_large_ntd, "大型級距（7 線以上或多廠）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": Basis.ASSUMPTION.value,
            "currency": "NTD",
            "note": "提案 §12.1 四種收費方式的級距；全部為假設值，未經報價或簽約驗證。",
            **{k: round(float(v), 2) for k, v in self.__dict__.items()},
        }


DEFAULT_PRICES = PriceBook()


# --------------------------------------------------------------------------------------
# 部署規模
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class DeploymentScope:
    """一次導入的規模。效益與成本都以此為分母。"""

    key: str
    label: str
    lines: int
    machines_per_line: int = field(default_factory=default_machines_per_line)
    cameras_per_line: int = 1
    managed_tier: str = "standard"   # standard | advanced
    note: str = ""

    @property
    def machines(self) -> int:
        return self.lines * self.machines_per_line

    @property
    def cameras(self) -> int:
        return self.lines * self.cameras_per_line

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": Basis.ASSUMPTION.value,
            "key": self.key,
            "label": self.label,
            "lines": self.lines,
            "machines_per_line": self.machines_per_line,
            "machines": self.machines,
            "cameras": self.cameras,
            "managed_tier": self.managed_tier,
            "note": self.note,
        }


def pilot_scope() -> DeploymentScope:
    return DeploymentScope(
        key="pilot",
        label="Pilot Line｜單線試點",
        lines=1,
        managed_tier="standard",
        note="導入的第一步：一條產線、一台關鍵設備、一種事故。這是驗證投資，不是回本專案。",
    )


def plant_scope(lines: int = 6) -> DeploymentScope:
    return DeploymentScope(
        key="plant",
        label=f"Plant Rollout｜全廠 {lines} 線",
        lines=lines,
        managed_tier="advanced",
        note="目標客戶規模：有高價設備、停機成本高、維修知識分散的中大型製造廠。",
    )


# --------------------------------------------------------------------------------------
# 成本試算
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CostItem:
    key: str
    label: str
    stream: str        # platform_saas | ai_usage | system_integration | managed_service | cht_network
    cadence: str       # one_time | monthly
    amount_ntd: float
    formula: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": Basis.ASSUMPTION.value,
            "key": self.key,
            "label": self.label,
            "stream": self.stream,
            "cadence": self.cadence,
            "amount_ntd": round(self.amount_ntd, 0),
            "annual_ntd": round(self.amount_ntd * (12 if self.cadence == "monthly" else 0), 0),
            "formula": self.formula,
        }


STREAM_LABELS: dict[str, str] = {
    "platform_saas": "平台 SaaS（廠／線／機台）",
    "ai_usage": "AI Usage（Camera／Agent／Inference）",
    "system_integration": "System Integration（一次性）",
    "managed_service": "Managed Service（維運）",
    "cht_network": "中華電信網路與邊緣（客戶 TCO）",
}


@dataclass
class SolutionCost:
    """一次導入的完整成本結構。"""

    scope: DeploymentScope
    items: list[CostItem]
    prices: PriceBook
    cost_multiplier: float = 1.0

    @property
    def one_time_ntd(self) -> float:
        return sum(i.amount_ntd for i in self.items if i.cadence == "one_time") * self.cost_multiplier

    @property
    def monthly_ntd(self) -> float:
        return sum(i.amount_ntd for i in self.items if i.cadence == "monthly") * self.cost_multiplier

    @property
    def annual_recurring_ntd(self) -> float:
        return self.monthly_ntd * 12.0

    @property
    def year_one_ntd(self) -> float:
        return self.one_time_ntd + self.annual_recurring_ntd

    def by_stream(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for item in self.items:
            bucket = out.setdefault(item.stream, {"one_time_ntd": 0.0, "annual_ntd": 0.0})
            if item.cadence == "one_time":
                bucket["one_time_ntd"] += item.amount_ntd * self.cost_multiplier
            else:
                bucket["annual_ntd"] += item.amount_ntd * 12.0 * self.cost_multiplier
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_dict(),
            "prices": self.prices.to_dict(),
            "items": [i.to_dict() for i in self.items],
            "by_stream": {
                stream: {
                    "basis": Basis.ASSUMPTION.value,
                    "label": STREAM_LABELS.get(stream, stream),
                    **{k: round(v, 0) for k, v in amounts.items()},
                }
                for stream, amounts in self.by_stream().items()
            },
            "totals": {
                "basis": Basis.DERIVED.value,
                "cost_multiplier": round(self.cost_multiplier, 3),
                "one_time_ntd": round(self.one_time_ntd, 0),
                "monthly_ntd": round(self.monthly_ntd, 0),
                "annual_recurring_ntd": round(self.annual_recurring_ntd, 0),
                "year_one_ntd": round(self.year_one_ntd, 0),
            },
        }


def build_solution_cost(
    scope: DeploymentScope,
    annual_agent_runs: float = 0.0,
    prices: PriceBook = DEFAULT_PRICES,
    cost_multiplier: float = 1.0,
) -> SolutionCost:
    """依規模與年度事件數組出成本結構。"""
    integration_fee, integration_note = prices.integration_fee(scope.lines)
    managed_monthly = (
        prices.managed_advanced_monthly_ntd
        if scope.managed_tier == "advanced"
        else prices.managed_standard_monthly_ntd
    )
    managed_label = "進階（7×24 + OT SOC + SLA 99.9%）" if scope.managed_tier == "advanced" else "標準（8×5 + 月報）"

    items = [
        CostItem(
            "platform_site", "平台基礎費（每廠）", "platform_saas", "monthly",
            prices.platform_site_monthly_ntd,
            f"{prices.platform_site_monthly_ntd:,.0f} × 1 廠",
        ),
        CostItem(
            "platform_line", "產線授權", "platform_saas", "monthly",
            prices.platform_line_monthly_ntd * scope.lines,
            f"{prices.platform_line_monthly_ntd:,.0f} × {scope.lines} 線",
        ),
        CostItem(
            "platform_machine", "機台授權", "platform_saas", "monthly",
            prices.platform_machine_monthly_ntd * scope.machines,
            f"{prices.platform_machine_monthly_ntd:,.0f} × {scope.machines} 台",
        ),
        CostItem(
            "ai_camera", "工安 Camera 邊緣 VLM", "ai_usage", "monthly",
            prices.ai_camera_monthly_ntd * scope.cameras,
            f"{prices.ai_camera_monthly_ntd:,.0f} × {scope.cameras} 路",
        ),
        CostItem(
            "ai_agent_runs", "Agent 閉環執行", "ai_usage", "monthly",
            prices.ai_agent_run_ntd * annual_agent_runs / 12.0,
            f"{prices.ai_agent_run_ntd:,.0f} × {annual_agent_runs:,.1f} 次/年 ÷ 12",
        ),
        CostItem(
            "system_integration", f"MES/PLC/IoT/Camera 串接 — {integration_note}",
            "system_integration", "one_time", integration_fee,
            f"{integration_note}：{integration_fee:,.0f}",
        ),
        CostItem(
            "managed_service", f"維運服務 — {managed_label}", "managed_service", "monthly",
            managed_monthly, f"{managed_monthly:,.0f}/月",
        ),
        CostItem(
            "cht_private_5g", "5G 企業專網（廠區級）", "cht_network", "monthly",
            prices.cht_private_5g_monthly_ntd, f"{prices.cht_private_5g_monthly_ntd:,.0f}/月",
        ),
        CostItem(
            "cht_mec", "MEC 邊緣節點代管（含 GPU 推論）", "cht_network", "monthly",
            prices.cht_mec_hosting_monthly_ntd, f"{prices.cht_mec_hosting_monthly_ntd:,.0f}/月",
        ),
        CostItem(
            "edge_hardware", "邊緣硬體與建置（一次性）", "cht_network", "one_time",
            prices.edge_hardware_one_time_ntd, f"{prices.edge_hardware_one_time_ntd:,.0f}",
        ),
    ]
    return SolutionCost(scope=scope, items=items, prices=prices, cost_multiplier=cost_multiplier)


__all__ = [
    "PriceBook",
    "DEFAULT_PRICES",
    "DeploymentScope",
    "pilot_scope",
    "plant_scope",
    "CostItem",
    "SolutionCost",
    "STREAM_LABELS",
    "build_solution_cost",
    "default_machines_per_line",
]
