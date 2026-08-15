"""舞台輸出：投影機上讀得到、且刻意不喧賓奪主的呈現。

三個約束（和 Dashboard 的設計語彙一致）：

* **一段一屏**。每段只有四塊：時間窗與畫面名稱、口白提示、細節、以及那段
  「要證明的事情 ＋ 當下數字」。評審的眼睛不用找。
* **數字一律靠右對齊、標籤靠左**。台上念的順序就是表格由上而下的順序。
* **顏色只用來分級**，不用來裝飾：紅＝被擋下／失敗，青＝結論，其餘一律 dim。
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .director import ActReport, StageRun
from .reliability import ReliabilityReport
from .script import SCRIPT, TOTAL_SECONDS, mmss

#: 細節區塊的標題。鍵是劇本裡的事實鍵。
DETAIL_TITLES: dict[str, str] = {
    "normal.machines": "機台",
    "normal.camera": "CAM-01",
    "detect.readings": "感測讀值",
    "detect.triggers": "觸發條件",
    "detect.camera": "CAM-01",
    "diagnose.modalities": "證據模態",
    "diagnose.weights": "排名權重",
    "diagnose.narrative": "敘述",
    "evidence.items": "可引用證據",
    "evidence.alternatives": "替代假設",
    "evidence.sop_refs": "SOP 條款",
    "plan.impact_orders": "受影響訂單",
    "plan.blocked": "被 Safety 阻擋",
    "plan.candidates": "方案排名",
    "approve.policy_reasons": "政策依據",
    "work_order.parts": "建議零件",
    "work_order.sop_refs": "SOP",
    "verify.effects": "執行結果",
    "verify.checks": "驗證項目",
    "summary.counterfactual": "反事實對照",
    "summary.audit_stages": "稽核軌跡",
}


def _style_for(line: str) -> str:
    if "BLOCK" in line or line.startswith("✘") or line.startswith("FAIL"):
        return "red"
    if line.startswith("PASS") or line.startswith("✔"):
        return "green"
    return "dim"


class StageRenderer:
    """把 :class:`~factory_guardian.stage.director.ActReport` 印到終端機。"""

    def __init__(self, console: Console | None = None, show_cue: bool = True, quiet: bool = False) -> None:
        self.console = console or Console()
        self.show_cue = show_cue
        self.quiet = quiet

    def opening(self, scenario_title: str, speed: float, offline: bool, settings: dict[str, Any]) -> None:
        if self.quiet:
            return
        pace = "照劇本（4:00）" if speed == 1.0 else ("不等待（驗證用）" if speed <= 0 else f"{speed:g}× 劇本時間")
        mode = "[red]最壞情況：無金鑰 ＋ 廠區對外鏈路中斷[/red]" if offline else "雲端鏈路正常"
        self.console.print(
            Panel(
                Text.from_markup(
                    f"[bold]四分鐘閉環 Demo[/bold]　·　{scenario_title}\n"
                    f"[dim]劇本：研究文件 §5.1（八段 / {mmss(TOTAL_SECONDS)}）｜節奏：{pace}[/dim]\n"
                    f"{mode}\n"
                    f"[dim]seed {settings.get('seed')}｜敘述 {settings.get('llm_mode')}"
                    f"｜核准 {'需人工' if settings.get('require_approval') else '自動'}"
                    f"｜鏈路 {settings.get('cloud_link')}[/dim]"
                ),
                border_style="cyan",
            )
        )

    def act(self, report: ActReport) -> None:
        """演完一段就印一段。"""
        if self.quiet:
            return
        act = report.act
        console = self.console
        console.rule(f"[bold]{act.window}　{act.act_id}　{act.screen}[/bold]")
        console.print(f"[dim]§5.1 要證明：{act.doc_claim}[/dim]")
        if self.show_cue:
            console.print(f"[dim]口白：{act.cue}[/dim]")
        console.print()

        for key, lines in report.detail_lines():
            console.print(f"  [bold]{DETAIL_TITLES.get(key, key)}[/bold]")
            for line in lines:
                console.print(f"    [{_style_for(line)}]{line}[/{_style_for(line)}]")
        if report.detail_lines():
            console.print()

        # 標籤欄固定寬度且不換行：台上念的是右邊那一欄的數字，
        # 標籤太長時寧可截斷，也不要讓一個數字被推到第二行去。
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column("", width=32, overflow="ellipsis", no_wrap=True)
        table.add_column("", justify="right", overflow="fold")
        for label, value in report.values():
            table.add_row(Text(label, style="dim"), Text(value, style="bold" if value != "—" else "dim"))
        console.print(table)
        console.print()

        mark = "[green]✔[/green]" if report.ok else "[red]✘[/red]"
        console.print(f"{mark} [bold cyan]{act.proves}[/bold cyan]")
        if report.missing:
            console.print(f"  [red]缺少資料：{'、'.join(report.missing)}[/red]")

    def closing(self, run: StageRun) -> None:
        if self.quiet:
            return
        console = self.console
        console.rule("[bold]開演結果[/bold]")
        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("判準", width=34, overflow="fold")
        table.add_column("", width=4)
        table.add_column("依據", overflow="fold")
        for check in run.checks:
            table.add_row(
                check.name,
                Text("PASS", style="green") if check.passed else Text("FAIL", style="red"),
                Text(check.detail, style="dim"),
            )
        console.print(table)
        console.print()

        summary = (
            f"[bold]{'DEMO 成功' if run.ok else 'DEMO 未通過'}[/bold]"
            f"　·　八段全數演出：{len(run.acts)}/{len(SCRIPT)}"
            f"　·　資料遺失率 {run.data_loss_pct:.1f}%"
            f"　·　閉環運算 {run.compute_s:.2f}s"
            f"　·　決策指紋 {run.fingerprint_hash}"
        )
        if run.offline_failover_s is not None:
            summary += f"\n[bold red]離線備援時間 {run.offline_failover_s:.2f}s[/bold red]" \
                       f"　·　從廠區對外鏈路中斷到閉環驗證完成，全程在邊緣完成（敘述器 {run.llm_mode}）"
        if run.audit_path:
            summary += f"\n[dim]稽核軌跡：{run.audit_path}（{run.audit_records} 筆）[/dim]"
        for failure in run.failures():
            summary += f"\n[red]{failure}[/red]"
        console.print(Panel(Text.from_markup(summary), border_style="green" if run.ok else "red"))


def render_reliability(report: ReliabilityReport, console: Console | None = None) -> None:
    """把可靠性統計印成評審看得懂的一張表（對應研究文件 §5.3「Demo 穩定」）。"""
    console = console or Console()
    table = Table(
        title=f"Demo 穩定度｜{report.scenario_id}｜連續 {report.runs} 次"
              f"｜{'最壞情況（無金鑰＋斷網）' if report.offline else '一般情況'}",
        header_style="bold",
    )
    table.add_column("指標", width=30)
    table.add_column("實測", justify="right", width=16)
    table.add_column("說明", overflow="fold")

    rows: list[tuple[str, str, str, str]] = [
        ("連續成功次數", f"{report.longest_streak} / {report.runs}", "文件 §5.3 指名指標", "cyan"),
        ("成功率", f"{report.success_rate_pct:.1f}%", f"{report.successes} 成功 / {report.failures} 失敗", ""),
        ("失敗率", f"{report.failure_rate_pct:.1f}%", "任一判準未過或缺資料即算失敗", ""),
        ("耗時 p50", f"{report.p50_s:.3f}s", "單次完整閉環（不含現場節奏停頓）", ""),
        ("耗時 p95", f"{report.p95_s:.3f}s", "", ""),
        ("耗時 max", f"{report.max_s:.3f}s", "最慢的一次", ""),
        (
            "決策一致性",
            "一致" if report.decisions_consistent else f"{len(report.fingerprints)} 種",
            "每輪的決策指紋（診斷／排名／安全裁決／核准／執行／KPI）",
            "cyan" if report.decisions_consistent else "red",
        ),
        ("資料遺失率（最大）", f"{report.data_loss_pct_max:.2f}%", "劇本要求的欄位有沒有拿不到的", ""),
    ]
    if report.offline_failover_p95_s is not None:
        rows.append(
            ("離線備援時間 p95", f"{report.offline_failover_p95_s:.3f}s",
             "斷網 → 閉環驗證完成；max "
             f"{report.offline_failover_max_s:.3f}s", "cyan")
        )
    for label, value, note, style in rows:
        table.add_row(label, Text(value, style=style or "bold"), Text(note, style="dim"))
    console.print(table)

    if report.fingerprints:
        console.print(
            "[dim]決策指紋："
            + "、".join(f"{h} ×{n}" for h, n in report.fingerprints.items())
            + f"｜總牆鐘時間 {report.wall_s:.2f}s[/dim]"
        )
    for reason in report.failure_reasons()[:10]:
        console.print(f"[red]{reason}[/red]")


__all__ = ["DETAIL_TITLES", "StageRenderer", "render_reliability"]
