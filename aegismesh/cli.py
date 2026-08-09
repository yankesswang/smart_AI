"""AegisMesh 命令列介面 —— 舞台 Demo 的主入口。

    python -m aegismesh.cli demo typhoon-fiber-cut
    python -m aegismesh.cli scenarios
    python -m aegismesh.cli topology
    python -m aegismesh.cli audit runs/audit-*.jsonl
"""

from __future__ import annotations

import argparse
import sys
from functools import partial
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from .audit import AuditLog
from .episode import AEGIS, HUMAN_HEURISTIC, POLICY_LABELS, compare
from .domain import RecoveryPlan
from .optimizer import STRATEGY_LABELS
from .orchestrator import Orchestrator, auto_approve
from .twin.engine import DigitalTwin
from .twin.scenarios import SCENARIOS, get_scenario

console = Console()

_HEALTH_STYLE = {"ok": "green", "warning": "yellow", "critical": "red"}
_DECISION_STYLE = {"allow": "green", "require_approval": "yellow", "deny": "red"}


# --------------------------------------------------------------------- 呈現


def _snapshot_table(twin: DigitalTwin, snap, title: str) -> Table:
    t = Table(title=title, title_style="bold", header_style="bold cyan", expand=False)
    t.add_column("優先級", justify="center")
    t.add_column("業務")
    t.add_column("路徑", overflow="fold")
    t.add_column("頻寬", justify="right")
    t.add_column("延遲", justify="right")
    t.add_column("丟包", justify="right")
    t.add_column("SLA", justify="center")

    for sid in sorted(snap.services, key=lambda s: twin.services[s].priority):
        st = snap.services[sid]
        svc = twin.services[sid]
        wan = next(
            (l.kind.value for l in twin.path_links(st.path or []) if l.kind.value in
             {"fiber", "5g", "satellite"}),
            "—",
        )
        latency = "—" if st.latency_ms == float("inf") else f"{st.latency_ms:.0f}ms"
        style = _HEALTH_STYLE[st.health.value]
        t.add_row(
            f"P{svc.priority}",
            svc.name,
            wan,
            f"{st.admitted_mbps:.0f}M",
            latency,
            f"{st.loss_pct:.2f}%",
            f"[{style}]{'✓' if st.slo_met else '✗'}[/{style}]",
        )
    return t


def _metrics_line(snap) -> str:
    return (
        f"關鍵業務可用率 [bold]{snap.critical_availability_pct:.0f}%[/bold]　"
        f"SLA 達成率 [bold]{snap.slo_compliance_pct:.0f}%[/bold]　"
        f"月成本 [bold]NT${snap.monthly_cost_ntd:,.0f}[/bold]"
    )


_DECISION_LABEL = {"allow": "自動放行", "require_approval": "需核准", "deny": "拒絕", "pending": "—"}


def _plan_table(plans: list[RecoveryPlan], selected_id: str | None) -> Table:
    t = Table(title="候選復原計畫（孿生推演結果）", header_style="bold cyan")
    t.add_column("策略", no_wrap=True)
    t.add_column("動作", justify="right")
    t.add_column("關鍵", justify="right")
    t.add_column("SLA", justify="right")
    t.add_column("月成本", justify="right", no_wrap=True)
    t.add_column("評分", justify="right")
    t.add_column("風險", justify="right")
    t.add_column("政策裁決", justify="center", no_wrap=True)

    for p in plans:
        pr = p.projected
        style = _DECISION_STYLE.get(p.policy_decision, "white")
        chosen = p.id == selected_id
        label = ("▶ " if chosen else "  ") + STRATEGY_LABELS.get(p.strategy, p.strategy)
        t.add_row(
            f"[bold]{label}[/bold]" if chosen else label,
            str(len(p.actions)),
            f"{pr.critical_availability_pct:.0f}%" if pr else "—",
            f"{pr.slo_compliance_pct:.0f}%" if pr else "—",
            f"NT${pr.monthly_cost_ntd:,.0f}" if pr else "—",
            f"{p.score:.1f}",
            f"{p.risk_score:.0f}",
            f"[{style}]{_DECISION_LABEL.get(p.policy_decision, p.policy_decision)}[/{style}]",
        )
    return t


def _interactive_approval(twin: DigitalTwin, plan: RecoveryPlan) -> bool:
    console.print()
    console.print(Panel(
        "\n".join(
            [f"[bold]計畫[/bold] {plan.id}　[bold]風險評分[/bold] {plan.risk_score:.0f}/100", ""]
            + [f"  • {f}" for f in plan.policy_findings]
            + ["", "[bold]將執行的動作：[/bold]"]
            + [f"  {i}. {twin.describe_action(a)}" for i, a in enumerate(plan.actions, 1)]
        ),
        title="[yellow]⚠ 需要人工核准[/yellow]",
        border_style="yellow",
    ))
    try:
        answer = console.input("[bold]核准執行？[/bold] (y/N) ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[red]未取得核准，中止。[/red]")
        return False
    return answer in {"y", "yes"}


# --------------------------------------------------------------------- 指令


def cmd_demo(args: argparse.Namespace) -> int:
    scenario = get_scenario(args.scenario)
    orch = Orchestrator()

    console.print(Rule("[bold]AegisMesh 天穹韌網[/bold] · 通訊韌性數位孿生閉環"))
    console.print(f"推理引擎：[cyan]{orch.llm.mode}[/cyan]")
    if not orch.llm.online:
        console.print("[dim]（.env 未設定 OPENAI_API_KEY，敘述改由決定性樣板產生；"
                      "所有數值與決策不受影響）[/dim]")
    console.print()

    stages: list = []
    orch_twin = orch.twin

    def on_stage(kind: str, payload) -> None:
        stages.append((kind, payload))
        if kind == "baseline":
            console.print(Rule("① 事故前基準"))
            console.print(_snapshot_table(orch_twin, payload, "正常營運狀態"))
            console.print(_metrics_line(payload))
        elif kind == "incident":
            console.print()
            console.print(Rule(f"② 災害注入：{scenario.name}"))
            console.print(f"[dim]{scenario.narrative}[/dim]")
            for f in scenario.faults:
                cap = orch_twin.links[f.link_id].capacity_mbps
                console.print(f"  [red]![/red] {f.description}")
                console.print(f"    [dim]$ {f.netem_command(capacity_mbps=cap)}[/dim]")
            console.print()
            console.print(_snapshot_table(orch_twin, payload, "事故當下狀態"))
            console.print(_metrics_line(payload))
        elif kind == "agent":
            console.print()
            console.print(Panel(
                payload.narrative or "[dim](無敘述)[/dim]",
                title=f"[bold cyan]{payload.agent}[/bold cyan] · {payload.stage}",
                border_style="cyan",
            ))
        elif kind == "plans":
            console.print()
            console.print(Rule("③④ 規劃 · 孿生推演 · 政策治理"))
            sel = payload["selected"]
            console.print(_plan_table(payload["plans"], sel.id if sel else None))
        elif kind == "approval":
            plan = payload["plan"]
            if not payload["human_gate"]:
                console.print(f"[green]政策自動放行（{plan.id} 未觸發任何治理規則）[/green]")
            elif args.auto_approve:
                console.print("[yellow]⚠ --auto-approve：略過人工閘門（僅供自動化測試）[/yellow]")
            else:
                console.print(f"[yellow]人工核准結果：{'核准' if payload['approved'] else '拒絕'}[/yellow]")
        elif kind == "execute":
            console.print()
            console.print(Rule("⑤ 執行變更"))
            for act in payload["applied"]:
                console.print(f"  [green]✓[/green] {act}")
        elif kind == "replan":
            console.print(f"[yellow]↻ 驗證未通過，啟動第 {payload['round'] + 1} 輪重規劃[/yellow]")

    approval = auto_approve if args.auto_approve else partial(_interactive_approval, orch_twin)
    result = orch.run(scenario, approval_fn=approval, max_rounds=args.max_rounds, on_stage=on_stage)

    console.print()
    console.print(Rule("⑥ 驗證與前後對照"))
    console.print(_snapshot_table(orch_twin, result.final, "復原後狀態"))

    cmp_table = Table(header_style="bold cyan")
    cmp_table.add_column("指標")
    cmp_table.add_column("事故前", justify="right")
    cmp_table.add_column("事故當下", justify="right")
    cmp_table.add_column("復原後", justify="right")
    s = result.summary()
    for label, key, suffix in [
        ("關鍵業務可用率", "critical_availability_pct", "%"),
        ("整體 SLA 達成率", "slo_compliance_pct", "%"),
    ]:
        v = s[key]
        cmp_table.add_row(label, f"{v['baseline']}{suffix}", f"{v['incident']}{suffix}",
                          f"[bold green]{v['final']}{suffix}[/bold green]")
    c = s["monthly_cost_ntd"]
    cmp_table.add_row("月度通訊成本", f"NT${c['baseline']:,}", "—", f"NT${c['final']:,}")
    console.print(cmp_table)

    ok, msg = orch.verify_audit()
    console.print()
    console.print(
        f"稽核軌跡：[cyan]{result.audit_path}[/cyan]　"
        f"雜湊鏈驗證：[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]　"
        f"共 {len(orch.audit.entries())} 筆"
    )
    if result.halted_reason:
        console.print(f"[yellow]閉環中止原因：{result.halted_reason}[/yellow]")
    console.print(
        f"[bold]{'✓ 生命關鍵業務已全數恢復' if result.succeeded else '✗ 仍有關鍵業務未恢復'}[/bold]"
        f"　（執行 {result.rounds} 輪閉環）"
    )
    return 0 if result.succeeded else 1


def cmd_episode(args: argparse.Namespace) -> int:
    """整場事件的多時段推演：AI 配速 vs 人工經驗法則。"""
    scenario = get_scenario(args.scenario)
    console.print(Rule(f"[bold]{scenario.name}[/bold] · {scenario.duration_hours:.0f} 小時推演"))
    console.print(f"[dim]{scenario.narrative}[/dim]\n")

    results = compare(scenario)
    for policy in (HUMAN_HEURISTIC, AEGIS):
        r = results[policy]
        t = Table(title=POLICY_LABELS[policy], header_style="bold cyan")
        t.add_column("時段", no_wrap=True)
        t.add_column("採用方案", no_wrap=True)
        t.add_column("生命關鍵", justify="right")
        t.add_column("關鍵服務", justify="right")
        t.add_column("衛星配額剩", justify="right", no_wrap=True)
        for s in r.steps:
            p0 = s.snapshot.life_critical_availability_pct
            style = "green" if s.critical_availability_pct >= 99.9 else (
                "yellow" if s.critical_availability_pct > 0 else "red")
            t.add_row(
                f"h{s.hour:.0f}–{s.hour + s.duration_h:.0f}",
                STRATEGY_LABELS.get(s.strategy, s.strategy or "—無可行方案—"),
                f"[{'green' if p0 >= 99.9 else 'red'}]{p0:.0f}%[/]",
                f"[{style}]{s.critical_availability_pct:.0f}%[/]",
                f"{s.quota_left_gb:.0f} GB",
            )
        console.print(t)
        console.print(
            f"  關鍵服務正常累計 [bold]{r.critical_service_hours:.1f}[/bold] / "
            f"{scenario.duration_hours:.0f} 小時"
            + (f"　衛星配額耗盡於 [red]h{r.quota_exhausted_at:.0f}[/red]"
               if r.quota_exhausted_at is not None else "　衛星配額撐完全程")
        )
        console.print()

    human, aegis = results[HUMAN_HEURISTIC], results[AEGIS]
    delta = aegis.critical_service_hours - human.critical_service_hours
    console.print(Rule("差異"))
    console.print(
        f"AegisMesh 讓關鍵醫療服務多正常運作 [bold green]{delta:+.1f} 小時[/bold green]"
        f"（{human.critical_service_hours:.1f} → {aegis.critical_service_hours:.1f}）。\n"
        f"[dim]差別發生在配速決策上：代價要到十幾小時後才浮現，"
        f"而那時已經沒有回頭路。[/dim]"
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .api.server import serve

    console.print(Rule("[bold]AegisMesh 天穹韌網[/bold] · 通訊韌性戰情室"))
    console.print(f"儀表板：[cyan]http://{args.host}:{args.port}[/cyan]")
    serve(host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_scenarios(_: argparse.Namespace) -> int:
    t = Table(title="可用災害情境", header_style="bold cyan")
    t.add_column("ID"); t.add_column("名稱"); t.add_column("故障注入")
    for s in SCENARIOS.values():
        t.add_row(s.id, s.name, "\n".join(f.description for f in s.faults))
    console.print(t)
    return 0


def cmd_topology(_: argparse.Namespace) -> int:
    twin = DigitalTwin()
    snap = twin.evaluate("baseline")

    nt = Table(title="節點", header_style="bold cyan")
    nt.add_column("ID"); nt.add_column("名稱"); nt.add_column("類型"); nt.add_column("站點")
    for n in twin.nodes.values():
        nt.add_row(n.id, n.name, n.kind.value, n.site)
    console.print(nt)

    lt = Table(title="鏈路", header_style="bold cyan")
    for c in ("ID", "來源", "目的", "類型", "容量", "基礎延遲", "成本/GB"):
        lt.add_column(c)
    for l in twin.links.values():
        lt.add_row(l.id, l.src, l.dst, l.kind.value,
                   f"{l.capacity_mbps:,.0f}M", f"{l.base_latency_ms:g}ms", f"NT${l.cost_per_gb:g}")
    console.print(lt)
    console.print(_snapshot_table(twin, snap, "業務基準狀態"))
    console.print(_metrics_line(snap))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        console.print(f"[red]找不到檔案：{path}[/red]")
        return 2
    log = AuditLog(path)
    entries = log.entries()
    ok, msg = log.verify()

    t = Table(title=f"稽核軌跡 {path.name}", header_style="bold cyan")
    t.add_column("#", justify="right"); t.add_column("時間"); t.add_column("階段")
    t.add_column("執行者"); t.add_column("雜湊", overflow="ellipsis", max_width=18)
    for i, e in enumerate(entries, 1):
        t.add_row(str(i), e["ts"][11:19], e["stage"], e["actor"], e["hash"])
    console.print(t)
    console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aegismesh", description="AegisMesh 天穹韌網 — 通訊韌性數位孿生平台"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="執行完整 Agentic 閉環")
    p_demo.add_argument("scenario", nargs="?", default="typhoon-fiber-cut",
                        choices=list(SCENARIOS), help="災害情境 ID")
    p_demo.add_argument("--auto-approve", action="store_true",
                        help="跳過人工核准閘門（僅供自動化測試）")
    p_demo.add_argument("--max-rounds", type=int, default=2, help="驗證失敗時的最大重規劃輪數")
    p_demo.set_defaults(func=cmd_demo)

    p_ep = sub.add_parser("episode", help="整場事件多時段推演：AI 配速 vs 人工經驗法則")
    p_ep.add_argument("scenario", nargs="?", default="typhoon-fiber-cut", choices=list(SCENARIOS))
    p_ep.set_defaults(func=cmd_episode)

    sub.add_parser("scenarios", help="列出可用災害情境").set_defaults(func=cmd_scenarios)

    p_serve = sub.add_parser("serve", help="啟動即時儀表板（FastAPI ＋ WebSocket）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true", help="開發模式：檔案變更自動重載")
    p_serve.set_defaults(func=cmd_serve)
    sub.add_parser("topology", help="顯示拓樸與基準狀態").set_defaults(func=cmd_topology)

    p_audit = sub.add_parser("audit", help="驗證稽核軌跡雜湊鏈")
    p_audit.add_argument("path", help="audit-*.jsonl 檔案路徑")
    p_audit.set_defaults(func=cmd_audit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
