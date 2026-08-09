"""FastAPI ＋ WebSocket 即時儀表板 —— 六分鐘舞台 Demo 的視覺化層。

閉環是同步的阻塞式流程（人工核准本來就該阻塞），WebSocket 是非同步的。
兩者用「工作執行緒 ＋ loop.call_soon_threadsafe」橋接：

    orchestrator.run()  ← 跑在 worker thread，approval_fn 在這裡阻塞
        │ emit(kind, payload)
        ▼
    asyncio.Queue  ← 由事件迴圈消費，推播給瀏覽器
        │
        ▼
    WebSocket ──→ 瀏覽器按下「核准」──→ Future.set_result() ──→ 解除 worker 阻塞

每個連線各自建立一個 Orchestrator（各自有獨立的孿生），多人同時觀看不會互相污染。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import SETTINGS
from ..domain import NetworkSnapshot, RecoveryPlan
from ..llm import LLMClient
from ..optimizer import STRATEGY_LABELS
from ..orchestrator import Orchestrator
from ..twin.engine import WAN_LABEL, DigitalTwin
from ..twin.scenarios import SCENARIOS, get_scenario

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

# 12 節點的固定版面。放在後端，拓樸調整時前端不必跟著改。
# NODE_W/NODE_H 也由後端決定，才能保證最右一欄不會被畫布切掉。
NODE_W, NODE_H = 134, 34
CANVAS_W, CANVAS_H = 980, 430

LAYOUT: dict[str, tuple[int, int]] = {
    "ward-ed":    (30, 58),
    "ward-icu":   (30, 158),
    "blk-img":    (30, 258),
    "blk-adm":    (30, 358),
    "core-sw":    (235, 208),
    "cpe-fiber":  (440, 88),
    "cpe-5g":     (440, 208),
    "sat-vsat":   (440, 328),
    "pop-fiber":  (640, 88),
    "gnb-5g":     (640, 208),
    "gw-sat":     (640, 328),
    "dc-hicloud": (835, 208),
}
assert all(x + NODE_W <= CANVAS_W for x, _ in LAYOUT.values()), "節點超出畫布寬度"

# 拓樸圖上的短名稱。刻意避開 CPE / VSAT / POP / gNB 這類縮寫 ——
# 評審不一定是電信背景，看不懂方框就看不懂整張圖。每個方框下方仍印出節點
# 原始 ID（cpe-fiber、gnb-5g…），技術背景的讀者要對照設備一樣找得到。
SHORT_NAME: dict[str, str] = {
    "ward-ed": "急診室",
    "ward-icu": "加護病房",
    "blk-img": "影像與遠距診療",
    "blk-adm": "行政與訪客區",
    "core-sw": "院內網路中樞",
    "cpe-fiber": "固網出口（主要）",
    "cpe-5g": "5G 出口（備援）",
    "sat-vsat": "衛星天線",
    "pop-fiber": "中華電信機房",
    "gnb-5g": "5G 基地台",
    "gw-sat": "衛星地面站",
    "dc-hicloud": "醫療雲端機房",
}

@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """啟動時就把三個唯讀 payload 算完，別留給第一個瀏覽器請求。

    代價幾乎全在 `_health_payload` 裡的 `import openai`（約 0.25s，且只有
    設了金鑰才會走到）。戰情室開場是三個 fetch 並發，那 0.25s 若發生在
    事件迴圈上，會連帶把 topology / scenarios 一起拖住 —— 首屏整個變慢。
    移到啟動階段之後，開場只剩下純粹的網路往返。
    """
    for build in (_topology_payload, _scenarios_payload, _health_payload):
        await asyncio.to_thread(build)
    yield


app = FastAPI(title="AegisMesh 天穹韌網", version="0.1.0", lifespan=_lifespan)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ------------------------------------------------------------------ 序列化


def _plan_changes(plan: RecoveryPlan, twin: DigitalTwin) -> list[dict[str, Any]]:
    """把計畫改寫成「每個醫療服務一列」的推演結果。

    最佳化器的動作是以「動作」為單位的：同一個服務常常拆成兩筆（改走 5G
    ＋ 限速至 40M），而頻寬沒變的服務根本不會產生動作。核准畫面照抄動作
    就會變成八行零散指令，還有一半服務憑空消失或顯示「不變」。

    所以這裡不讀動作，直接讀影子孿生推演出來的結果，並與「完整服務所需頻寬」
    對照 —— 值班主管要判斷的是「核准之後每項服務拿到的夠不夠」。
    """
    if plan.projected is None:
        return []

    rows: list[dict[str, Any]] = []
    for sid, svc in twin.services.items():
        state = plan.projected.services.get(sid)
        if state is None:
            continue
        wan = next(
            (WAN_LABEL[twin.links[lid].kind.value] for lid in state.link_ids
             if twin.links[lid].kind.value in WAN_LABEL),
            None,
        )
        rows.append({
            "name": svc.name,
            "priority": svc.priority,
            "critical": svc.is_critical,
            "clinical_note": svc.clinical_note,
            "wan": wan,
            "mbps": round(state.admitted_mbps, 1),
            # 用災害期間的需求，不是平常的需求 —— 否則急診拿到 27M 會被
            # 標成「完整服務」，但這場災害裡它其實需要 50M
            "required_mbps": round(twin.required_mbps(sid), 1),
            "reachable": state.reachable,
        })
    return sorted(rows, key=lambda r: r["priority"])


def _plan_brief(plan: RecoveryPlan, twin: DigitalTwin) -> dict[str, Any]:
    # 動作說明交給孿生渲染：它握有業務名稱，才能把 svc-ed-vitals 講成
    # 「急診生命徵象即時串流」。原始 ID 仍在稽核軌跡裡。
    pr = plan.projected
    return {
        "id": plan.id,
        "strategy": plan.strategy,
        "strategy_label": STRATEGY_LABELS.get(plan.strategy, plan.strategy),
        "actions": [twin.describe_action(a) for a in plan.actions],
        "action_count": len(plan.actions),
        "changes": _plan_changes(plan, twin),
        "score": round(plan.score, 1),
        "risk_score": round(plan.risk_score, 1),
        "policy_decision": plan.policy_decision,
        "policy_findings": plan.policy_findings,
        "findings": plan.policy_findings_detail,
        "llm_rationale": plan.llm_rationale,
        "projected": {
            "critical_availability_pct": round(pr.critical_availability_pct, 1),
            "slo_compliance_pct": round(pr.slo_compliance_pct, 1),
            "monthly_cost_ntd": round(pr.monthly_cost_ntd),
            "quota_hours_left": {
                k: (None if v == float("inf") else round(v, 1))
                for k, v in pr.quota_hours_left.items()
            },
        } if pr else None,
    }


def _serialize(kind: str, payload: Any, twin: DigitalTwin) -> dict[str, Any]:
    if kind in {"baseline", "incident"} and isinstance(payload, NetworkSnapshot):
        return {"kind": kind, "snapshot": payload.to_dict()}
    if kind == "agent":
        return {"kind": kind, "agent": payload.to_dict()}
    if kind == "plans":
        selected = payload["selected"]
        return {
            "kind": kind,
            "plans": [_plan_brief(p, twin) for p in payload["plans"]],
            "selected": selected.id if selected else None,
        }
    if kind == "approval":
        return {
            "kind": kind,
            "plan_id": payload["plan"].id,
            "approved": payload["approved"],
            "human_gate": payload["human_gate"],
        }
    if kind == "execute":
        return {"kind": kind, "plan_id": payload["plan"].id, "applied": payload["applied"]}
    if kind in {"agent_start", "replan", "replan_exhausted"}:
        return {"kind": kind, **payload}
    return {"kind": kind}


# ------------------------------------------------------------------ REST


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/topology")
async def topology() -> JSONResponse:
    return JSONResponse(_topology_payload())


@lru_cache(maxsize=1)
def _topology_payload() -> dict[str, Any]:
    """拓樸與基準狀態是唯讀資料，只建構一次。"""
    twin = DigitalTwin()
    data = twin.topology_dict()
    for node in data["nodes"]:
        x, y = LAYOUT.get(node["id"], (0, 0))
        node["x"], node["y"] = x, y
        node["short"] = SHORT_NAME.get(node["id"], node["name"])
    data["canvas"] = {"w": CANVAS_W, "h": CANVAS_H, "node_w": NODE_W, "node_h": NODE_H}
    data["baseline"] = twin.evaluate("baseline").to_dict()
    return data


@app.get("/api/scenarios")
async def scenarios() -> JSONResponse:
    return JSONResponse(_scenarios_payload())


@lru_cache(maxsize=1)
def _scenarios_payload() -> list[dict[str, Any]]:
    twin = DigitalTwin()
    return [
        {
            "id": s.id,
            "name": s.name,
            "narrative": s.narrative,
            "duration_hours": s.duration_hours,
            "faults": [
                {
                    "link": f.link_id,
                    "state": f.state.value,
                    "description": f.description,
                    "netem": f.netem_command(
                        capacity_mbps=twin.links[f.link_id].capacity_mbps
                    ),
                }
                for f in s.faults
            ],
        }
        for s in SCENARIOS.values()
    ]


@app.get("/api/health")
async def health() -> JSONResponse:
    # 正常情況下 _lifespan 已經預熱過，這裡直接命中快取。沒預熱到的話
    # （例如測試直接建 TestClient）第一次會 import openai，丟到執行緒跑，
    # 才不會佔住事件迴圈拖慢同時併發的其他請求。
    return JSONResponse(await asyncio.to_thread(_health_payload))


@lru_cache(maxsize=1)
def _health_payload() -> dict[str, Any]:
    """避免每次健康檢查都建立 OpenAI HTTP client 與連線池。"""
    llm = LLMClient(SETTINGS)
    return {
        "status": "ok",
        "llm_online": llm.online,
        "llm_mode": llm.mode,
        "require_approval": SETTINGS.require_approval,
    }


# ------------------------------------------------------------------ WebSocket


class DemoSession:
    """一條 WebSocket 連線的閉環執行狀態。

    核准結果刻意用 threading 原語（Event）而非 asyncio Future 傳遞。
    早期版本走 run_coroutine_threadsafe，結果是：瀏覽器在核准前關掉分頁時，
    worker thread 會卡在 fut.result() 直到逾時 —— 因為結果的「送達」依賴
    事件迴圈還活著。改用 Event 之後，close() 可以立刻解除阻塞，
    重複連線也不會累積殭屍執行緒。
    """

    APPROVAL_TIMEOUT_SEC = 300.0

    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop) -> None:
        self.ws = ws
        self.loop = loop
        self.out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.orch = Orchestrator()

        self._closed = threading.Event()
        self._approval_ready = threading.Event()
        self._approval_result = False

    # --- 由 worker thread 呼叫（絕不觸碰 asyncio 物件）--------------------

    def _push(self, message: dict[str, Any]) -> None:
        """跨執行緒把訊息排進推播佇列。連線已關就直接丟棄。"""
        if self._closed.is_set():
            return
        try:
            self.loop.call_soon_threadsafe(self.out.put_nowait, message)
        except RuntimeError:                       # 事件迴圈已關閉
            self._closed.set()

    def emit(self, kind: str, payload: Any) -> None:
        self._push(_serialize(kind, payload, self.orch.twin))

    def request_approval(self, plan: RecoveryPlan) -> bool:
        """在 worker thread 阻塞，直到瀏覽器回覆、連線中斷、或逾時。"""
        if self._closed.is_set():
            return False

        self._approval_result = False
        self._approval_ready.clear()
        self._push({"kind": "approval_request", "plan": _plan_brief(plan, self.orch.twin)})

        if not self._approval_ready.wait(timeout=self.APPROVAL_TIMEOUT_SEC):
            logger.warning("核准逾時（%.0fs），視為拒絕", self.APPROVAL_TIMEOUT_SEC)
            return False
        return self._approval_result

    # --- 由事件迴圈呼叫 --------------------------------------------------

    def resolve_approval(self, approved: bool) -> None:
        self._approval_result = approved
        self._approval_ready.set()

    def close(self) -> None:
        """連線結束：務必解除 worker thread 的阻塞，否則執行緒會洩漏。"""
        self._closed.set()
        self.resolve_approval(False)

    # --- 閉環 ----------------------------------------------------------

    async def run(self, scenario_id: str, max_rounds: int = 2) -> None:
        try:
            scenario = get_scenario(scenario_id)
        except KeyError:
            await self.out.put({"kind": "error", "message": f"未知情境：{scenario_id}"})
            return
        await self.out.put({
            "kind": "started",
            "scenario": {"id": scenario.id, "name": scenario.name,
                         "narrative": scenario.narrative,
                         "duration_hours": scenario.duration_hours},
            "llm_mode": self.orch.llm.mode,
            "faults": [
                {
                    "link": f.link_id, "state": f.state.value, "description": f.description,
                    "netem": f.netem_command(
                        capacity_mbps=self.orch.twin.links[f.link_id].capacity_mbps
                    ),
                }
                for f in scenario.faults
            ],
        })

        result = await asyncio.to_thread(
            self.orch.run, scenario, self.request_approval, max_rounds, self.emit
        )
        ok, msg = self.orch.verify_audit()
        await self.out.put({
            "kind": "finished",
            "summary": result.summary(),
            "final": result.final.to_dict(),
            "audit": {
                "path": str(result.audit_path),
                "verified": ok,
                "message": msg,
                "entries": self.orch.audit.count(),
            },
        })


@app.websocket("/ws/demo")
async def ws_demo(ws: WebSocket) -> None:
    await ws.accept()
    session = DemoSession(ws, asyncio.get_running_loop())

    async def pump() -> None:
        """把佇列中的事件持續推播給瀏覽器。"""
        while True:
            await ws.send_json(await session.out.get())

    pump_task = asyncio.create_task(pump())
    run_task: asyncio.Task | None = None

    try:
        while True:
            msg = await ws.receive_json()
            action = msg.get("action")

            if action == "run":
                if run_task and not run_task.done():
                    await session.out.put({"kind": "error", "message": "已有閉環執行中"})
                    continue
                try:
                    max_rounds = int(msg.get("max_rounds", 2))
                except (TypeError, ValueError):
                    await session.out.put({"kind": "error", "message": "max_rounds 必須是整數"})
                    continue
                if not 1 <= max_rounds <= 10:
                    await session.out.put({"kind": "error", "message": "max_rounds 必須介於 1 到 10"})
                    continue
                run_task = asyncio.create_task(
                    session.run(str(msg.get("scenario", "typhoon-fiber-cut")), max_rounds)
                )
            elif action == "approve":
                session.resolve_approval(bool(msg.get("approved")))
            elif action == "ping":
                await session.out.put({"kind": "pong"})

    except WebSocketDisconnect:
        logger.info("瀏覽器已離線")
    except Exception as exc:
        logger.exception("WebSocket 發生錯誤：%s", exc)
    finally:
        # 順序很重要：先解除 worker thread 的阻塞，再取消 asyncio 任務。
        # 反過來的話，正在等核准的執行緒會失去被喚醒的機會。
        session.close()
        pump_task.cancel()
        if run_task:
            run_task.cancel()
        tasks = [pump_task, *([run_task] if run_task else [])]
        with suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)


def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    import uvicorn

    uvicorn.run("aegismesh.api.server:app" if reload else app,
                host=host, port=port, reload=reload, log_level="info")
