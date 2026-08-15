"""商業案例層 —— 把技術 KPI 換算成年度商業論據。

決賽評分「商業價值與市場性」占 40%（研究文件 §1.5），是權重最大的一項。
評審不會因為「產能達成率 96.8%」而給分，他們要聽的是「一年省多少錢、多久回本、
賣給誰、怎麼收費、憑什麼比歷屆得獎作品強」。

這一層不產生新的量測，它**只做換算**：吃 :class:`~factory_guardian.benchmark.BenchmarkReport`
的實測 KPI，乘上具名的假設參數，推導出年度效益、方案成本、ROI、回收期與破口分析。

四個檔案：

* :mod:`~factory_guardian.business.assumptions` —— 所有假設參數，每個都有名字與依據。
* :mod:`~factory_guardian.business.pricing` —— 提案 §12.1 四種收費方式的級距與成本結構。
* :mod:`~factory_guardian.business.model` —— 年化模型、三情境敏感度、破口分析。
* :mod:`~factory_guardian.business.competitors` —— 與三件歷屆得獎作品的差異化比較。
"""

from .assumptions import ASSUMPTIONS, DISCLAIMER, Assumption, Basis, assumptions_to_list
from .competitors import COMPARISON_AXES, COMPETITORS, competitor_report
from .model import (
    BASE,
    CASES,
    CONSERVATIVE,
    OPTIMISTIC,
    BenefitLine,
    BreakevenPoint,
    BusinessCase,
    ScenarioBenefit,
    SensitivityCase,
    annual_events_by_scenario,
    audit_figures,
    breakeven,
    build_business_case,
    build_business_report,
    incumbent_sensitivity,
    safety_tradeoff,
    scenario_benefit,
)
from .pricing import (
    DEFAULT_PRICES,
    DeploymentScope,
    PriceBook,
    SolutionCost,
    build_solution_cost,
    pilot_scope,
    plant_scope,
)

__all__ = [
    "Assumption",
    "ASSUMPTIONS",
    "Basis",
    "DISCLAIMER",
    "assumptions_to_list",
    "PriceBook",
    "DEFAULT_PRICES",
    "DeploymentScope",
    "SolutionCost",
    "build_solution_cost",
    "pilot_scope",
    "plant_scope",
    "SensitivityCase",
    "CONSERVATIVE",
    "BASE",
    "OPTIMISTIC",
    "CASES",
    "BenefitLine",
    "ScenarioBenefit",
    "BusinessCase",
    "BreakevenPoint",
    "scenario_benefit",
    "annual_events_by_scenario",
    "build_business_case",
    "build_business_report",
    "breakeven",
    "incumbent_sensitivity",
    "safety_tradeoff",
    "audit_figures",
    "COMPETITORS",
    "COMPARISON_AXES",
    "competitor_report",
]
