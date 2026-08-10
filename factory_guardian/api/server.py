"""FastAPI 服務。

三種角色：
1. Demo Dashboard 的後端（即時狀態、事件串流、人工核准）。
2. Simulator API —— 規格 §11 的「Action 層」介面，真實導入時由 OPC-UA / MQTT / MES Adapter 取代。
3. Benchmark / 稽核查詢介面。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import DATA_DISCLAIMER, __version__
from ..audit import load_audit, summarize_audit
from ..benchmark import MODE_LABELS, REPORT_COLUMNS, run_benchmark
from ..config import get_settings
from ..knowledge.corpus import MAINTENANCE_HISTORY, MANUALS
from ..knowledge.retriever import default_kb
from ..policy.engine import PolicyEngine
from ..twin.faults import FAULTS
from ..twin.scenarios import SCENARIOS
from .session import DemoSession

STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------------------
class ResetRequest(BaseModel):
    scenario_id: str | None = None


class InjectRequest(BaseModel):
    scenario_id: str


class TickRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=200)


class RunRequest(BaseModel):
    max_ticks: int = Field(default=40, ge=1, le=300)


class ApprovalRequest(BaseModel):
    plan_id: str
    approved: bool
    approver: str = "operator"
    reason: str = ""


class BenchmarkRequest(BaseModel):
    scenario_ids: list[str] | None = None


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Factory Guardian AI",
        version=__version__,
        description="Agentic AI 智慧工廠自主營運與風險管理平台（競賽 MVP）",
    )
    session = DemoSession(settings=settings)
    app.state.session = session

    # ---------------------------------------------------------------- 基本資訊
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "disclaimer": DATA_DISCLAIMER}

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {
            "settings": settings.describe(),
            "disclaimer": DATA_DISCLAIMER,
            "llm_role": "LLM 僅產生敘述文字；方案排名、安全裁決與動作執行皆由規則引擎與模擬器決定。",
        }

    @app.get("/api/scenarios")
    def scenarios() -> dict[str, Any]:
        return {
            "scenarios": [s.to_dict() for s in SCENARIOS.values()],
            "faults": [
                {"fault_id": f.fault_id, "label": f.label, "signature": f.signature_note,
                 "manual_refs": list(f.manual_refs), "repair_min": f.repair_min}
                for f in FAULTS.values()
            ],
            "note": "Ground Truth 存在情境定義中，但只用於 Benchmark 評分，不會進入 Agent 輸入。",
        }

    @app.get("/api/topology")
    def topology() -> dict[str, Any]:
        topo = session.twin.topo
        return {
            "graph": topo.graph_json(),
            "machines": [
                {
                    "machine_id": m.machine_id, "name": m.name, "kind": m.kind.value,
                    "rated_rate_uph": m.rated_rate_uph, "products": list(m.products),
                    "changeover_min": m.changeover_min, "repair_min": m.repair_min,
                    "signals": [
                        {"name": s.name, "unit": s.unit, "nominal": s.nominal, "range": s.describe_range()}
                        for s in m.signals
                    ],
                }
                for m in topo.machines.values()
            ],
            "lines": [{"line_id": l.line_id, "name": l.name, "stages": [list(s) for s in l.stages]}
                      for l in topo.lines.values()],
            "products": [{"product_id": p.product_id, "name": p.name} for p in topo.products.values()],
        }

    @app.get("/api/policy")
    def policy() -> dict[str, Any]:
        return PolicyEngine(require_approval=settings.require_approval).describe()

    @app.get("/api/knowledge")
    def knowledge() -> dict[str, Any]:
        kb = default_kb()
        return {
            "stats": kb.stats(),
            "manuals": [
                {"ref": d.ref, "title": d.title, "type": d.doc_type, "fault_ids": list(d.fault_ids), "synthetic": d.synthetic}
                for d in MANUALS
            ],
            "history_sample": [c.as_text() for c in MAINTENANCE_HISTORY[:5]],
            "disclaimer": DATA_DISCLAIMER,
        }

    @app.get("/api/knowledge/search")
    def knowledge_search(q: str, top_k: int = 5) -> dict[str, Any]:
        return {"query": q, "hits": [h.to_dict() for h in default_kb().search(q, top_k=top_k)]}

    # ---------------------------------------------------------------- Session 控制
    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return session.state()

    @app.post("/api/session/reset")
    def reset(req: ResetRequest) -> dict[str, Any]:
        if req.scenario_id and req.scenario_id not in SCENARIOS:
            raise HTTPException(404, f"未知情境 {req.scenario_id}")
        return session.reset(req.scenario_id)

    @app.post("/api/session/inject")
    def inject(req: InjectRequest) -> dict[str, Any]:
        if req.scenario_id not in SCENARIOS:
            raise HTTPException(404, f"未知情境 {req.scenario_id}")
        return session.inject(req.scenario_id)

    @app.post("/api/session/tick")
    def tick(req: TickRequest) -> dict[str, Any]:
        if session.running:
            raise HTTPException(409, "閉環執行中，請等待完成後再手動推進。")
        return session.tick(req.count)

    @app.post("/api/session/run")
    def run(req: RunRequest) -> dict[str, Any]:
        if session.running:
            raise HTTPException(409, "閉環已在執行中。")
        session.run_loop(req.max_ticks)
        return {"started": True}

    @app.get("/api/session/pending-approval")
    def pending_approval() -> dict[str, Any]:
        return {"pending": session.pending.to_dict() if session.pending else None}

    @app.post("/api/session/approve")
    def approve(req: ApprovalRequest) -> dict[str, Any]:
        ok = session.submit_approval(req.plan_id, req.approved, req.approver, req.reason)
        if not ok:
            raise HTTPException(409, "目前沒有等待中的核准，或方案代號不符。")
        return {"ok": True}

    # ---------------------------------------------------------------- 事件串流
    @app.get("/api/events")
    def events(since: int = 0) -> dict[str, Any]:
        return {"events": session.events_since(since), "seq": session.seq}

    @app.get("/api/stream")
    async def stream(since: int = 0) -> StreamingResponse:
        async def generator():
            cursor = since
            while True:
                new_events = session.events_since(cursor)
                for event in new_events:
                    cursor = event["seq"]
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                await asyncio.sleep(0.3)

        return StreamingResponse(generator(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------------- 稽核與 Benchmark
    @app.get("/api/audit")
    def audit(limit: int = 200) -> dict[str, Any]:
        records = session.audit.to_list()
        return {"run_id": session.audit.run_id, "path": str(session.audit.path),
                "total": len(records), "records": records[-limit:]}

    @app.get("/api/audit/{run_id}")
    def audit_file(run_id: str) -> dict[str, Any]:
        path = settings.audit_dir / f"audit-{run_id}.jsonl"
        if not path.exists():
            raise HTTPException(404, f"找不到稽核檔 {path.name}")
        records = load_audit(path)
        return {"run_id": run_id, "summary": summarize_audit(records), "records": records}

    @app.post("/api/benchmark")
    def benchmark(req: BenchmarkRequest) -> dict[str, Any]:
        report = run_benchmark(scenario_ids=req.scenario_ids, settings=settings, persist_audit=False)
        return report.to_dict()

    @app.get("/api/benchmark/columns")
    def benchmark_columns() -> dict[str, Any]:
        return {
            "columns": [{"key": k, "label": label, "higher_is_better": hib} for k, label, hib in REPORT_COLUMNS],
            "modes": MODE_LABELS,
        }

    # ---------------------------------------------------------------- 前端
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def _page(filename: str) -> HTMLResponse:
        path = STATIC_DIR / filename
        if not path.exists():
            return HTMLResponse(f"<h1>Factory Guardian AI</h1><p>缺少靜態檔 {filename}。</p>", status_code=500)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        """產線戰情中心（即時 Dashboard）。"""
        return _page("index.html")

    @app.get("/benchmark", response_class=HTMLResponse)
    def benchmark_page() -> HTMLResponse:
        """三組對照組 Benchmark 報告頁。"""
        return _page("benchmark.html")

    return app


app = create_app()


__all__ = ["app", "create_app"]
