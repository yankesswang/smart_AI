"""Factory Guardian AI 命令列介面 —— 舞台 Demo 的主入口。

    factory-guardian stage                         # 決賽舞台：文件 §5.1 的四分鐘八段劇本
    factory-guardian stage-check --runs 30         # Demo 穩定度：連續 N 次的真實統計
    factory-guardian demo bearing-degradation      # 跑完整閉環（含互動核准）
    factory-guardian scenarios                     # 列出情境與故障模型
    factory-guardian topology                      # 印出工廠拓撲與 Knowledge Graph
    factory-guardian episode bearing-degradation --mode guardian
    factory-guardian benchmark                     # 三組對照組 KPI 比較
    factory-guardian business-case                 # 由實測 KPI 推導年度 ROI 與回收期
    factory-guardian serve                         # 啟動 Dashboard
    factory-guardian audit runs/audit-*.jsonl      # 讀回稽核軌跡
    factory-guardian knowledge "振動上升 溫度上升"   # 測試 RAG 檢索
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import DATA_DISCLAIMER, __version__
from .audit import load_audit, summarize_audit
from .benchmark import MODE_LABELS, REPORT_COLUMNS, BenchmarkReport, format_cell, run_benchmark
from .business import build_business_report, pilot_scope, plant_scope
from .business.pricing import STREAM_LABELS
from .config import get_settings
from .domain import ApprovalDecision, RecoveryPlan, SafetyVerdictKind
from .episode import MODES, run_episode
from .knowledge.retriever import default_kb
from .policy.engine import PolicyDecision
from .stage import SPEEDS, StageDirector, StageRenderer, render_reliability, run_reliability, script_dict
from .twin.faults import FAULTS
from .twin.scenarios import SCENARIOS, get_scenario
from .twin.topology import build_factory

console = Console()

VERDICT_STYLE = {"PASS": "green", "APPROVAL_REQUIRED": "yellow", "BLOCK": "red"}


# --------------------------------------------------------------------------------------
# 顯示元件
# --------------------------------------------------------------------------------------
def _banner() -> None:
    console.print(
        Panel(
            Text.from_markup(
                "[bold]FACTORY GUARDIAN AI[/bold]  ·  Agentic AI 智慧工廠自主營運與風險管理平台\n"
                f"[dim]v{__version__}｜{DATA_DISCLAIMER}[/dim]"
            ),
            border_style="cyan",
        )
    )


def _plan_table(plans: list[dict[str, Any]], title: str = "方案比較（排名由規則引擎計算，非 LLM）") -> Table:
    table = Table(title=title, header_style="bold", show_lines=False)
    # 方案名稱是唯一可伸縮的欄位，數字欄位固定寬度，
    # 這樣終端機不夠寬時 Rich 只會縮方案名稱，不會把整張表的數字都截成「…」。
    table.add_column("#", justify="right", width=3, no_wrap=True)
    table.add_column("方案", min_width=16, overflow="ellipsis", no_wrap=True)
    table.add_column("產能", justify="right", width=5, no_wrap=True)
    table.add_column("延遲", justify="right", width=6, no_wrap=True)
    table.add_column("復原", justify="right", width=6, no_wrap=True)
    table.add_column("成本", justify="right", width=8, no_wrap=True)
    table.add_column("工安", width=5, no_wrap=True)
    table.add_column("分數", justify="right", width=5, no_wrap=True)
    short = {"PASS": "PASS", "APPROVAL_REQUIRED": "APPR", "BLOCK": "BLOCK"}
    for plan in sorted(plans, key=lambda p: p["rank"]):
        proj = plan["projection"]
        verdict = plan["safety"]["verdict"] if plan["safety"] else "—"
        style = "dim strike" if not plan["feasible"] else ("bold cyan" if plan["rank"] == 1 else "")
        table.add_row(
            str(plan["rank"]),
            f"{plan['plan_id']}｜{plan['title']}",
            f"{proj['production_pct']:.0f}%",
            f"{proj['max_order_delay_min']:.0f}m",
            f"{proj['recovery_min']:.0f}m" if proj["recovered"] else "未復原",
            f"{proj['cost_ntd']:,.0f}",
            Text(short.get(verdict, verdict), style=VERDICT_STYLE.get(verdict, "")),
            f"{plan['score']:.3f}" if plan["feasible"] else "—",
            style=style,
        )
    return table


def _kpi_line(kpi: dict[str, Any]) -> str:
    def g(key: str, suffix: str = "", digits: int = 0) -> str:
        value = kpi.get(key)
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "✔" if value else "✘"
        return f"{value:.{digits}f}{suffix}"

    return (
        f"偵測 {g('detection_latency_min', ' min')}｜診斷 {g('time_to_diagnose_min', ' min')}"
        f"｜根因正確 {g('diagnosis_correct')}｜產能達成 {g('production_attainment_pct', '%', 1)}"
        f"｜最大延遲 {g('max_order_delay_min', ' min')}｜危險曝露 {g('hazard_exposure_min', ' min')}"
        f"｜驗證 {g('verification_passed')}"
    )


def _interactive_approval(plan: RecoveryPlan, decision: PolicyDecision) -> ApprovalDecision:
    """CLI 的 Human-in-the-loop：高風險動作停下來等人。"""
    proj = plan.projection
    console.print(
        Panel(
            Text.from_markup(
                f"[bold yellow]需要人工核准[/bold yellow]（風險等級 {decision.risk}）\n"
                f"方案：[bold]{plan.plan_id}｜{plan.title}[/bold]\n"
                f"動作：{' → '.join(a.kind.value for a in plan.actions)}\n"
                f"預測：產能 {proj.production_pct:.0f}%｜最大延遲 {proj.max_order_delay_min:.0f} 分鐘"
                f"｜復原 {proj.recovery_min:.0f} 分鐘｜成本 {proj.cost_ntd:,.0f} NTD\n"
                f"政策依據：{'；'.join(decision.reasons)}"
            ),
            border_style="yellow",
        )
    )
    if not sys.stdin.isatty():
        console.print("[dim]非互動終端，依安全原則不自動核准。[/dim]")
        return ApprovalDecision(plan.plan_id, False, "non-interactive", "非互動環境，未取得核准。")
    try:
        answer = console.input("[bold]核准執行？[/bold] (y/N) ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    approved = answer in {"y", "yes"}
    return ApprovalDecision(
        plan.plan_id, approved, "cli-operator",
        "現場主管核准" if approved else "現場主管退回", auto=False,
    )


# --------------------------------------------------------------------------------------
# 指令
# --------------------------------------------------------------------------------------
def cmd_scenarios(_: argparse.Namespace) -> int:
    _banner()
    table = Table(title="Demo 情境", header_style="bold")
    table.add_column("ID"); table.add_column("標題"); table.add_column("視野", justify="right")
    table.add_column("注入"); table.add_column("說明", width=44)
    for scenario in SCENARIOS.values():
        table.add_row(
            scenario.scenario_id, scenario.title, f"{scenario.horizon_ticks}t",
            "、".join(f"{i.fault_id}@{i.machine_id}" for i in scenario.injections),
            scenario.description,
        )
    console.print(table)

    fault_table = Table(title="故障模型（感測器指紋）", header_style="bold")
    fault_table.add_column("fault_id"); fault_table.add_column("名稱"); fault_table.add_column("指紋", width=46)
    fault_table.add_column("手冊"); fault_table.add_column("工時", justify="right")
    for model in FAULTS.values():
        fault_table.add_row(model.fault_id, model.label, model.signature_note,
                            "、".join(model.manual_refs), f"{model.repair_min:g}m")
    console.print(fault_table)
    console.print("[dim]Ground Truth 只用於 Benchmark 評分，不會進入任何 Agent 的輸入。[/dim]")
    return 0


def cmd_topology(_: argparse.Namespace) -> int:
    _banner()
    topo = build_factory()
    table = Table(title="工廠拓撲", header_style="bold")
    table.add_column("機台"); table.add_column("名稱"); table.add_column("額定", justify="right")
    table.add_column("產品"); table.add_column("下游"); table.add_column("替代機台")
    for mid, machine in topo.machines.items():
        table.add_row(mid, machine.name, f"{machine.rated_rate_uph:.0f} 件/hr",
                      "、".join(machine.products), "、".join(topo.downstream(mid)) or "—",
                      "、".join(topo.alternates(mid)) or "—")
    console.print(table)

    graph = topo.graph_json()
    console.print(f"[bold]Knowledge Graph[/bold]：{len(graph['nodes'])} 節點 / {len(graph['edges'])} 邊")
    kinds: dict[str, int] = {}
    for node in graph["nodes"]:
        kinds[node.get("kind", "?")] = kinds.get(node.get("kind", "?"), 0) + 1
    console.print("  " + "、".join(f"{k}×{v}" for k, v in kinds.items()))

    order_table = Table(title="訂單（MES-like synthetic）", header_style="bold")
    order_table.add_column("訂單"); order_table.add_column("產品"); order_table.add_column("數量", justify="right")
    order_table.add_column("交期", justify="right"); order_table.add_column("優先"); order_table.add_column("機台")
    for order in topo.initial_orders[:8]:
        order_table.add_row(order.order_id, order.product_id, str(order.quantity),
                            f"{order.due_in_min:.0f}m", f"P{order.priority}", order.assigned_machine or "—")
    console.print(order_table)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    _banner()
    scenario = get_scenario(args.scenario)
    console.print(Panel(f"[bold]{scenario.title}[/bold]\n{scenario.description}", border_style="cyan"))

    settings = get_settings()
    console.print(f"[dim]設定：{settings.describe()}[/dim]\n")

    seen: set[str] = set()

    def on_stage(stage: str, payload: dict[str, Any]) -> None:
        if stage == "tick":
            return
        if stage == "detect":
            event = payload["event"]
            console.rule(f"[bold red]1. DETECT[/bold red]  {event['event_id']}")
            console.print(f"  機台 {event['machine_id']}｜嚴重度 [bold]{event['severity'].upper()}[/bold]"
                          f"｜tick {event['tick']}｜偵測器 {event['detector']}")
            console.print(f"  觸發條件：{'、'.join(event['triggers'])}")
        elif stage == "confirm" and payload.get("waited_ticks"):
            console.print(f"  [yellow]證據不足，續觀察 {payload['waited_ticks']} 分鐘後重新診斷[/yellow]")
        elif stage == "diagnose":
            diagnosis = payload["diagnosis"]
            console.rule("[bold]2. DIAGNOSE[/bold]")
            for cand in diagnosis["candidates"][:3]:
                mark = "→" if cand["fault_id"] == diagnosis["top_fault_id"] else " "
                console.print(f"  {mark} {cand['label']}  [bold cyan]{cand['confidence']:.0%}[/bold cyan]")
            console.print(f"\n[dim]{diagnosis['narrative']}[/dim]\n")
            top = diagnosis["candidates"][0]
            for evidence in top["evidence"][:4]:
                console.print(f"    [dim]· [{evidence['source']}] {evidence['reference']}：{evidence['statement'][:90]}[/dim]")
        elif stage == "impact":
            console.rule("[bold]3. IMPACT[/bold]")
            console.print(f"[dim]{payload['impact']['narrative']}[/dim]")
        elif stage == "plan":
            console.rule("[bold]4. PLAN[/bold]")
            console.print(f"  產生 {len(payload['plans'])} 個候選方案，投影由 Simulator 乾跑計算。")
        elif stage == "safety":
            console.rule("[bold]5. SAFETY[/bold]")
            for review in payload["reviews"]:
                style = VERDICT_STYLE.get(review["verdict"], "")
                console.print(f"  {review['plan_id']}: [{style}]{review['verdict']}[/{style}]")
                for finding in review["findings"]:
                    console.print(f"      [dim]{finding['rule_id']} {finding['message'][:100]}[/dim]")
        elif stage == "rank":
            console.rule("[bold]6. RANK[/bold]")
            console.print(payload["explanation"])
        elif stage == "work_order":
            work_order = payload["work_order"]
            console.print(f"\n  [bold]維修工單[/bold] {work_order['work_order_id']}｜{work_order['priority']}"
                          f"｜{work_order['required_skill']}｜{work_order['estimated_repair_min']:.0f} 分鐘"
                          f"｜完整度 {work_order['completeness_pct']:.0f}%")
            console.print(f"  [dim]零件：{'、'.join(work_order['suggested_parts'])}｜SOP：{'、'.join(work_order['sop_refs'])}[/dim]")
        elif stage == "execute":
            console.rule("[bold]7. EXECUTE[/bold]")
            for effect in payload["effects"]:
                icon = "✔" if effect["ok"] else "✘"
                console.print(f"  {icon} {effect['message']}")
        elif stage == "verify":
            report = payload["report"]
            console.rule("[bold]8. VERIFY[/bold]")
            for check in report["checks"]:
                icon = "[green]PASS[/green]" if check["passed"] else "[red]FAIL[/red]"
                console.print(f"  {icon} {check['name']}：預期 {check['expected']:.1f} {check['comparator']} 實測 {check['actual']:.1f}")
            console.print(f"\n[dim]{report['narrative']}[/dim]")
        elif stage == "escalate" and stage not in seen:
            console.print(Panel(f"[yellow]{payload['reason']}[/yellow]", title="ESCALATE", border_style="yellow"))
        seen.add(stage)

    approval = None if args.auto_approve else _interactive_approval
    result = run_episode(
        scenario,
        mode=args.mode,
        approval=approval,
        on_stage=on_stage,
        require_approval=not args.auto_approve,
    )

    data = result.to_dict()
    if data["loop"] and data["loop"]["plans"]:
        console.print()
        console.print(_plan_table(data["loop"]["plans"]))

    console.rule("[bold cyan]EPISODE KPI[/bold cyan]")
    console.print(_kpi_line(data["kpi"]))
    console.print(f"[dim]Ground Truth：{data['ground_truth']}（僅用於評分）[/dim]")
    console.print(f"[dim]稽核軌跡：{data['audit_path']}[/dim]")
    if args.json:
        console.print_json(json.dumps(data, ensure_ascii=False, default=str))
    return 0


def cmd_episode(args: argparse.Namespace) -> int:
    scenario = get_scenario(args.scenario)
    result = run_episode(scenario, mode=args.mode, require_approval=False)
    data = result.to_dict()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return 0
    _banner()
    console.print(f"[bold]{scenario.title}[/bold]｜模式 {args.mode}")
    console.print(_kpi_line(data["kpi"]))
    if data["loop"] and data["loop"]["plans"]:
        console.print(_plan_table(data["loop"]["plans"]))
    console.print(f"[dim]稽核軌跡：{data['audit_path']}[/dim]")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    _banner()
    scenario_ids = args.scenarios or None
    console.print("[dim]執行三組對照組（Baseline A / Baseline B / Factory Guardian），"
                  "相同 seed、相同情境、相同總時長…[/dim]\n")
    report = run_benchmark(scenario_ids=scenario_ids, persist_audit=not args.no_audit)

    for scenario_id in report.scenarios():
        data = report.by_scenario(scenario_id)
        modes = [m for m in MODES if m in data]
        table = Table(title=f"{scenario_id}", header_style="bold")
        table.add_column("KPI", width=24)
        for mode in modes:
            table.add_column(MODE_LABELS[mode].split("｜")[0], justify="right")
        for key, label, higher_is_better in REPORT_COLUMNS:
            values = [data[m].get(key) for m in modes]
            numeric = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
            best = (max(numeric) if higher_is_better else min(numeric)) if numeric else None
            cells = []
            for value in values:
                text = format_cell(value)
                if best is not None and isinstance(value, (int, float)) and not isinstance(value, bool) and value == best:
                    cells.append(Text(text, style="bold cyan"))
                else:
                    cells.append(Text(text))
            table.add_row(label, *cells)
        console.print(table)

    console.rule("[bold]跨情境彙總[/bold]")
    summary = Table(header_style="bold")
    summary.add_column("模式", width=26)
    summary.add_column("診斷正確率", justify="right")
    summary.add_column("產能達成率", justify="right")
    summary.add_column("最大延遲", justify="right")
    summary.add_column("危險曝露", justify="right")
    summary.add_column("二次損壞率", justify="right")
    # 永續：浪費碳排是「劣化多耗的電」換算來的，單位能耗則同時反映多耗的電與少做的件。
    summary.add_column("浪費碳排", justify="right")
    summary.add_column("單位能耗", justify="right")
    for mode, agg in report.aggregate().items():
        summary.add_row(
            MODE_LABELS[mode],
            f"{agg['diagnosis_accuracy_pct']:.0f}%" if agg["diagnosis_accuracy_pct"] is not None else "—",
            f"{agg['production_attainment_pct']:.1f}%",
            f"{agg['max_order_delay_min']:.0f}m",
            f"{agg['hazard_exposure_min']:.0f}m",
            f"{agg['secondary_damage'] * 100:.0f}%",
            f"{agg['co2e_waste_kg']:.2f}kg",
            f"{agg['energy_intensity_kwh_per_unit']:.2f}",
        )
    console.print(summary)
    console.print(f"[dim]{report.to_dict()['disclaimer']}[/dim]")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2, default=str)
        console.print(f"[dim]報表已輸出：{args.out}[/dim]")

    if getattr(args, "business", False):
        console.print()
        _render_business(build_business_report(report, scope=plant_scope()))
    return 0


# --------------------------------------------------------------------------------------
# 商業案例
# --------------------------------------------------------------------------------------
def _ntd(value: Any, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.{digits}f}"


def _pct(value: Any, digits: int = 1) -> str:
    return "—" if value is None else f"{float(value):,.{digits}f}%"


def _assumption_value(value: float) -> str:
    """假設值的顯示格式。

    ``,.4g`` 會把 500000 印成 ``5e+05`` —— 評審在紙本上讀到科學記號會直接卡住，
    所以大數走千分位，小數才保留有效位數。
    """
    magnitude = abs(float(value))
    if magnitude >= 1000:
        return f"{value:,.0f}"
    if magnitude >= 1:
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{value:,.4g}"


def _render_business(data: dict[str, Any]) -> None:
    """把商業案例報表印成評審看得懂的表格。"""
    meta = data["meta"]
    console.print(Panel(
        Text.from_markup(
            f"[bold]{meta['title']}[/bold]\n"
            f"[dim]{meta['roi_formula']}\n來源：{meta['roi_source']}[/dim]\n\n"
            f"效益來自：{meta['kpi_source']}\n"
            f"情境：{'、'.join(meta['scenarios'])}\n\n"
            f"[yellow]刻意不計入[/yellow]：{meta['not_counted']}"
        ),
        border_style="cyan",
    ))

    # --- 假設表 ------------------------------------------------------------------------
    table = Table(title="假設參數（每一個都有名字、單位與依據）", header_style="bold")
    table.add_column("參數", width=26, overflow="ellipsis")
    table.add_column("值", justify="right", width=11, no_wrap=True)
    table.add_column("單位", width=13, no_wrap=True)
    table.add_column("依據", width=4, no_wrap=True)
    table.add_column("來源／推估過程", overflow="fold")
    for item in data["assumptions"]:
        style = {"實測": "cyan", "推導": "", "假設": "yellow"}.get(item["basis_label"], "")
        table.add_row(
            item["label"], _assumption_value(item["value"]), item["unit"],
            Text(item["basis_label"], style=style), item["source"],
        )
    console.print(table)

    cases = {c["case"]["key"]: c for c in data["cases"]}
    base = cases.get("base") or data["cases"][0]

    # --- 效益拆解（基準情境）------------------------------------------------------------
    rollup = base["benefit_rollup"]
    table = Table(
        title=f"年度效益拆解｜{base['case']['label']}情境｜{base['scope']['label']}"
              f"（{base['scope']['lines']} 線 × {base['scope']['machines_per_line']} 台）",
        header_style="bold",
    )
    table.add_column("效益項", width=26)
    table.add_column("追溯的實測 KPI 欄位", width=42, overflow="fold")
    table.add_column("年度金額 NTD", justify="right", width=13)
    line_index = {ln["key"]: ln for s in base["scenarios"] for ln in s["lines"]}
    for key, amount in rollup.items():
        if key == "basis" or not key.endswith("_ntd"):
            continue
        benefit_key = key[: -len("_ntd")]
        line = line_index.get(benefit_key, {})
        table.add_row(
            line.get("label", benefit_key),
            "、".join(line.get("kpi_fields", [])) or "—",
            Text(_ntd(amount), style="red" if amount < 0 else ""),
        )
    total = sum(v for k, v in rollup.items() if k != "basis" and k.endswith("_ntd"))
    table.add_row(Text("合計", style="bold"), "", Text(_ntd(total), style="bold cyan"))
    console.print(table)

    # --- 每情境 -------------------------------------------------------------------------
    table = Table(title="各情境的年度事件數與效益（每條產線）", header_style="bold")
    table.add_column("情境", width=24)
    table.add_column("故障", width=22)
    table.add_column("次/年", justify="right", width=6)
    table.add_column("視野縮放", justify="right", width=8)
    table.add_column("每次 NTD", justify="right", width=11)
    table.add_column("年度 NTD", justify="right", width=12)
    for scenario in base["scenarios"]:
        amount = scenario["annual_ntd"]
        table.add_row(
            scenario["scenario_id"], scenario["fault_id"],
            f"{scenario['annual_events']:.2f}", f"{scenario['duration_scale']:.3f}",
            _ntd(scenario["per_event_ntd"]),
            Text(_ntd(amount), style="red" if amount < 0 else ""),
        )
    console.print(table)

    # --- 成本結構 -----------------------------------------------------------------------
    table = Table(title="方案成本結構（提案 §12.1 四種收費方式 ＋ 中華電信網路與邊緣）", header_style="bold")
    table.add_column("收費方式", width=34)
    table.add_column("一次性 NTD", justify="right", width=12)
    table.add_column("年度 NTD", justify="right", width=12)
    for stream, amounts in base["cost"]["by_stream"].items():
        table.add_row(
            STREAM_LABELS.get(stream, stream),
            _ntd(amounts["one_time_ntd"]) if amounts["one_time_ntd"] else "—",
            _ntd(amounts["annual_ntd"]) if amounts["annual_ntd"] else "—",
        )
    totals = base["cost"]["totals"]
    table.add_row(
        Text("合計", style="bold"),
        Text(_ntd(totals["one_time_ntd"]), style="bold"),
        Text(_ntd(totals["annual_recurring_ntd"]), style="bold"),
    )
    console.print(table)

    # --- 三情境 -------------------------------------------------------------------------
    table = Table(title="敏感度分析：保守／基準／樂觀（基準情境刻意偏保守）", header_style="bold")
    table.add_column("項目", width=22)
    for case in data["cases"]:
        table.add_column(case["case"]["label"], justify="right", width=14)
    rows: list[tuple[str, Any]] = [
        ("現況告警逃逸率", lambda c: f"{c['case']['escalation_rate']:.0%}"),
        ("效益實現率", lambda c: f"{c['case']['realization']:.0%}"),
        ("事件頻率倍率", lambda c: f"{c['case']['frequency_multiplier']:.2f}x"),
        ("方案成本倍率", lambda c: f"{c['case']['cost_multiplier']:.2f}x"),
        ("年度效益 NTD", lambda c: _ntd(c["result"]["annual_benefit_ntd"])),
        ("年度成本 NTD", lambda c: _ntd(c["result"]["annual_cost_ntd"])),
        ("一次性投入 NTD", lambda c: _ntd(c["result"]["one_time_cost_ntd"])),
        ("第一年 ROI", lambda c: _pct(c["result"]["year_one_roi_pct"])),
        ("穩態年度 ROI", lambda c: _pct(c["result"]["steady_roi_pct"])),
        ("回收期（月）", lambda c: "不回本" if c["result"]["payback_months"] is None
         else f"{c['result']['payback_months']:.1f}"),
        ("三年 NPV NTD", lambda c: _ntd(c["result"]["npv_3y_ntd"])),
    ]
    for label, fn in rows:
        cells = []
        for case in data["cases"]:
            text = fn(case)
            style = "red" if text.startswith("-") or text == "不回本" else ""
            if case["case"]["key"] == "base" and not style:
                style = "bold cyan"
            cells.append(Text(text, style=style))
        table.add_row(label, *cells)
    console.print(table)

    # --- 導入起點 -----------------------------------------------------------------------
    entry = data["entry_deployment"]
    console.print(Panel(
        Text.from_markup(
            f"[bold]{entry['scope']['label']}[/bold]（基準情境）"
            f"｜年度效益 {_ntd(entry['result']['annual_benefit_ntd'])}"
            f"｜年度成本 {_ntd(entry['result']['annual_cost_ntd'])}"
            f"｜穩態 ROI [bold]{_pct(entry['result']['steady_roi_pct'])}[/bold]\n"
            f"[dim]{entry['scope']['note']}[/dim]"
        ),
        border_style="yellow",
    ))

    # --- 破口分析 -----------------------------------------------------------------------
    table = Table(title="破口分析：在什麼假設下 ROI 不成立（基準情境、穩態）", header_style="bold")
    table.add_column("參數", width=22)
    table.add_column("基準值", justify="right", width=9)
    table.add_column("臨界值", justify="right", width=9)
    table.add_column("結論", overflow="fold")
    for point in data["breakeven"]:
        table.add_row(
            point["label"], _assumption_value(point["base_value"]),
            "—" if point["breakeven_value"] is None else _assumption_value(point["breakeven_value"]),
            point["verdict"],
        )
    console.print(table)

    # --- 工安取捨 -----------------------------------------------------------------------
    safety = data.get("safety_tradeoff")
    if safety:
        console.print(Panel(
            Text.from_markup(
                f"[bold]工安取捨（{safety['scenario_id']}，對照 Baseline A）[/bold]\n"
                f"移除 [bold]{safety['exposure_avoided_min']:.0f} 分鐘[/bold]人員危險曝露，"
                f"放棄 [bold]{_ntd(safety['production_cost_ntd'])} NTD[/bold] 邊際貢獻"
                f"　→　每分鐘曝露 [bold]{safety['ntd_per_exposure_minute']:.1f} NTD[/bold]\n"
                f"在假設的致傷機率下期望損失只有 {_ntd(safety['expected_loss_avoided_ntd'])} NTD，"
                f"淨額 [red]{_ntd(safety['net_ntd'])} NTD[/red]。\n"
                f"要打平需致傷機率 ≥ [bold]{safety['breakeven_injury_probability_per_hour']:.2%}/曝露小時[/bold]"
                f"，或單次事故成本 ≥ [bold]{_ntd(safety['breakeven_incident_cost_ntd'])} NTD[/bold]。\n"
                f"[yellow]{safety['stance']}[/yellow]"
            ),
            border_style="red",
        ))

    # --- 現況假設敏感度 ------------------------------------------------------------------
    table = Table(title="最敏感的一根軸：拿什麼當「現況」（其餘參數固定為基準情境）", header_style="bold")
    table.add_column("現況假設", width=36)
    table.add_column("年度效益 NTD", justify="right", width=13)
    table.add_column("穩態 ROI", justify="right", width=10)
    table.add_column("回收期", justify="right", width=8)
    for row in data["incumbent_sensitivity"]:
        table.add_row(
            row["label"], _ntd(row["annual_benefit_ntd"]), _pct(row["steady_roi_pct"]),
            "不回本" if row["payback_months"] is None else f"{row['payback_months']:.1f} 月",
        )
    console.print(table)

    # --- 競品 ---------------------------------------------------------------------------
    competitors = data["competitors"]
    table = Table(title="競品差異化（對手皆為同一評審體系選出的歷屆得獎作品）", header_style="bold")
    table.add_column("作品", width=20)
    table.add_column("年／獎項", width=13, no_wrap=True)
    table.add_column("重疊", width=30, overflow="fold")
    table.add_column("我們的避讓", overflow="fold")
    for item in competitors["competitors"]:
        table.add_row(item["name"], f"{item['year']}｜{item['award']}", item["overlap"], item["avoidance"])
    console.print(table)

    table = Table(title="比較軸（我們這一欄都指得到 repo 裡的位置）", header_style="bold")
    table.add_column("軸", width=18)
    table.add_column("歷屆作品（依公開資料）", width=34, overflow="fold")
    table.add_column("Factory Guardian", width=44, overflow="fold")
    table.add_column("可驗證位置", width=34, overflow="fold")
    for axis in competitors["axes"]:
        table.add_row(axis["axis"], axis["them"], axis["ours"], axis["evidence"])
    console.print(table)
    console.print(f"[bold]定位[/bold]：{competitors['positioning']}")
    console.print(f"[yellow]{competitors['risk']}[/yellow]")
    console.print(f"\n[dim]{meta['disclaimer']}[/dim]")


def cmd_business_case(args: argparse.Namespace) -> int:
    _banner()
    if args.from_json:
        with open(args.from_json, encoding="utf-8") as fh:
            report = BenchmarkReport.from_dict(json.load(fh))
        console.print(f"[dim]沿用既有 Benchmark 結果：{args.from_json}[/dim]\n")
    else:
        console.print("[dim]先跑三組對照組取得實測 KPI（相同 seed、相同情境、相同總時長）…[/dim]\n")
        report = run_benchmark(scenario_ids=args.scenarios or None, persist_audit=not args.no_audit)

    scope = pilot_scope() if args.scope == "pilot" else plant_scope(args.lines)
    data = build_business_report(report, scope=scope)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return 0

    _render_business(data)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
        console.print(f"[dim]商業案例報表已輸出：{args.out}[/dim]")
    return 0


# --------------------------------------------------------------------------------------
# 舞台 Demo（研究文件 §5.1 的四分鐘八段劇本）
# --------------------------------------------------------------------------------------
def cmd_stage(args: argparse.Namespace) -> int:
    """決賽現場那四分鐘。``--speed fast`` 則是同一齣戲的快速驗證。"""
    if args.script:
        data = script_dict()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        # 刻意不用表格：口白是整段文字，塞進欄位只會被壓成一行八個字，背稿的人讀不了。
        _banner()
        console.print(f"[bold]{data['source']}[/bold]　[dim]共 {data['total_seconds']:.0f} 秒 / "
                      f"{len(data['acts'])} 段[/dim]\n")
        for act in data["acts"]:
            console.rule(f"[bold]{act['window']}　{act['act_id']}　{act['screen']}[/bold]")
            console.print(f"  [dim]§5.1 要證明：[/dim]{act['doc_claim']}")
            console.print(f"  [dim]實作證明：[/dim][cyan]{act['proves']}[/cyan]")
            console.print(f"  [dim]台上口白：[/dim]{act['cue']}")
            console.print(f"  [dim]念這些欄位：{'、'.join(act['metrics']) or '—'}[/dim]")
            console.print()
        return 0

    scenario = get_scenario(args.scenario)
    renderer = StageRenderer(console=console, show_cue=not args.no_cue, quiet=args.json)
    if not args.json:
        _banner()

    director = StageDirector(
        scenario_id=args.scenario,
        speed=args.speed,
        offline=args.offline,
        approval=_interactive_approval if args.manual_approval else None,
        observer=renderer.act,
        persist_audit=not args.no_audit,
        counterfactual=not args.no_counterfactual,
    )
    renderer.opening(scenario.title, director.speed, director.offline, director.settings.describe())
    run = director.run()
    renderer.closing(run)

    if args.json:
        print(json.dumps(run.to_dict(), ensure_ascii=False, indent=2, default=str))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(run.to_dict(), fh, ensure_ascii=False, indent=2, default=str)
        if not args.json:
            console.print(f"[dim]舞台記錄已輸出：{args.out}[/dim]")
    return 0 if run.ok else 1


def cmd_stage_check(args: argparse.Namespace) -> int:
    """連續跑 N 次，回答評審的「你的 Demo 穩定嗎」（研究文件 §5.3）。"""
    _banner()
    mode = "最壞情況：無金鑰 ＋ 廠區對外鏈路中斷" if args.offline else "一般情況"
    console.print(f"[dim]連續執行 {args.runs} 次完整劇本（{args.scenario}｜{mode}）…[/dim]")

    with console.status("[dim]執行中…[/dim]") as status:
        def progress(outcome: Any) -> None:
            mark = "✔" if outcome.ok else "✘"
            status.update(f"[dim]{mark} 第 {outcome.index}/{args.runs} 次｜{outcome.seconds:.3f}s"
                          f"｜指紋 {outcome.fingerprint_hash}[/dim]")

        report = run_reliability(
            runs=args.runs,
            scenario_id=args.scenario,
            offline=args.offline,
            on_run=progress,
        )

    render_reliability(report, console=console)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2, default=str)
        console.print(f"[dim]穩定度報表已輸出：{args.out}[/dim]")
    if args.json:
        console.print_json(json.dumps(report.to_dict(), ensure_ascii=False, default=str))

    ok = report.failures == 0 and report.decisions_consistent
    return 0 if ok else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    _banner()
    console.print(f"[bold]Dashboard[/bold] → http://{args.host}:{args.port}")
    uvicorn.run("factory_guardian.api.server:app", host=args.host, port=args.port, reload=args.reload, log_level="warning")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    paths: list[str] = []
    for pattern in args.paths:
        paths.extend(sorted(glob.glob(pattern)))
    if not paths:
        console.print("[red]找不到符合的稽核檔。[/red]")
        return 1
    for path in paths:
        records = load_audit(path)
        summary = summarize_audit(records)
        console.rule(f"[bold]{path}[/bold]")
        console.print(f"{summary['records']} 筆｜{summary['first_ts']} → {summary['last_ts']}")
        stage_table = Table(header_style="bold")
        stage_table.add_column("stage"); stage_table.add_column("次數", justify="right")
        for stage, count in sorted(summary["stages"].items(), key=lambda kv: -kv[1]):
            stage_table.add_row(stage, str(count))
        console.print(stage_table)
        if args.verbose:
            for record in records:
                console.print(f"[dim]{record['ts']}[/dim] [cyan]{record['stage']}[/cyan] "
                              f"[dim]{record['actor']}[/dim] {json.dumps(record['detail'], ensure_ascii=False)[:220]}")
    return 0


def cmd_knowledge(args: argparse.Namespace) -> int:
    kb = default_kb()
    _banner()
    console.print(f"[dim]知識庫：{kb.stats()}[/dim]\n")
    hits = kb.search(args.query, top_k=args.top_k)
    if not hits:
        console.print("[yellow]沒有命中任何段落。[/yellow]")
        return 0
    for hit in hits:
        console.print(f"[bold cyan]{hit.score:.3f}[/bold cyan] [bold]{hit.chunk.ref}[/bold] "
                      f"[dim]({hit.chunk.source})[/dim] {hit.chunk.title}")
        console.print(f"  [dim]{hit.chunk.text[:200]}[/dim]\n")
    affinity = kb.fault_affinity(args.query, list(FAULTS))
    console.print("文件支持度：" + "、".join(f"{k} {v:.2f}" for k, v in sorted(affinity.items(), key=lambda kv: -kv[1])))
    return 0


# --------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory-guardian", description="Factory Guardian AI 競賽 Demo")
    parser.add_argument("--version", action="version", version=f"factory-guardian {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("stage", help="決賽舞台：研究文件 §5.1 的四分鐘八段閉環劇本")
    p.add_argument("--scenario", default="bearing-degradation", choices=list(SCENARIOS))
    p.add_argument("--speed", default="live",
                   help=f"節奏：{'、'.join(SPEEDS)}，或直接給倍率（例如 0.5）。live=照劇本四分鐘，fast=不等待")
    p.add_argument("--offline", action="store_true",
                   help="最壞情況：不用金鑰且模擬廠區對外鏈路中斷，並量測離線備援時間")
    p.add_argument("--manual-approval", action="store_true", help="A6 停下來真的等人按（預設為腳本化核准）")
    p.add_argument("--no-cue", action="store_true", help="不印台上口白提示（正式演出時畫面更乾淨）")
    p.add_argument("--no-counterfactual", action="store_true", help="不跑 Baseline A 對照組（A8 的避免損失會留白）")
    p.add_argument("--no-audit", action="store_true", help="不寫稽核檔")
    p.add_argument("--script", action="store_true", help="只印劇本本身，不執行")
    p.add_argument("--out", help="輸出舞台記錄 JSON 路徑")
    p.add_argument("--json", action="store_true", help="只輸出 JSON")
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("stage-check", help="Demo 穩定度：連續 N 次的成功率、耗時分佈與決策一致性")
    p.add_argument("--runs", type=int, default=30, help="連續執行次數（預設 30）")
    p.add_argument("--scenario", default="bearing-degradation", choices=list(SCENARIOS))
    p.add_argument("--offline", action="store_true", help="在最壞情況下量（無金鑰＋斷網），並統計離線備援時間")
    p.add_argument("--out", help="輸出穩定度報表 JSON 路徑")
    p.add_argument("--json", action="store_true", help="附帶輸出完整 JSON")
    p.set_defaults(func=cmd_stage_check)

    p = sub.add_parser("demo", help="跑完整 Agent 閉環（舞台 Demo）")
    p.add_argument("scenario", nargs="?", default="bearing-degradation", choices=list(SCENARIOS))
    p.add_argument("--mode", default="guardian", choices=list(MODES))
    p.add_argument("--auto-approve", action="store_true", help="不停下來等人工核准（腳本化 Demo）")
    p.add_argument("--json", action="store_true", help="附帶輸出完整 JSON")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("scenarios", help="列出情境與故障模型")
    p.set_defaults(func=cmd_scenarios)

    p = sub.add_parser("topology", help="列出工廠拓撲與 Knowledge Graph")
    p.set_defaults(func=cmd_topology)

    p = sub.add_parser("episode", help="跑單一 Episode 並輸出 KPI")
    p.add_argument("scenario", choices=list(SCENARIOS))
    p.add_argument("--mode", default="guardian", choices=list(MODES))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_episode)

    p = sub.add_parser("benchmark", help="三組對照組 KPI 比較")
    p.add_argument("scenarios", nargs="*", choices=list(SCENARIOS) + [[]], default=[])
    p.add_argument("--out", help="輸出 JSON 報表路徑")
    p.add_argument("--no-audit", action="store_true", help="不寫稽核檔")
    p.add_argument("--business", action="store_true", help="接著輸出商業案例（年度效益、ROI、回收期）")
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser("business-case", help="由實測 KPI 推導年度效益、ROI、回收期與競品比較")
    p.add_argument("scenarios", nargs="*", choices=list(SCENARIOS) + [[]], default=[])
    p.add_argument("--scope", default="plant", choices=("plant", "pilot"), help="部署規模（預設全廠）")
    p.add_argument("--lines", type=int, default=6, help="全廠規模的產線數（--scope plant 時生效）")
    p.add_argument("--from-json", help="沿用既有 benchmark --out 的 JSON，不重跑模擬")
    p.add_argument("--out", help="輸出商業案例 JSON 路徑")
    p.add_argument("--json", action="store_true", help="只輸出 JSON")
    p.add_argument("--no-audit", action="store_true", help="不寫稽核檔")
    p.set_defaults(func=cmd_business_case)

    p = sub.add_parser("serve", help="啟動 Dashboard 與 API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("audit", help="讀回稽核軌跡")
    p.add_argument("paths", nargs="+")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("knowledge", help="測試手冊 / SOP / 維修紀錄檢索")
    p.add_argument("query")
    p.add_argument("--top-k", type=int, default=5)
    p.set_defaults(func=cmd_knowledge)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print("\n[dim]已中斷。[/dim]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
