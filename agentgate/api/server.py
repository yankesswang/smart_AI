"""FastAPI 服務(規格 §6)。

四種角色:
1. 治理閘門 API:/api/gate/* —— 動作請求 → 完整裁決、預演、核准、稽核。
2. 營運實境:/api/ops/* —— 當班流量、工單與對話、影子後台查詢。
3. Demo 導播:/api/demo/* —— 四分鐘劇本(§7.2)的步驟驅動與 A/B 對照。
4. 核准介面與 Dashboard 的後端。

可靠性原則(§7.1):本機離線可跑,政策裁決層不依賴任何外部服務。
營運流量由前端輪詢懶惰推進(見 ``console.OpsSimulator``),不開背景執行緒。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import DATA_DISCLAIMER, __version__
from ..agent import (
    AgentSettings,
    CustomerServiceAgent,
    available_references,
    resolve_case,
)
from ..console import CASE_TEMPLATES, TEMPLATES_BY_ID, OpsSimulator, current_shift
from ..demo import DEMO_SCRIPT, LIVE_AGENT_STEPS, build_step_case_with_agent
from ..gates.g1_resolution import attach_runtime_provenance, resolve_action
from ..gates.g4_approval import APPROVERS
from ..mechanism import describe as describe_mechanism
from ..pipeline import AgentGatePipeline, GateConfig
from ..shadow import PLANS, ShadowTelecomEnv
from ..scenarios import build_scenarios, scenario_stats
from ..validation import run_validation

STATIC_DIR = Path(__file__).parent / "static"

# 開機時回填多久的當班歷史。治理層不該是空的 —— 它已經上工一段時間了。
WARM_START_MINUTES = 45


# --------------------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------------------
class EvaluatePayload(BaseModel):
    """自由形式的工具呼叫 payload;G1 在此邊界解析與驗證。"""

    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    principal: str = ""
    principal_role: str = "customer"
    agent_id: str = ""
    provenance_chain: list[dict[str, Any]] = Field(default_factory=list)
    reasoning: str = ""
    # runtime(harness)握有的事實:這次 tool call 之前 Agent 讀過哪些工具回傳/附件、
    # 使用者確認過哪些動作、已知悉哪些條件。給了它,來源鏈就以 runtime 為準;
    # 沒給,payload 自報的 confirms / confirmed_action_hash / acknowledgements
    # 會在 G1 邊界被清空(見 gates/g1_resolution)。
    harness_context: dict[str, Any] | None = None
    action_id: str | None = None
    trace_id: str | None = None


class DecisionPayload(BaseModel):
    approver_id: str
    credential: str          # 門號級身分綁定碼(Demo 模擬)
    reason: str = ""


class DemoStepPayload(BaseModel):
    step_id: str
    #: "template" = 既有的情境樣板路徑;"llm" = 真的讓 LLM Agent 讀工單決定工具呼叫。
    #: 沒有金鑰時 "llm" 會自動降級回樣板並在回應與稽核鏈標記 degraded。
    agent: str = "template"


class AgentRunPayload(BaseModel):
    """讓被治理的 LLM Agent 跑一件工單。

    ``case_id`` 指向營運中心已經發生過的工單(重放它的對話與附件);
    ``scenario_id`` 指向 Demo 步驟或情境樣板(當場生一件新的)。
    """

    case_id: str | None = None
    scenario_id: str | None = None
    evaluate: bool = True         # False = 只要 tool call 與來源鏈,不進治理管線


class GateTogglePayload(BaseModel):
    enabled: bool


class TamperPayload(BaseModel):
    seq: int
    detail: dict[str, Any] = Field(default_factory=lambda: {"note": "tampered"})


class StreamPayload(BaseModel):
    running: bool


class InjectPayload(BaseModel):
    template_id: str


def create_app(live_ops: bool | None = None,
               warm_minutes: int = WARM_START_MINUTES) -> FastAPI:
    """建立服務。

    ``live_ops`` 決定要不要跑當班流量模擬(預設開,可用 ``AGENTGATE_LIVE_OPS=0``
    或參數關掉)。測試把它關掉,因為治理行為的驗證要在一個安靜、確定的環境裡做;
    現場則一定要開 —— 空的治理台不像一個在運作的系統。
    """
    if live_ops is None:
        live_ops = os.environ.get("AGENTGATE_LIVE_OPS", "1") != "0"

    app = FastAPI(
        title="AgentGate",
        version=__version__,
        description="AI Agent 可稽核治理層(競賽 MVP)",
    )

    shadow = ShadowTelecomEnv()
    pipeline = AgentGatePipeline(shadow=shadow, config=GateConfig())
    ops = OpsSimulator(pipeline=pipeline, shadow=shadow)
    ops.running = live_ops
    if live_ops:
        ops.warm_start(minutes=warm_minutes)

    agent_holder: dict[str, Any] = {"agent": None}

    def get_agent() -> CustomerServiceAgent:
        """被治理的那個 Agent。延遲建立 —— 沒人要用真 Agent 就別碰 openai 套件。"""
        if agent_holder["agent"] is None:
            agent_holder["agent"] = CustomerServiceAgent(audit=pipeline.audit)
        return agent_holder["agent"]

    state: dict[str, Any] = {
        "benchmark": None,          # 最近一次驗證報告
        "demo_events": [],          # Demo 敘事事件(前端輪詢)
        "ab_result": None,          # A/B 對照的兩次結果
        "live_ops": live_ops,
        "demo_cases": {},           # 劇本步驟產生的工單(供案件檢視回查)
    }

    def publish(event_type: str, payload: dict[str, Any]) -> None:
        state["demo_events"].append(
            {"seq": len(state["demo_events"]), "type": event_type, "payload": payload}
        )

    def tick() -> None:
        """把營運時鐘推到現在。所有前端輪詢端點都會先呼叫它。"""
        if state["live_ops"]:
            ops.advance()

    def find_case(case_id: str) -> dict[str, Any] | None:
        record = ops.get_case(case_id)
        if record is not None:
            return record.to_dict()
        return state["demo_cases"].get(case_id)

    # ---------------------------------------------------------------- 基本資訊
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "disclaimer": DATA_DISCLAIMER}

    # ---------------------------------------------------------------- 治理閘門(§6)
    def _resolve(payload: EvaluatePayload):
        """把 payload 過一次 runtime provenance 契約再解析。

        有 harness_context 就以 runtime 紀錄改寫來源鏈(自報只能縮小信任);
        沒有的話走純自報路徑,而 resolve_action 會把自報的確認欄位清掉。
        """
        raw = payload.model_dump()
        harness = raw.pop("harness_context", None)
        findings: list[Any] = []
        if harness is not None:
            raw, findings = attach_runtime_provenance(raw, harness)
        raw["_runtime_findings"] = findings
        return resolve_action(raw)

    @app.post("/api/gate/evaluate")
    def gate_evaluate(payload: EvaluatePayload) -> dict[str, Any]:
        """動作請求 → 完整裁決(同步)。"""
        request, errors = _resolve(payload)
        if request is None:
            raise HTTPException(422, detail={"gate": "G1", "errors": errors})
        verdict = pipeline.evaluate(request)
        publish("verdict", verdict.to_dict())
        return verdict.to_dict()

    @app.post("/api/gate/simulate")
    def gate_simulate(payload: EvaluatePayload) -> dict[str, Any]:
        """僅執行 G3 預演,不進入核准佇列。"""
        request, errors = _resolve(payload)
        if request is None:
            raise HTTPException(422, detail={"gate": "G1", "errors": errors})
        return pipeline.simulate(request)

    @app.get("/api/gate/pending")
    def gate_pending() -> dict[str, Any]:
        """待核准佇列(含完整證據包)。

        排序刻意是「最急的在最上面」:先看 SLA 是否已逾時,再看剩餘時間。
        值班台的排序方式本身就是政策的一部分 —— 照送件順序排,高風險案件會被
        一整排低風險的擋在後面。
        """
        tick()
        now = datetime.now(timezone.utc)
        pending = sorted(
            pipeline.queue.pending(),
            key=lambda p: (not p.sla_state(now)["breached"],
                           p.sla_state(now)["remaining_seconds"]),
        )
        decided = sorted(
            (p for p in pipeline.queue.all_items() if p.status != "pending"),
            key=lambda p: p.decided_at or "",
        )
        return {
            "pending": [p.evidence_package(now) for p in pending],
            "decided": [p.evidence_package(now) for p in decided][-12:],
            "approvers": [
                {"approver_id": k, "name": v["name"], "role": v["role"]}
                for k, v in APPROVERS.items()
            ],
            "duty": current_shift(now.astimezone()),
        }

    @app.post("/api/gate/approve/{approval_id}")
    def gate_approve(approval_id: str, payload: DecisionPayload) -> dict[str, Any]:
        """核准(需核准者身分憑證)。"""
        verdict, error = pipeline.decide_approval(
            approval_id, True, payload.approver_id, payload.credential, payload.reason)
        if verdict is None:
            raise HTTPException(409, error)
        publish("approval", verdict.to_dict())
        return verdict.to_dict()

    @app.post("/api/gate/reject/{approval_id}")
    def gate_reject(approval_id: str, payload: DecisionPayload) -> dict[str, Any]:
        """駁回(須填理由)。"""
        if not payload.reason.strip():
            raise HTTPException(422, "駁回必須填寫理由")
        verdict, error = pipeline.decide_approval(
            approval_id, False, payload.approver_id, payload.credential, payload.reason)
        if verdict is None:
            raise HTTPException(409, error)
        publish("approval", verdict.to_dict())
        return verdict.to_dict()

    @app.get("/api/gate/audit")
    def gate_audit(limit: int = 200, trace_id: str | None = None) -> dict[str, Any]:
        """稽核軌跡查詢。"""
        return {
            "run_id": pipeline.audit.run_id,
            "total": len(pipeline.audit.records),
            "records": pipeline.audit.to_list(limit=limit, trace_id=trace_id),
        }

    @app.get("/api/gate/audit/verify")
    def gate_audit_verify() -> dict[str, Any]:
        """雜湊鏈完整性驗證。"""
        return pipeline.audit.verify()

    @app.get("/api/gate/policy")
    def gate_policy() -> dict[str, Any]:
        """匯出現行政策(規則 + 動作權限表 + 政策版本)。

        ``version.policy_version`` 是規則條文、權限表與生效限額的內容雜湊,
        也是每筆 g2_adjudication 稽核紀錄上帶的那一個 —— 稽核可以拿紀錄上的
        版本號回來比對「當時生效的條文是不是這一份」。
        """
        described = pipeline.engine.describe()
        return {**described, "policy_version": described["version"]["policy_version"]}

    @app.get("/api/gate/mechanism")
    def gate_mechanism() -> dict[str, Any]:
        """匯出運作機制說明(§3.1 六道關卡)。

        和 /api/gate/policy 同一個原則:說明由後端從生效中的程式碼匯出,
        前端不另外維護一份敘述。附帶的探針 payload 就是 /api/gate/evaluate
        的請求體 —— 前端按下去送的是真的動作請求,不是預錄的結果。
        """
        return {**describe_mechanism(), "disclaimer": DATA_DISCLAIMER}

    @app.post("/api/gate/agent/run")
    def gate_agent_run(payload: AgentRunPayload) -> dict[str, Any]:
        """被治理的 LLM Agent 跑一件工單:tool call → 來源鏈 → GateVerdict。

        模型看得到附件全文(含夾帶的注入行),但 ``provenance_chain`` 由 runtime
        依「模型讀進 context 的東西」組出,不由模型自報(規格 §4.2)。
        模型的輸出必須先通過 G1 ``validate_request()`` 才進得了 G2(§4.3)。
        """
        if payload.case_id:
            record = find_case(payload.case_id)
            if record is None:
                raise HTTPException(404, f"找不到工單 {payload.case_id}")
            reference = record["case"].get("template_id", "")
            reference = reference[5:] if reference.startswith("demo_") else reference
        elif payload.scenario_id:
            reference = payload.scenario_id
        else:
            raise HTTPException(422, "需要 case_id 或 scenario_id 其中之一")

        try:
            case, template_request = resolve_case(reference, shadow=shadow)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

        agent = get_agent()
        run = agent.run(case, fallback=lambda: template_request)
        request = run.request or template_request
        result: dict[str, Any] = {
            "case": case.to_dict(),
            "agent": run.to_dict(),
            "request": request.to_dict(),
            "verdict": None,
            "latency": {"mapping_latency_ms": round(run.mapping_latency_ms, 3),
                        "gate_latency_ms": None},
            "references": available_references(),
            "disclaimer": DATA_DISCLAIMER,
        }
        if payload.evaluate:
            verdict = pipeline.evaluate(request)
            result["verdict"] = verdict.to_dict()
            result["latency"]["gate_latency_ms"] = round(verdict.decision_latency_ms, 3)
            state["demo_cases"][case.case_id] = {
                "case": case.to_dict(), "request": request.to_dict(),
                "verdict": verdict.to_dict(), "seq": -1, "agent": run.to_dict(),
            }
            publish("agent_run", result)
        return result

    @app.get("/api/gate/agent")
    def gate_agent_info() -> dict[str, Any]:
        """被治理的 Agent 長什麼樣:模式、七個工具的 schema、可跑的情境。"""
        from ..agent import SYSTEM_PROMPT, TOOL_SPECS

        settings = AgentSettings.from_env()
        return {
            "mode": settings.mode,
            "llm_available": settings.llm_enabled,
            "model": settings.model,
            "max_tokens": settings.max_tokens,
            "system_prompt": SYSTEM_PROMPT,
            "tools": TOOL_SPECS,
            "references": available_references(),
            "note": "來源鏈由 runtime 注入,模型無從自報;模型輸出須通過 G1 schema 驗證。",
        }

    @app.get("/api/gate/metrics")
    def gate_metrics() -> dict[str, Any]:
        """§5.2 指標:線上可觀測的即時值 + 最近一次驗證管線結果。"""
        stats = scenario_stats(build_scenarios())
        return {
            "live": pipeline.metrics.summary(),
            "audit": pipeline.audit.completeness(),
            "chain": pipeline.audit.verify(),
            "policy_version": pipeline.engine.version()["policy_version"],
            "benchmark": state["benchmark"],
            "scenario_stats": stats,
            "note": (
                f"HAR/TCR/FBR/ESB 需要情境標註,由驗證管線在 {stats['total']} 條測試集上計算;"
                "FBR 拆成 FBR_gate(被 G0/G2/G3 擋下)與 FBR_approver(被核准者駁回),"
                "兩者相加即 FBR;live 區塊為線上即時值(決策延遲、各關卡攔截數)。"),
        }

    # ---------------------------------------------------------------- 營運實境
    @app.get("/api/ops/state")
    def ops_state(since: int = 0, stream: int = 40, minutes: int = 30) -> dict[str, Any]:
        """營運中心的複合狀態:當班摘要 + 每分鐘流量 + 最近事件流。"""
        tick()
        now = datetime.now(timezone.utc)
        return {
            "enabled": state["live_ops"],
            "summary": ops.summary(now),
            "traffic": ops.traffic(minutes=minutes, now=now),
            "stream": [row for row in ops.recent(stream) if row["seq"] >= since],
            "cursor": ops.records[-1].seq if ops.records else -1,
            "disclaimer": DATA_DISCLAIMER,
        }

    @app.get("/api/ops/case/{case_id}")
    def ops_case(case_id: str) -> dict[str, Any]:
        """單一工單的完整檔案:對話逐字、附件原文、動作請求、裁決與稽核紀錄。"""
        record = find_case(case_id)
        if record is None:
            raise HTTPException(404, f"找不到工單 {case_id}")
        trace_id = record["request"].get("trace_id", "")
        return {
            **record,
            "audit": pipeline.audit.to_list(trace_id=trace_id) if trace_id else [],
        }

    @app.get("/api/ops/templates")
    def ops_templates() -> dict[str, Any]:
        """可手動投入的情境樣板(現場讓評審點名要看哪一種)。"""
        return {
            "templates": [
                {"template_id": t.template_id, "label": t.label,
                 "channel": t.channel, "class": t.klass, "attack_type": t.attack_type}
                for t in CASE_TEMPLATES
            ]
        }

    @app.post("/api/ops/inject")
    def ops_inject(payload: InjectPayload) -> dict[str, Any]:
        """立刻投入一件指定情境的工單,走完整治理管線。"""
        template = TEMPLATES_BY_ID.get(payload.template_id)
        if template is None:
            raise HTTPException(404, f"未知情境樣板 {payload.template_id}")
        record = ops.inject(template)
        publish("ops_case", record.summary())
        return record.to_dict()

    @app.post("/api/ops/stream")
    def ops_stream(payload: StreamPayload) -> dict[str, Any]:
        """暫停/恢復當班流量(演示時把背景流量停住,畫面才不會搶戲)。"""
        ops.set_running(payload.running)
        state["live_ops"] = payload.running
        return {"running": ops.running}

    @app.get("/api/ops/accounts")
    def ops_accounts(q: str = "", limit: int = 40) -> dict[str, Any]:
        """影子後台帳戶查詢。G3 預演說「會撈出 N 筆」,這裡就是那 N 筆的來源。"""
        hits = shadow.search_accounts(q, limit)
        return {
            "total": len(shadow.accounts),
            "matched": len(hits),
            "accounts": [a.summary() for a in hits],
            "synthetic": True,
        }

    @app.get("/api/ops/account/{account_id}")
    def ops_account(account_id: str) -> dict[str, Any]:
        account = shadow.get_account(account_id)
        if account is None:
            raise HTTPException(404, f"找不到帳戶 {account_id}")
        return {
            "account": account.summary(),
            "history": shadow.account_history(account_id),
            "plans": PLANS,
        }

    # ---------------------------------------------------------------- 驗證管線
    @app.post("/api/benchmark")
    def benchmark_run() -> dict[str, Any]:
        report = run_validation()
        state["benchmark"] = report
        return report

    @app.get("/api/benchmark")
    def benchmark_get() -> dict[str, Any]:
        if state["benchmark"] is None:
            raise HTTPException(404, "尚未執行驗證管線;請先 POST /api/benchmark")
        return state["benchmark"]

    # ---------------------------------------------------------------- Demo 導播(§7.2)
    @app.get("/api/demo/script")
    def demo_script() -> dict[str, Any]:
        settings = AgentSettings.from_env()
        return {
            "script": DEMO_SCRIPT,
            "gate_enabled": pipeline.config.gate_enabled,
            "agent": {
                # 沒有金鑰時前端仍然可以按「真 Agent」,只是會誠實顯示降級。
                "llm_available": settings.llm_enabled,
                "mode": settings.mode,
                "live_steps": list(LIVE_AGENT_STEPS),
            },
            "disclaimer": DATA_DISCLAIMER,
        }

    @app.post("/api/demo/step")
    def demo_step(payload: DemoStepPayload) -> dict[str, Any]:
        step = next((s for s in DEMO_SCRIPT if s["step_id"] == payload.step_id), None)
        if step is None:
            raise HTTPException(404, f"未知 Demo 步驟 {payload.step_id}")
        agent = get_agent() if payload.agent == "llm" else None

        def run(step_id: str) -> tuple[dict[str, Any], Any, dict[str, Any] | None]:
            """跑一個劇本步驟,並把工單留下來供「案件檢視」回查。"""
            case, request, agent_run = build_step_case_with_agent(step_id, agent)
            verdict = pipeline.evaluate(request)
            state["demo_cases"][case.case_id] = {
                "case": case.to_dict(), "request": request.to_dict(),
                "verdict": verdict.to_dict(), "seq": -1, "agent": agent_run,
            }
            return case.to_dict(), verdict, agent_run

        if step["action"] == "evaluate":
            case, verdict, agent_run = run(payload.step_id)
            result = {
                "step": step, "case": case, "verdict": verdict.to_dict(),
                "agent": agent_run,
                # 決策延遲拆成兩段。混成一個數字的話,「治理層很慢」與
                # 「模型很慢」就分不出來了 —— 而 AgentGate 只為後半段負責。
                "latency": {
                    "mapping_latency_ms": round(agent_run["mapping_latency_ms"], 3)
                                          if agent_run else None,
                    "gate_latency_ms": round(verdict.decision_latency_ms, 3),
                },
            }
            publish("demo_step", result)
            return result

        if step["action"] == "ab_compare":
            # 關閉治理層 → 同一份 PDF 的指令直接執行 → 自動重新開啟
            case, gated, agent_run = run("injection")
            pipeline.config.gate_enabled = False
            _, ungated, agent_run_ungated = run("injection_ungated")
            pipeline.config.gate_enabled = True
            result = {
                "step": step,
                "case": case,
                "gated": gated.to_dict(),
                "ungated": ungated.to_dict(),
                "agent": agent_run,
                "agent_ungated": agent_run_ungated,
                "latency": {
                    "mapping_latency_ms": round(agent_run["mapping_latency_ms"], 3)
                                          if agent_run else None,
                    "gate_latency_ms": round(gated.decision_latency_ms, 3),
                },
                "gate_enabled": True,
            }
            state["ab_result"] = result
            publish("ab_compare", result)
            return result

        # goto_audit / goto_metrics:純導覽步驟,前端切換分頁
        publish("demo_step", {"step": step})
        return {"step": step}

    @app.post("/api/demo/gate")
    def demo_gate(payload: GateTogglePayload) -> dict[str, Any]:
        """A/B 對照的手動開關。切換本身也寫入稽核鏈(降級也留痕)。"""
        pipeline.config.gate_enabled = payload.enabled
        pipeline.audit.append(
            trace_id="demo-control", action_id="gate-toggle",
            stage="degrade" if not payload.enabled else "restore",
            actor="demo-operator", gate_enabled=payload.enabled,
        )
        return {"gate_enabled": pipeline.config.gate_enabled}

    @app.post("/api/demo/tamper")
    def demo_tamper(payload: TamperPayload) -> dict[str, Any]:
        """竄改演示:改掉一筆稽核紀錄,讓 verify 當場抓出來(僅 Demo)。"""
        ok = pipeline.audit.tamper_for_demo(payload.seq, payload.detail)
        if not ok:
            raise HTTPException(404, f"稽核鏈中沒有第 {payload.seq} 筆")
        return {"tampered_seq": payload.seq, "verify": pipeline.audit.verify()}

    @app.post("/api/demo/reset")
    def demo_reset() -> dict[str, Any]:
        shadow.reset()
        pipeline.audit.reset()
        pipeline.queue.clear()
        pipeline.metrics.reset()
        pipeline.config.gate_enabled = True
        state["benchmark"] = None
        state["demo_events"] = []
        state["ab_result"] = None
        state["demo_cases"] = {}
        ops.reset()
        # 重設後仍然回填當班歷史:歸零的意思是「回到剛接班的樣子」,
        # 不是「回到一個從來沒運作過的系統」。
        if state["live_ops"]:
            ops.warm_start(minutes=warm_minutes)
        return {"ok": True, "live_ops": state["live_ops"]}

    @app.get("/api/demo/events")
    def demo_events(since: int = 0) -> dict[str, Any]:
        events = [e for e in state["demo_events"] if e["seq"] >= since]
        return {"events": events, "seq": len(state["demo_events"])}

    @app.get("/api/state")
    def composite_state() -> dict[str, Any]:
        """前端輪詢用的複合狀態(順便推進營運時鐘)。"""
        tick()
        now = datetime.now(timezone.utc)
        pending = pipeline.queue.pending()
        return {
            "gate_enabled": pipeline.config.gate_enabled,
            "pending_count": len(pending),
            "pending_breached": sum(1 for p in pending if p.sla_state(now)["breached"]),
            "audit_total": len(pipeline.audit.records),
            "metrics": pipeline.metrics.summary(),
            "executed_log": shadow.executed_log[-5:],
            "has_benchmark": state["benchmark"] is not None,
            "live_ops": state["live_ops"],
            "ops_cases": len(ops.records),
            "duty": current_shift(now.astimezone()),
        }

    # ---------------------------------------------------------------- 前端
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        path = STATIC_DIR / "index.html"
        if not path.exists():
            return HTMLResponse("<h1>AgentGate</h1><p>缺少靜態檔 index.html。</p>",
                                status_code=500)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/scenario", response_class=HTMLResponse)
    def scenario_page() -> HTMLResponse:
        """使用場景說明。

        管制台回答「它在運作」,這一頁回答「它在管什麼」:誰在受理、AI 客服 Agent
        手上有哪七種後台權限、三種攻擊型態怎麼發生、治理層卡在哪一段,以及邊界與
        合成資料聲明。純靜態說明頁,不打任何 API —— 頁面上的數字對應程式碼常數
        (ACTION_POLICY、REFUND_*_LIMIT、BULK_FORBIDDEN_COUNT、SLA_SECONDS、
        CHANNELS/SEATS/SHIFTS、scenario_stats),改動常數時要一併更新這一頁。
        """
        path = STATIC_DIR / "scenario.html"
        if not path.exists():
            return HTMLResponse("<h1>AgentGate</h1><p>缺少靜態檔 scenario.html。</p>",
                                status_code=500)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/mechanism", response_class=HTMLResponse)
    def mechanism_page() -> HTMLResponse:
        """運作機制視角。

        主要入口是 Dashboard 的「運作機制」分頁;這一頁是同一支自足模組
        (static/mechanism.js)的獨立入口,供提案截圖與嵌入用 ——
        兩邊掛的是同一份說明,不會各自漂移。
        """
        path = STATIC_DIR / "mechanism.html"
        if not path.exists():
            return HTMLResponse("<h1>AgentGate</h1><p>缺少靜態檔 mechanism.html。</p>",
                                status_code=500)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    return app


app = create_app()

__all__ = ["app", "create_app"]
