"""Dashboard 用的即時 Demo Session。

閉環是同步阻塞的程式碼，Dashboard 需要即時看到每個階段，
所以閉環跑在工作執行緒裡，透過 ``on_stage`` 把事件推進事件緩衝區，
前端再用 SSE 讀出來。人工核准則是讓工作執行緒在 ``threading.Event`` 上等待，
直到有人從 API 送出核准或退回 —— 這就是規格 §9.1 的 Human-in-the-loop。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..agents.base import AgentContext
from ..audit import AuditLog
from ..config import Settings, get_settings
from ..domain import ApprovalDecision, RecoveryPlan, Severity
from ..orchestrator import LoopResult, Orchestrator
from ..policy.engine import PolicyDecision, PolicyEngine
from ..prediction import ForecastService
from ..twin.engine import FactoryTwin
from ..twin.scenarios import get_scenario

MAX_EVENTS = 4000
APPROVAL_TIMEOUT_S = 300.0


@dataclass
class PendingApproval:
    plan: RecoveryPlan
    decision: PolicyDecision
    event: threading.Event = field(default_factory=threading.Event)
    result: ApprovalDecision | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"plan": self.plan.to_dict(), "policy": self.decision.to_dict()}


class DemoSession:
    """一個 Dashboard Session：一座孿生工廠 + 一組 Agent + 一份稽核軌跡。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.seq = 0
        self.pending: PendingApproval | None = None
        self.running = False
        self.scenario_id: str | None = None
        self.last_loop: LoopResult | None = None
        self.live: dict[str, Any] = {}     # 閉環進行中的各階段產出（供頁面重新整理後還原）
        self.history: list[dict[str, Any]] = []      # 給前端畫趨勢圖用的時間序列
        self.machine_logs: list[dict[str, Any]] = []  # 給 Demo / 外部 adapter 的機台遙測批次
        self.started_at = datetime.now(timezone.utc)
        # 整個 session 共用一個 ForecastService：TabFM 權重載入約 8 秒，
        # 每次 reset() 重建會讓換案例都付一次這個成本。
        self.prediction = ForecastService()
        self.reset()

    # ------------------------------------------------------------------ 生命週期
    def reset(self, scenario_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            self.audit = AuditLog(settings=self.settings)
            self.ctx = AgentContext(
                settings=self.settings,
                audit=self.audit,
                policy=PolicyEngine(require_approval=self.settings.require_approval),
            )
            self.twin = FactoryTwin(seed=self.settings.seed, tick_minutes=self.settings.tick_seconds / 60.0)
            self.orch = Orchestrator(
                twin=self.twin, ctx=self.ctx, approval=self._request_approval, on_stage=self._on_stage,
                forecaster=self.prediction,
            )
            self.events = []
            self.seq = 0
            self.pending = None
            self.running = False
            self.last_loop = None
            self.live = {}
            self.history = []
            self.machine_logs = []
            self.started_at = datetime.now(timezone.utc)
            self.scenario_id = scenario_id
            if scenario_id:
                self.twin.schedule(get_scenario(scenario_id).injections)
        self._record_history()
        self._record_machine_logs(self.twin.snapshot(), source="session_reset")
        self._push("reset", {"scenario_id": scenario_id, "run_id": self.audit.run_id})
        return self.state()

    def inject(self, scenario_id: str) -> dict[str, Any]:
        scenario = get_scenario(scenario_id)
        with self._lock:
            self.scenario_id = scenario_id
            # 相對於「現在」注入，讓評審按下按鈕就看到訊號開始走。
            for injection in scenario.injections:
                shifted = type(injection)(
                    fault_id=injection.fault_id,
                    machine_id=injection.machine_id,
                    start_tick=self.twin.tick + max(0, injection.start_tick - 2),
                    ramp_ticks=injection.ramp_ticks,
                    max_progress=injection.max_progress,
                )
                self.twin.schedule([shifted])
        self.audit.log("inject", "operator", scenario=scenario_id,
                       note="由 Dashboard 手動注入故障；標籤僅存在 Simulator 內部。")
        self._push("inject", {"scenario_id": scenario_id, "title": scenario.title})
        return self.state()

    # ------------------------------------------------------------------ 推進
    def tick(self, count: int = 1) -> dict[str, Any]:
        for _ in range(max(1, count)):
            snapshot = self.twin.step()
            self.orch.monitoring.detect(snapshot, self.twin.topo)
            self.orch.safety.perceive(snapshot)
            self._record_history()
            self._record_machine_logs(snapshot, source="simulator_tick")
        self._push("tick", {"tick": self.twin.tick})
        return self.state()

    def run_loop(self, max_ticks: int = 40) -> None:
        """在工作執行緒裡跑完整閉環。"""
        if self.running:
            return
        self.running = True
        thread = threading.Thread(target=self._run_loop_worker, args=(max_ticks,), daemon=True)
        thread.start()

    def _run_loop_worker(self, max_ticks: int) -> None:
        try:
            event = self.orch.run_until_event(max_ticks, min_severity=Severity.WARNING)
            if event is None:
                self._push("idle", {"message": "在觀察視窗內未偵測到需要處理的異常。"})
                return
            self.last_loop = self.orch.handle_event(event)
            self._push("loop_done", {"result": self.last_loop.to_dict()})
        except Exception as exc:  # Demo 不能因為單一錯誤整個掛掉
            self._push("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            self.running = False
            self.pending = None
            self._record_history()

    # ------------------------------------------------------------------ 人工核准
    def _request_approval(self, plan: RecoveryPlan, decision: PolicyDecision) -> ApprovalDecision:
        pending = PendingApproval(plan=plan, decision=decision)
        self.pending = pending
        self._push("approval_required", pending.to_dict())
        if not pending.event.wait(timeout=APPROVAL_TIMEOUT_S):
            self.pending = None
            return ApprovalDecision(plan.plan_id, False, "timeout", "等待人工核准逾時，依安全原則不執行。")
        self.pending = None
        return pending.result or ApprovalDecision(plan.plan_id, False, "unknown", "未取得有效核准。")

    def submit_approval(self, plan_id: str, approved: bool, approver: str, reason: str = "") -> bool:
        pending = self.pending
        if pending is None or pending.plan.plan_id != plan_id:
            return False
        pending.result = ApprovalDecision(plan_id, approved, approver, reason, auto=False)
        pending.event.set()
        self._push("approval_submitted", {"plan_id": plan_id, "approved": approved, "approver": approver})
        return True

    # ------------------------------------------------------------------ 事件與狀態
    # 這些階段的產出要留在 session 裡，讓中途重新整理頁面不會把面板洗白。
    _LIVE_STAGES = ("diagnose", "impact", "plan", "safety", "rank", "work_order", "execute", "verify")

    def _on_stage(self, stage: str, payload: dict[str, Any]) -> None:
        if stage == "tick":
            self._record_history()
        elif stage in self._LIVE_STAGES:
            # 閉環跑到一半時，last_loop 還是 None —— 這時候如果評審按了重新整理，
            # 所有面板都會變回空的。所以每個階段的產出即時留存一份。
            self.live[stage] = payload
        elif stage == "detect":
            self.live["detect"] = payload
        self._push(stage, payload)

    def _push(self, stage: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "stage": stage, "payload": payload})
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]

    def events_since(self, since: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self.events if e["seq"] > since]

    def _record_history(self) -> None:
        snapshot = self.twin.snapshot()
        point: dict[str, Any] = {
            "tick": snapshot.tick,
            "production_pct": round(snapshot.production_pct, 1),
            "factory_health": round(snapshot.factory_health, 1),
        }
        for mid, machine in snapshot.machines.items():
            point[f"{mid}.health"] = round(machine.health, 1)
            for name, reading in machine.readings.items():
                point[f"{mid}.{name}"] = round(reading.value, 2)
        with self._lock:
            self.history.append(point)
            if len(self.history) > 400:
                del self.history[: len(self.history) - 400]

    def _record_machine_logs(self, snapshot: Any, source: str) -> None:
        """把一個 Twin snapshot 轉成接近 PLC/MES 的遙測批次。

        這裡只使用 Agent 可見的 snapshot，不會把 fault、fault_progress 或
        其他 Ground Truth 寫進 Demo log。每個 tick 對每台機台產生一筆心跳，
        方便畫面展示，也讓未來替換成 OPC-UA/MQTT adapter 時保持相同 schema。
        """
        timestamp = self.started_at + timedelta(minutes=snapshot.sim_minutes)
        rows: list[dict[str, Any]] = []
        for machine_id, machine in snapshot.machines.items():
            worst_band = machine.worst_band.value
            if not machine.online:
                message = "machine offline / maintenance state"
            elif worst_band == "critical":
                message = "sensor threshold exceeded"
            elif worst_band == "warning":
                message = "sensor deviation detected"
            else:
                message = "heartbeat nominal"
            rows.append({
                "timestamp": timestamp.isoformat(timespec="seconds"),
                "tick": snapshot.tick,
                "sim_minutes": round(snapshot.sim_minutes, 1),
                "source": source,
                "machine_id": machine_id,
                "machine_name": machine.name,
                "state": machine.state.value,
                "online": machine.online,
                "health": round(machine.health, 1),
                "utilization_pct": round(machine.utilization_pct, 1),
                "production_rate_uph": round(machine.production_rate_uph, 1),
                "worst_band": worst_band,
                "message": message,
                "readings": {
                    name: {
                        "value": reading.value,
                        "unit": reading.unit,
                        "band": reading.band.value,
                    }
                    for name, reading in machine.readings.items()
                },
            })
        with self._lock:
            self.machine_logs.extend(rows)
            if len(self.machine_logs) > 1200:
                del self.machine_logs[: len(self.machine_logs) - 1200]

    def logs_since(self, since_tick: int = 0, machine_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                row for row in self.machine_logs
                if row["tick"] >= since_tick and (machine_id is None or row["machine_id"] == machine_id)
            ]

    def prediction_history(self) -> list[dict[str, Any]]:
        """Return an immutable snapshot for model inference outside the session lock."""
        with self._lock:
            return [point.copy() for point in self.history]

    def state(self) -> dict[str, Any]:
        snapshot = self.twin.snapshot()
        monitoring = self.orch.monitoring.snapshot_summary(snapshot, self.twin.topo)
        return {
            "run_id": self.audit.run_id,
            "scenario_id": self.scenario_id,
            "running": self.running,
            "snapshot": snapshot.to_dict(),
            "monitoring": monitoring,
            "kpi": {k: round(v, 2) for k, v in self.twin.kpi().items()},
            "hazard_exposure_min": self.twin.hazard_exposure_min,
            "pending_approval": self.pending.to_dict() if self.pending else None,
            "last_loop": self.last_loop.to_dict() if self.last_loop else None,
            "live": self.live,
            "history": self.history[-120:],
            "machine_logs": self.machine_logs[-120:],
            "settings": self.settings.describe(),
        }


__all__ = ["DemoSession", "PendingApproval"]
