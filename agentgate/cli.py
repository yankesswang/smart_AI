"""AgentGate CLI。

用法:
    agentgate serve            啟動治理閘門與 Dashboard(預設 http://127.0.0.1:8600)
    agentgate benchmark        跑驗證管線,輸出六指標 × 六 baseline 表格
    agentgate scenarios        列出 120 條測試情境統計
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentgate", description="AI Agent 可稽核治理層")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="啟動 API 與 Dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8600)
    serve.add_argument("--reload", action="store_true")

    bench = sub.add_parser("benchmark", help="跑驗證管線")
    bench.add_argument("--json", action="store_true", help="輸出完整 JSON 報告")

    sub.add_parser("scenarios", help="測試情境統計")

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

    if args.command == "scenarios":
        from .scenarios import build_scenarios, scenario_stats
        print(json.dumps(scenario_stats(build_scenarios()), ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
