"""商業案例層 —— 把治理層的實測比例換算成年度商業論據(規格 §8)。

決賽評分「商業價值與市場性」占 **40%**,是權重最大的一項。評審不會因為
「HAR 0%、ESB 100%」而給分,他們要聽的是「一年省多少錢、多久回本、賣給誰、
怎麼收費、什麼情況下這個方案不值得買」。

這一層**不產生新的量測**,它只做換算。輸入是兩組實測值:

* :func:`~.measurement.measure_ops` —— ``console.OpsSimulator`` 回填的當班歷史。
  高風險比例、自動放行率、送核准比例**是數出來的**,不是假設(規格 §8.2 明講
  「第一項可從測試集直接量出高風險比例,不需假設」)。
* :func:`~.measurement.measure_harm_prevention` —— ``validation.run_validation()``
  的 B0 與 B3 HAR,誤動作損失避免那一項的乘數。

四個檔案:

* :mod:`~agentgate.business.assumptions` —— 所有假設參數,每個都有名字與依據。
* :mod:`~agentgate.business.measurement` —— 從 console 與 validation 直接量。
* :mod:`~agentgate.business.pricing` —— 規格 §8.1 三段式收費 + 高保證加購 + 中華電信項目。
* :mod:`~agentgate.business.model` —— 年化模型、三情境、破口分析、現況敏感度。

CLI 入口::func:`run_business_case`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .assumptions import (
    ASSUMPTION_DOC,
    Assumption,
    Basis,
    BASIS_LABEL,
    DISCLAIMER,
    static_assumptions,
)
from .measurement import (
    DEFAULT_SEEDS,
    DEFAULT_WINDOW_MINUTES,
    HarmMeasurement,
    OpsMeasurement,
    ShiftSample,
    measure_harm_prevention,
    measure_ops,
)
from .model import (
    BASE,
    CASES,
    CONSERVATIVE,
    COVERAGE_MODES,
    OPTIMISTIC,
    BenefitLine,
    BreakevenPoint,
    BusinessCase,
    ScenarioCase,
    annual_actions,
    audit_figures,
    breakeven,
    build_business_case,
    build_business_report,
    coverage_for,
    harm_expected_annual_ntd,
    incumbent_sensitivity,
    measured_assumptions,
)
from .pricing import (
    DEFAULT_PRICES,
    PlatformCost,
    PriceBook,
    build_platform_cost,
    default_agent_seats,
)


def _fmt(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "不回本"
    return f"{value:,.1f}{suffix}"


def summarize_markdown(report: dict[str, Any]) -> str:
    """把報表壓成終端機看得懂的幾張表(CLI 直接印這個)。"""
    lines: list[str] = ["# AgentGate｜商業案例", "", "## 實測(console 回填當班資料)", ""]
    m = report["measurement"]
    lines += [
        f"- seed × {len(m['seeds'])}、每次回填 {m['window_minutes']} 分鐘,"
        f"合計 {m['actions_total']:,} 件動作",
        f"- 每分鐘動作數 **{m['actions_per_minute']:.2f}**",
        f"- 自動放行率 **{m['auto_pass_rate']['mean']:.1%}**"
        f"({m['auto_pass_rate']['min']:.1%}–{m['auto_pass_rate']['max']:.1%})",
        f"- 送核准比例 **{m['approval_rate']['mean']:.1%}**"
        f"({m['approval_rate']['min']:.1%}–{m['approval_rate']['max']:.1%})",
        f"- 攔截比例 **{m['blocked_rate']['mean']:.1%}**",
        f"- 高風險動作比例(risk ≥ high)**{m['high_risk_share']['mean']:.1%}**",
        f"- 風險 ≥ medium 比例 **{m['review_needed_share']['mean']:.1%}**(現況分層覆核覆蓋率)",
        "",
        "## 三情境",
        "",
        "| 項目 | " + " | ".join(c["case"]["label"] for c in report["cases"]) + " |",
        "|---|" + "---:|" * len(report["cases"]),
    ]
    rows = [
        ("年度動作總數", lambda c: f"{c['volume']['annual_actions']:,.0f}"),
        ("年度效益(NTD)", lambda c: f"{c['result']['annual_benefit_ntd']:,.0f}"),
        ("年度成本(NTD)", lambda c: f"{c['result']['annual_cost_ntd']:,.0f}"),
        ("一次性投入(NTD)", lambda c: f"{c['result']['one_time_ntd']:,.0f}"),
        ("第一年 ROI", lambda c: _fmt(c['result']['year_one_roi_pct'], "%")),
        ("穩態年度 ROI", lambda c: _fmt(c['result']['steady_roi_pct'], "%")),
        ("回收期(月)", lambda c: _fmt(c['result']['payback_months'])),
        ("三年 NPV(8%)", lambda c: f"{c['result']['npv_3y_ntd']:,.0f}"),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(c) for c in report["cases"]) + " |")

    lines += ["", "## 破口分析(基準情境下,讓年度效益 = 年度成本的臨界值)", "",
              "| 參數 | 基準值 | 臨界值 | 結論 |", "|---|---:|---:|---|"]
    for p in report["breakeven"]:
        crit = "—" if p["breakeven_value"] is None else f"{p['breakeven_value']:.4f}"
        lines.append(f"| {p['label']} | {p['base_value']:.4f} | {crit} | {p['verdict']} |")

    lines += ["", "## 最敏感的一根軸:拿什麼當現況", "",
              "| 現況假設 | 覆核覆蓋率 | 等效人力 | 年度效益 | 穩態 ROI |",
              "|---|---:|---:|---:|---:|"]
    for row in report["incumbent_sensitivity"]:
        lines.append(
            f"| {row['label']} | {row['coverage']:.1%} | {row['incumbent_review_fte']:.1f} 人 | "
            f"{row['annual_benefit_ntd']:,.0f} | {_fmt(row['steady_roi_pct'], '%')} |"
        )

    cht = report["cht_revenue"]
    lines += ["", "## 中華電信可辨識收入(年度)", "",
              f"- hicloud 主權雲託管:{cht['hicloud_annual_ntd']:,.0f} NTD",
              f"- 門號級身分驗證:{cht['identity_verification_annual_ntd']:,.0f} NTD"
              f"({cht['identity_verification_calls']:,.0f} 次 × "
              f"{cht['identity_verification_unit_ntd']} NTD)",
              f"- **合計 {cht['total_annual_ntd']:,.0f} NTD/年**",
              "", "> " + report["meta"]["disclaimer"]]
    return "\n".join(lines)


def run_business_case(
    out: str | Path | None = None,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    quiet: bool = False,
) -> dict[str, Any]:
    """CLI 入口:跑量測 → 建報表 → 印摘要 →(可選)寫 JSON。

    ``agentgate business-case [--out business.json]`` 接這一支。
    """
    report = build_business_report(seeds=seeds, window_minutes=window_minutes)
    if not quiet:
        print(summarize_markdown(report))
    if out is not None:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        if not quiet:
            print(f"\n報表已寫入 {path}")
    return report


__all__ = [
    "ASSUMPTION_DOC",
    "BASE",
    "BASIS_LABEL",
    "CASES",
    "CONSERVATIVE",
    "COVERAGE_MODES",
    "DEFAULT_PRICES",
    "DEFAULT_SEEDS",
    "DEFAULT_WINDOW_MINUTES",
    "DISCLAIMER",
    "OPTIMISTIC",
    "Assumption",
    "Basis",
    "BenefitLine",
    "BreakevenPoint",
    "BusinessCase",
    "HarmMeasurement",
    "OpsMeasurement",
    "PlatformCost",
    "PriceBook",
    "ScenarioCase",
    "ShiftSample",
    "annual_actions",
    "audit_figures",
    "breakeven",
    "build_business_case",
    "build_business_report",
    "build_platform_cost",
    "coverage_for",
    "default_agent_seats",
    "harm_expected_annual_ntd",
    "incumbent_sensitivity",
    "measure_harm_prevention",
    "measure_ops",
    "measured_assumptions",
    "run_business_case",
    "static_assumptions",
    "summarize_markdown",
]
