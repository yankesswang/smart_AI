"""Benchmark：對照組比較（規格 §10.1）。

* Baseline A：只有固定 Threshold 告警，不做跨資料診斷。
* Baseline B：偵測後直接停機，不做生產影響與替代排程。
* Factory Guardian：跨 Machine / Production / Safety 做完整閉環。

三組跑在相同 seed、相同情境、相同總時長的孿生體上，KPI 直接可比。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import Settings, get_settings
from .episode import MODES, EpisodeResult, run_episode
from .twin.scenarios import SCENARIOS, get_scenario

MODE_LABELS = {
    "baseline-a": "Baseline A｜固定門檻告警",
    "baseline-b": "Baseline B｜偵測即停機",
    "guardian": "Factory Guardian｜完整閉環",
}

# 報表欄位：(KPI 欄位, 顯示名稱, 越大越好)
REPORT_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("detection_latency_min", "偵測延遲(min)", False),
    ("time_to_diagnose_min", "診斷確認(min)", False),
    ("diagnosis_correct", "根因正確", True),
    ("diagnosis_confidence", "診斷信心度", True),
    ("false_positive_events", "誤報事件", False),
    ("work_order_completeness_pct", "工單完整度(%)", True),
    ("production_attainment_pct", "產能達成率(%)", True),
    ("production_loss_units", "產能損失(件)", False),
    ("production_loss_ntd", "產能損失(NTD)", False),
    ("max_order_delay_min", "最大交期延遲(min)", False),
    ("late_orders", "延遲訂單數", False),
    ("recovery_min", "復原時間(min)", False),
    # 永續發展性：能源浪費與碳排都是「劣化多耗的電」積分出來的，不是另外估的。
    # 總能耗（energy_kwh）單看會被「停機當然省電」誤導，所以永遠要和
    # 單位產出能耗（energy_intensity_kwh_per_unit）一起看 —— 後者同時吃到分子與分母。
    ("energy_kwh", "總能耗(kWh)", False),
    ("energy_waste_kwh", "劣化浪費電力(kWh)", False),
    ("energy_waste_ntd", "浪費電費(NTD)", False),
    ("co2e_kg", "碳排(kgCO2e)", False),
    ("co2e_waste_kg", "浪費碳排(kgCO2e)", False),
    ("energy_intensity_kwh_per_unit", "單位產出能耗(kWh/件)", False),
    ("unsafe_block_rate_pct", "不安全方案阻擋率(%)", True),
    ("safety_violations_executed", "執行到的違規方案", False),
    ("hazard_exposure_min", "人員危險曝露(min)", False),
    ("machine_health_final", "設備最終健康度", True),
    ("secondary_damage", "二次損壞", False),
    ("human_interventions", "人工介入次數", False),
    ("verification_passed", "執行後驗證通過", True),
)


@dataclass
class BenchmarkRow:
    scenario_id: str
    mode: str
    kpi: dict[str, Any]
    ground_truth: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {"scenario_id": self.scenario_id, "mode": self.mode, "kpi": self.kpi, "ground_truth": self.ground_truth}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BenchmarkRow":
        return cls(
            scenario_id=data["scenario_id"],
            mode=data["mode"],
            kpi=dict(data.get("kpi") or {}),
            ground_truth=dict(data.get("ground_truth") or {}),
        )


@dataclass
class BenchmarkReport:
    rows: list[BenchmarkRow] = field(default_factory=list)
    results: list[EpisodeResult] = field(default_factory=list)

    def by_scenario(self, scenario_id: str) -> dict[str, dict[str, Any]]:
        return {r.mode: r.kpi for r in self.rows if r.scenario_id == scenario_id}

    def scenarios(self) -> list[str]:
        seen: list[str] = []
        for row in self.rows:
            if row.scenario_id not in seen:
                seen.append(row.scenario_id)
        return seen

    def aggregate(self) -> dict[str, dict[str, Any]]:
        """跨情境彙總每個模式的平均表現。"""
        summary: dict[str, dict[str, Any]] = {}
        for mode in MODES:
            mode_rows = [r for r in self.rows if r.mode == mode]
            if not mode_rows:
                continue
            agg: dict[str, Any] = {"episodes": len(mode_rows)}
            for key, _, _ in REPORT_COLUMNS:
                values = [r.kpi.get(key) for r in mode_rows]
                numeric = [float(v) for v in values if isinstance(v, (int, float, bool)) and v is not None]
                agg[key] = round(sum(numeric) / len(numeric), 2) if numeric else None
            # 診斷正確率單獨算：只計有跑診斷的 episode
            correct = [r.kpi.get("diagnosis_correct") for r in mode_rows]
            judged = [c for c in correct if c is not None]
            agg["diagnosis_accuracy_pct"] = round(100.0 * sum(bool(c) for c in judged) / len(judged), 1) if judged else None
            summary[mode] = agg
        return summary

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BenchmarkReport":
        """從 ``to_dict()`` 的輸出重建報表（不含 EpisodeResult）。

        Benchmark 要跑滿 5 情境 × 3 模式，不便宜。商業案例層只需要 KPI 差值，
        所以讓 `--out` 存下來的 JSON 可以直接餵回去，不必為了換一組假設重跑一次模擬。
        """
        return cls(rows=[BenchmarkRow.from_dict(r) for r in data.get("rows", [])])

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "aggregate": self.aggregate(),
            "mode_labels": MODE_LABELS,
            "columns": [{"key": k, "label": label, "higher_is_better": hib} for k, label, hib in REPORT_COLUMNS],
            "disclaimer": "所有數據皆由 Factory Digital Twin 實際模擬執行產生；Sensor / Orders / Manual / History 為合成資料。",
        }


def run_benchmark(
    scenario_ids: Iterable[str] | None = None,
    modes: Iterable[str] = MODES,
    settings: Settings | None = None,
    persist_audit: bool = True,
) -> BenchmarkReport:
    """跑完整對照組並回傳報表。"""
    settings = settings or get_settings()
    scenario_ids = list(scenario_ids) if scenario_ids else list(SCENARIOS)
    report = BenchmarkReport()
    for scenario_id in scenario_ids:
        scenario = get_scenario(scenario_id)
        for mode in modes:
            result = run_episode(
                scenario,
                mode=mode,
                settings=settings,
                persist_audit=persist_audit,
                require_approval=False,      # Benchmark 一律自動核准，讓三組條件一致
            )
            report.results.append(result)
            report.rows.append(
                BenchmarkRow(scenario_id=scenario_id, mode=mode, kpi=result.kpi.to_dict(),
                             ground_truth=result.ground_truth)
            )
    return report


def format_cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "✔" if value else "✘"
    if isinstance(value, float):
        # 小數值要多給一位：單位產出能耗（0.82 vs 0.32 kWh/件）與診斷信心度
        # 在只有一位小數時會被四捨五入成同一個數字，鑑別力就沒了。
        return f"{value:,.2f}" if abs(value) < 10 else f"{value:,.1f}"
    return str(value)


__all__ = ["run_benchmark", "BenchmarkReport", "BenchmarkRow", "REPORT_COLUMNS", "MODE_LABELS", "format_cell"]
