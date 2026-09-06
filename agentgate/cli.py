"""AgentGate CLI。

用法:
    agentgate serve                   啟動治理閘門與 Dashboard(預設 http://127.0.0.1:8600)
    agentgate benchmark               跑驗證管線,輸出六指標 × 六 baseline 表格
    agentgate benchmark-llm           真 LLM 的 B1 自我審查 baseline(會打 API)
    agentgate scenarios               列出測試情境統計
    agentgate agent-run <scenario_id> 讓真的 LLM 客服 Agent 讀一件工單、決定工具呼叫,
                                      再把它送進治理管線
    agentgate business-case           商業案例:實測放行率 → 年化 ROI、三情境、破口分析
    agentgate external-eval           AgentDojo 公開 benchmark 外部驗證(預設離線重放 trace)
"""

from __future__ import annotations

import argparse
import json
import sys


def _print_agent_run(run: dict, verdict: dict) -> None:
    """把一次 Agent 決策印成兩塊:模型發出的 tool call、runtime 附上的來源鏈。

    兩塊刻意分開印。合成一塊的話,「來源鏈是誰產生的」這個問題就模糊掉了 ——
    而那正是這整條路徑要證明的事。
    """
    call = run.get("tool_call") or {}
    print(f"\n模式:{run['mode']}" + ("(降級)" if run["degraded"] else ""))
    print(f"映射延遲 mapping_latency_ms = {run['mapping_latency_ms']} ms"
          f" · token {run['tokens']['total']}")
    print("\n── 模型實際發出的 tool call ──────────────────────────────")
    print(f"  {call.get('name', '(無)')}({json.dumps(call.get('arguments', {}), ensure_ascii=False)})")
    if run.get("reasoning"):
        print(f"  模型自述:{run['reasoning']}")
    if run.get("validation_errors"):
        print(f"  G1 schema 驗證:{run['validation_errors']}")

    print("\n── runtime 附上的來源鏈(模型不參與) ─────────────────────")
    for src in run.get("read_sources", []):
        mark = "✔ 進鏈" if src["in_chain"] else "· 不進鏈(證據)"
        print(f"  [{mark}] {src['channel']:<16} {src['source_ref']}")
        print(f"           {src['why']}")

    print("\n── 治理裁決 ────────────────────────────────────────────")
    print(f"  status={verdict['status']}  risk={verdict['risk']}"
          f"  blocked_at={verdict['gate_blocked_at']}")
    print(f"  gate_latency_ms = {verdict['decision_latency_ms']} ms"
          "(閘門;與上面的映射延遲分開計,不混成一個數字)")
    for reason in verdict.get("reasons", [])[:3]:
        print(f"  · {reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentgate", description="AI Agent 可稽核治理層")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="啟動 API 與 Dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8600)
    serve.add_argument("--reload", action="store_true")

    bench = sub.add_parser("benchmark", help="跑驗證管線")
    bench.add_argument("--json", action="store_true", help="輸出完整 JSON 報告")

    bench_llm = sub.add_parser(
        "benchmark-llm", help="真 LLM 的 B1 自我審查 baseline(會打 OpenAI API)")
    bench_llm.add_argument("--repeats", type=int, default=3,
                           help="每條情境重跑幾次(量一致性;預設 3)")
    bench_llm.add_argument("--workers", type=int, default=8)
    bench_llm.add_argument("--limit", type=int, default=0,
                           help="只跑前 N 條(試水溫用;0 = 全部)")
    bench_llm.add_argument("--cached", action="store_true",
                           help="不打 API,直接讀 runs/agentgate_b1_llm.json")
    bench_llm.add_argument("--json", action="store_true")

    sub.add_parser("scenarios", help="測試情境統計")

    agent_run = sub.add_parser(
        "agent-run", help="真 LLM 客服 Agent 讀工單 → 工具呼叫 → 治理裁決")
    agent_run.add_argument("scenario_id",
                           help="Demo 步驟(injection / refund / bill_query)"
                                "或情境樣板 id(pdf_injection / kb_injection / …)")
    agent_run.add_argument("--json", action="store_true")
    agent_run.add_argument("--list", action="store_true", help="列出可用的情境 id")

    business = sub.add_parser("business-case", help="商業案例(ROI / 三情境 / 破口分析)")
    business.add_argument("--out", default=None, help="把完整報告寫成 JSON")
    business.add_argument("--json", action="store_true", help="改印完整 JSON 而非 Markdown 摘要")

    external = sub.add_parser("external-eval", help="AgentDojo 外部驗證(離線重放,--record 才打 API)")
    external.add_argument("--record", action="store_true",
                          help="重新用 gpt-4o-mini 錄製 trace(會打 API,約 30 次呼叫)")
    external.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn
        uvicorn.run("agentgate.api.server:app", host=args.host, port=args.port,
                    reload=args.reload)
        return 0

    if args.command == "benchmark":
        from .validation import run_validation, summarize_markdown
        report = run_validation()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
        else:
            print(summarize_markdown(report))
        return 0

    if args.command == "benchmark-llm":
        from .llm_baseline import (
            coverage,
            load_cached_b1_result,
            run_llm_b1,
            summarize_markdown,
        )
        from .scenarios import build_scenarios

        scenarios = build_scenarios()
        if args.limit:
            scenarios = scenarios[:args.limit]
        if args.cached:
            report = load_cached_b1_result()
            if report is None:
                print("找不到快取 runs/agentgate_b1_llm.json;請先不加 --cached 跑一次。",
                      file=sys.stderr)
                return 1
            cov = coverage(scenarios, report)
            if cov["stale"]:
                print(f"⚠ 快取涵蓋 {cov['cached']}/{cov['scenarios']} 條情境,"
                      f"有 {cov['missing']} 條未涵蓋(測試集擴充後需重跑)。",
                      file=sys.stderr)
        else:
            def progress(done: int, total: int) -> None:
                if done % 20 == 0 or done == total:
                    print(f"  … {done}/{total}", file=sys.stderr, flush=True)

            report = run_llm_b1(scenarios, repeats=args.repeats,
                                workers=args.workers, progress=progress)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
        else:
            print(summarize_markdown(report))
        return 0

    if args.command == "scenarios":
        from .scenarios import build_scenarios, scenario_stats
        print(json.dumps(scenario_stats(build_scenarios()), ensure_ascii=False, indent=2))
        return 0

    if args.command == "agent-run":
        from .agent import CustomerServiceAgent, available_references, resolve_case
        from .pipeline import AgentGatePipeline, GateConfig
        from .shadow import ShadowTelecomEnv

        if args.list:
            print(json.dumps(available_references(), ensure_ascii=False, indent=2))
            return 0

        shadow = ShadowTelecomEnv()
        pipeline = AgentGatePipeline(shadow=shadow, config=GateConfig())
        try:
            case, template_request = resolve_case(args.scenario_id, shadow=shadow)
        except KeyError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        agent = CustomerServiceAgent(audit=pipeline.audit)
        run = agent.run(case, fallback=lambda: template_request)
        request = run.request or template_request
        verdict = pipeline.evaluate(request)

        payload = {"case": case.to_dict(), "agent": run.to_dict(),
                   "verdict": verdict.to_dict(),
                   "latency": {"mapping_latency_ms": round(run.mapping_latency_ms, 3),
                               "gate_latency_ms": round(verdict.decision_latency_ms, 3)}}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=1, default=str))
        else:
            print(f"工單 {case.case_id} · {case.subject}")
            _print_agent_run(run.to_dict(), verdict.to_dict())
        return 0

    if args.command == "business-case":
        from .business import run_business_case, summarize_markdown
        report = run_business_case(out=args.out, quiet=True)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
        else:
            print(summarize_markdown(report))
        return 0

    if args.command == "external-eval":
        from .external import run_agentdojo_eval
        report = run_agentdojo_eval(record=True if args.record else None)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
        else:
            md = report.get("markdown") if isinstance(report, dict) else None
            print(md or json.dumps(report, ensure_ascii=False, indent=1, default=str))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
