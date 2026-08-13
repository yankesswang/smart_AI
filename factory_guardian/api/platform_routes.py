"""中央管理平台 API（/api/v1）。

與既有的 Demo API（/api/*）並存但互不干擾：Demo API 服務展示頁面，
這組 API 服務正式維運主控台。差別在於這裡的每個寫入端點都：

1. 需要有效登入 token
2. 檢查角色權限
3. 以真實操作者身分寫入稽核

認證方式：``Authorization: Bearer <token>``，或 ``fg_token`` cookie
（讓瀏覽器導頁時不必自己帶 header）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..platform.adapters import describe_adapter_kinds
from ..platform.auth import ROLE_LABELS, ROLES, AuthError, Principal
from ..platform.bootstrap import Platform
from ..platform.services import (
    ALARM_STATES,
    WORK_ORDER_KINDS,
    WORK_ORDER_PRIORITIES,
    WORK_ORDER_STATES,
)
from ..twin.scenarios import SCENARIOS

TOKEN_COOKIE = "fg_token"


# --------------------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class AlarmAckRequest(BaseModel):
    note: str = ""


class AlarmResolveRequest(BaseModel):
    resolution: str = Field(min_length=1, max_length=2000)


class WorkOrderCreateRequest(BaseModel):
    site_id: str
    title: str = Field(min_length=1, max_length=300)
    machine_id: str = ""
    detail: str = ""
    kind: str = "corrective"
    priority: str = "normal"
    assignee: str = ""
    estimated_min: float = Field(default=0.0, ge=0, le=10_000)
    alarm_id: str | None = None


class WorkOrderUpdateRequest(BaseModel):
    state: str | None = None
    assignee: str | None = None
    priority: str | None = None
    completion_note: str | None = None


class ApprovalRequest(BaseModel):
    plan_id: str
    approved: bool
    reason: str = ""


class SiteControlRequest(BaseModel):
    autopilot: bool | None = None


class InjectRequest(BaseModel):
    scenario_id: str


class HandoverRequest(BaseModel):
    site_id: str
    shift: str = Field(min_length=1, max_length=40)
    summary: str = Field(min_length=1, max_length=4000)
    open_items: list[str] | None = None


class SiteCreateRequest(BaseModel):
    site_id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    region: str = ""
    adapter_kind: str = "simulated"
    adapter_config: dict[str, Any] = Field(default_factory=dict)
    autopilot: bool = True


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(min_length=1, max_length=80)
    role: str


class UserUpdateRequest(BaseModel):
    role: str | None = None
    enabled: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=200)


# --------------------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------------------
def build_platform_router(platform: Platform) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["platform"])

    # ------------------------------------------------------------------ 認證相依
    def current_principal(
        authorization: str | None = Header(default=None),
        fg_token: str | None = Cookie(default=None),
    ) -> Principal:
        token = fg_token
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        principal = platform.auth.resolve(token)
        if principal is None:
            raise HTTPException(401, "請先登入，或登入已逾期。")
        return principal

    def require(permission: str):
        """產生一個檢查特定權限的相依項。"""

        def checker(principal: Principal = Depends(current_principal)) -> Principal:
            if not principal.can(permission):
                raise HTTPException(
                    403,
                    f"角色「{ROLE_LABELS.get(principal.role, principal.role)}」"
                    f"沒有執行此操作的權限（需要 {permission}）。",
                )
            return principal

        return checker

    def site_runtime(site_id: str):
        try:
            return platform.fleet.runtime(site_id)
        except KeyError:
            raise HTTPException(404, f"未知站點 {site_id}") from None
        except NotImplementedError as exc:
            raise HTTPException(503, str(exc)) from exc

    # ------------------------------------------------------------------ 認證
    @router.post("/auth/login")
    def login(req: LoginRequest, response: Response) -> dict[str, Any]:
        try:
            token, principal = platform.auth.login(req.username, req.password)
        except AuthError as exc:
            platform.audit.record(req.username, "auth.login", outcome="denied",
                                  detail={"reason": exc.message})
            raise HTTPException(exc.status, exc.message) from exc
        # httponly：前端 JS 讀不到，降低 XSS 竊取 token 的風險。
        response.set_cookie(
            TOKEN_COOKIE, token, httponly=True, samesite="lax", max_age=12 * 3600, path="/"
        )
        platform.audit.record(principal.username, "auth.login", actor_role=principal.role)
        return {"token": token, "user": principal.to_dict()}

    @router.post("/auth/logout")
    def logout(
        response: Response,
        principal: Principal = Depends(current_principal),
        fg_token: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        if fg_token:
            platform.auth.logout(fg_token)
        response.delete_cookie(TOKEN_COOKIE, path="/")
        platform.audit.record(principal.username, "auth.logout", actor_role=principal.role)
        return {"ok": True}

    @router.get("/auth/me")
    def me(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
        return {"user": principal.to_dict()}

    # ------------------------------------------------------------------ Fleet
    @router.get("/fleet/overview")
    def fleet_overview(
        principal: Principal = Depends(require("fleet:read")),
    ) -> dict[str, Any]:
        return platform.fleet.overview()

    @router.get("/sites")
    def list_sites(
        principal: Principal = Depends(require("site:read")),
    ) -> dict[str, Any]:
        return {"sites": platform.fleet.site_records()}

    @router.get("/sites/{site_id}")
    def site_detail(
        site_id: str, principal: Principal = Depends(require("site:read"))
    ) -> dict[str, Any]:
        return site_runtime(site_id).detail()

    @router.get("/sites/{site_id}/topology")
    def site_topology(
        site_id: str, principal: Principal = Depends(require("site:read"))
    ) -> dict[str, Any]:
        topo = site_runtime(site_id).twin.topo
        return {
            "graph": topo.graph_json(),
            "machines": [
                {
                    "machine_id": m.machine_id, "name": m.name, "kind": m.kind.value,
                    "rated_rate_uph": m.rated_rate_uph, "products": list(m.products),
                    "signals": [
                        {"name": s.name, "unit": s.unit, "nominal": s.nominal,
                         "range": s.describe_range()}
                        for s in m.signals
                    ],
                }
                for m in topo.machines.values()
            ],
            "lines": [
                {"line_id": l.line_id, "name": l.name, "stages": [list(s) for s in l.stages]}
                for l in topo.lines.values()
            ],
        }

    @router.get("/sites/{site_id}/events")
    def site_events(
        site_id: str,
        principal: Principal = Depends(require("site:read")),
        since: int = 0,
    ) -> dict[str, Any]:
        runtime = site_runtime(site_id)
        return {"events": runtime.events_since(since), "seq": runtime.seq}

    @router.get("/sites/{site_id}/stream")
    async def site_stream(
        site_id: str,
        principal: Principal = Depends(require("site:read")),
        since: int = 0,
    ) -> StreamingResponse:
        runtime = site_runtime(site_id)

        async def generator():
            cursor = since
            while True:
                for event in runtime.events_since(cursor):
                    cursor = event["seq"]
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                await asyncio.sleep(0.4)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/sites/{site_id}/control")
    def site_control(
        site_id: str,
        req: SiteControlRequest,
        principal: Principal = Depends(require("site:control")),
    ) -> dict[str, Any]:
        runtime = site_runtime(site_id)
        if req.autopilot is not None:
            runtime.set_autopilot(req.autopilot)
            platform.audit.record(
                principal.username, "site.autopilot", actor_role=principal.role,
                site_id=site_id, detail={"enabled": req.autopilot},
            )
        return runtime.summary()

    @router.post("/sites/{site_id}/start")
    def site_start(
        site_id: str, principal: Principal = Depends(require("site:control"))
    ) -> dict[str, Any]:
        runtime = site_runtime(site_id)
        runtime.start()
        platform.audit.record(principal.username, "site.start", actor_role=principal.role,
                              site_id=site_id)
        return runtime.summary()

    @router.post("/sites/{site_id}/stop")
    def site_stop(
        site_id: str, principal: Principal = Depends(require("site:control"))
    ) -> dict[str, Any]:
        runtime = site_runtime(site_id)
        runtime.stop()
        platform.audit.record(principal.username, "site.stop", actor_role=principal.role,
                              site_id=site_id)
        return runtime.summary()

    @router.post("/sites/{site_id}/run-loop")
    def site_run_loop(
        site_id: str, principal: Principal = Depends(require("site:control"))
    ) -> dict[str, Any]:
        runtime = site_runtime(site_id)
        if not runtime.trigger_loop():
            raise HTTPException(409, "此站點的閉環正在執行中。")
        platform.audit.record(principal.username, "site.run_loop", actor_role=principal.role,
                              site_id=site_id)
        return {"started": True}

    @router.post("/sites/{site_id}/inject")
    def site_inject(
        site_id: str,
        req: InjectRequest,
        principal: Principal = Depends(require("site:control")),
    ) -> dict[str, Any]:
        if req.scenario_id not in SCENARIOS:
            raise HTTPException(404, f"未知情境 {req.scenario_id}")
        runtime = site_runtime(site_id)
        try:
            runtime.inject_scenario(req.scenario_id, principal.username)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        platform.audit.record(
            principal.username, "site.inject", actor_role=principal.role, site_id=site_id,
            detail={"scenario_id": req.scenario_id},
        )
        return runtime.summary()

    @router.get("/sites/{site_id}/approval")
    def site_approval(
        site_id: str, principal: Principal = Depends(require("site:read"))
    ) -> dict[str, Any]:
        runtime = site_runtime(site_id)
        return {"pending": runtime.pending.to_dict() if runtime.pending else None}

    @router.post("/sites/{site_id}/approval")
    def site_approve(
        site_id: str,
        req: ApprovalRequest,
        principal: Principal = Depends(require("approval:decide")),
    ) -> dict[str, Any]:
        runtime = site_runtime(site_id)
        ok = runtime.submit_approval(req.plan_id, req.approved, principal.username, req.reason)
        if not ok:
            raise HTTPException(409, "目前沒有等待中的核准，或方案代號不符。")
        platform.audit.record(
            principal.username, "approval.decide", actor_role=principal.role, site_id=site_id,
            target=req.plan_id,
            detail={"approved": req.approved, "reason": req.reason},
            outcome="approved" if req.approved else "rejected",
        )
        return {"ok": True}

    # ------------------------------------------------------------------ 告警
    @router.get("/alarms")
    def list_alarms(
        principal: Principal = Depends(require("alarm:read")),
        site_id: str | None = None,
        state: str | None = None,
        severity: str | None = None,
        machine_id: str | None = None,
        active_only: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        if state and state not in ALARM_STATES:
            raise HTTPException(400, f"未知告警狀態 {state}")
        return platform.alarms.list(
            site_id=site_id, state=state, severity=severity, machine_id=machine_id,
            active_only=active_only, limit=limit, offset=offset,
        )

    @router.get("/alarms/{alarm_id}")
    def get_alarm(
        alarm_id: str, principal: Principal = Depends(require("alarm:read"))
    ) -> dict[str, Any]:
        alarm = platform.alarms.get(alarm_id)
        if alarm is None:
            raise HTTPException(404, f"找不到告警 {alarm_id}")
        return alarm

    @router.post("/alarms/{alarm_id}/acknowledge")
    def ack_alarm(
        alarm_id: str,
        req: AlarmAckRequest,
        principal: Principal = Depends(require("alarm:ack")),
    ) -> dict[str, Any]:
        try:
            return platform.alarms.acknowledge(alarm_id, principal.username, principal.role, req.note)
        except KeyError:
            raise HTTPException(404, f"找不到告警 {alarm_id}") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/alarms/{alarm_id}/resolve")
    def resolve_alarm(
        alarm_id: str,
        req: AlarmResolveRequest,
        principal: Principal = Depends(require("alarm:resolve")),
    ) -> dict[str, Any]:
        try:
            return platform.alarms.resolve(alarm_id, principal.username, req.resolution, principal.role)
        except KeyError:
            raise HTTPException(404, f"找不到告警 {alarm_id}") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    # ------------------------------------------------------------------ 工單
    @router.get("/work-orders")
    def list_work_orders(
        principal: Principal = Depends(require("workorder:read")),
        site_id: str | None = None,
        state: str | None = None,
        assignee: str | None = None,
        machine_id: str | None = None,
        open_only: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        if state and state not in WORK_ORDER_STATES:
            raise HTTPException(400, f"未知工單狀態 {state}")
        return platform.work_orders.list(
            site_id=site_id, state=state, assignee=assignee, machine_id=machine_id,
            open_only=open_only, limit=limit, offset=offset,
        )

    @router.post("/work-orders")
    def create_work_order(
        req: WorkOrderCreateRequest,
        principal: Principal = Depends(require("workorder:create")),
    ) -> dict[str, Any]:
        if req.priority not in WORK_ORDER_PRIORITIES:
            raise HTTPException(400, f"未知優先度 {req.priority}")
        if req.kind not in WORK_ORDER_KINDS:
            raise HTTPException(400, f"未知工單類型 {req.kind}")
        if platform.fleet.site_record(req.site_id) is None:
            raise HTTPException(404, f"未知站點 {req.site_id}")
        return platform.work_orders.create(
            req.site_id, title=req.title, machine_id=req.machine_id, detail=req.detail,
            kind=req.kind, priority=req.priority, assignee=req.assignee,
            estimated_min=req.estimated_min, alarm_id=req.alarm_id,
            created_by=principal.username, actor_role=principal.role,
        )

    @router.patch("/work-orders/{work_order_id}")
    def update_work_order(
        work_order_id: str,
        req: WorkOrderUpdateRequest,
        principal: Principal = Depends(require("workorder:update")),
    ) -> dict[str, Any]:
        try:
            return platform.work_orders.update(
                work_order_id, principal.username, actor_role=principal.role,
                state=req.state, assignee=req.assignee, priority=req.priority,
                completion_note=req.completion_note,
            )
        except KeyError:
            raise HTTPException(404, f"找不到工單 {work_order_id}") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ------------------------------------------------------------------ 交接
    @router.get("/handovers")
    def list_handovers(
        principal: Principal = Depends(require("site:read")),
        site_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        return {"handovers": platform.handovers.list(site_id, limit)}

    @router.post("/handovers")
    def create_handover(
        req: HandoverRequest,
        principal: Principal = Depends(require("handover:write")),
    ) -> dict[str, Any]:
        if platform.fleet.site_record(req.site_id) is None:
            raise HTTPException(404, f"未知站點 {req.site_id}")
        return platform.handovers.create(
            req.site_id, shift=req.shift, author=principal.username, summary=req.summary,
            actor_role=principal.role, open_items=req.open_items,
        )

    # ------------------------------------------------------------------ 分析
    @router.get("/analytics/summary")
    def analytics_summary(
        principal: Principal = Depends(require("analytics:read")),
        site_id: str | None = None,
        days: int = Query(default=7, ge=1, le=90),
    ) -> dict[str, Any]:
        return platform.analytics.summary(site_id, days)

    @router.get("/analytics/kpi-trend")
    def analytics_kpi_trend(
        principal: Principal = Depends(require("analytics:read")),
        site_id: str = Query(...),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        return {"site_id": site_id, "samples": platform.analytics.kpi_trend(site_id, limit)}

    # ------------------------------------------------------------------ 稽核
    @router.get("/audit")
    def list_audit(
        principal: Principal = Depends(require("audit:read")),
        site_id: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        since: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return platform.audit.list(
            site_id=site_id, actor=actor, action=action, since=since,
            limit=limit, offset=offset,
        )

    # ------------------------------------------------------------------ 管理
    @router.get("/admin/users")
    def list_users(
        principal: Principal = Depends(require("user:manage")),
    ) -> dict[str, Any]:
        return {"users": platform.auth.list_users(), "roles": list(ROLES),
                "role_labels": ROLE_LABELS}

    @router.post("/admin/users")
    def create_user(
        req: UserCreateRequest,
        principal: Principal = Depends(require("user:manage")),
    ) -> dict[str, Any]:
        try:
            user = platform.auth.create_user(req.username, req.password, req.display_name, req.role)
        except AuthError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        platform.audit.record(principal.username, "user.create", actor_role=principal.role,
                              target=req.username, detail={"role": req.role})
        return user

    @router.patch("/admin/users/{username}")
    def update_user(
        username: str,
        req: UserUpdateRequest,
        principal: Principal = Depends(require("user:manage")),
    ) -> dict[str, Any]:
        if platform.auth.get_user(username) is None:
            raise HTTPException(404, f"找不到帳號 {username}")
        # 不允許把自己降級或停用：避免管理員把自己鎖在系統外面。
        if username == principal.username and (req.role not in (None, principal.role) or req.enabled is False):
            raise HTTPException(400, "不能變更或停用自己的帳號權限，請由另一位管理員操作。")
        try:
            if req.role is not None:
                platform.auth.set_role(username, req.role)
            if req.enabled is not None:
                platform.auth.set_enabled(username, req.enabled)
            if req.password is not None:
                platform.auth.set_password(username, req.password)
        except AuthError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        platform.audit.record(
            principal.username, "user.update", actor_role=principal.role, target=username,
            detail={"role": req.role, "enabled": req.enabled,
                    "password_changed": req.password is not None},
        )
        return platform.auth.get_user(username) or {}

    @router.delete("/admin/users/{username}")
    def delete_user(
        username: str,
        principal: Principal = Depends(require("user:manage")),
    ) -> dict[str, Any]:
        if username == principal.username:
            raise HTTPException(400, "不能刪除自己的帳號。")
        if platform.auth.get_user(username) is None:
            raise HTTPException(404, f"找不到帳號 {username}")
        platform.auth.delete_user(username)
        platform.audit.record(principal.username, "user.delete", actor_role=principal.role,
                              target=username)
        return {"ok": True}

    @router.post("/admin/sites")
    def create_site(
        req: SiteCreateRequest,
        principal: Principal = Depends(require("site:manage")),
    ) -> dict[str, Any]:
        config = dict(req.adapter_config)
        config["autopilot"] = req.autopilot
        record = platform.fleet.register_site(
            req.site_id, req.name, region=req.region, adapter_kind=req.adapter_kind,
            adapter_config=config, autopilot=req.autopilot,
        )
        platform.audit.record(principal.username, "site.create", actor_role=principal.role,
                              site_id=req.site_id, detail={"adapter_kind": req.adapter_kind})
        return record

    @router.delete("/admin/sites/{site_id}")
    def delete_site(
        site_id: str,
        principal: Principal = Depends(require("site:manage")),
    ) -> dict[str, Any]:
        if platform.fleet.site_record(site_id) is None:
            raise HTTPException(404, f"未知站點 {site_id}")
        platform.fleet.remove_site(site_id)
        platform.audit.record(principal.username, "site.delete", actor_role=principal.role,
                              site_id=site_id)
        return {"ok": True}

    # ------------------------------------------------------------------ 中繼資料
    @router.get("/meta")
    def meta(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
        """前端需要的下拉選單與能力清單。"""
        return {
            "adapter_kinds": describe_adapter_kinds(),
            "scenarios": [
                {"scenario_id": s.scenario_id, "title": s.title} for s in SCENARIOS.values()
            ],
            "alarm_states": list(ALARM_STATES),
            "work_order_states": list(WORK_ORDER_STATES),
            "work_order_priorities": list(WORK_ORDER_PRIORITIES),
            "work_order_kinds": list(WORK_ORDER_KINDS),
            "roles": [{"role": r, "label": ROLE_LABELS[r]} for r in ROLES],
            "user": principal.to_dict(),
        }

    return router


__all__ = ["build_platform_router"]
