"""中央管理平台測試：持久化、權限、告警生命週期、工單與 Fleet。

這些測試守住「正式產品」與「Demo」的差別：
資料要留得住、權限要擋得住、狀態機不能被繞過、稽核要記得住是誰做的。
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from factory_guardian.api.server import create_app
from factory_guardian.platform.auth import (
    PERMISSIONS,
    AuthError,
    AuthService,
    ensure_default_admin,
    hash_password,
    verify_password,
)
from factory_guardian.platform.bootstrap import build_platform
from factory_guardian.platform.fleet import FleetManager
from factory_guardian.platform.services import (
    AlarmService,
    AnalyticsService,
    AuditService,
    HandoverService,
    WorkOrderService,
    describe_trigger,
)
from factory_guardian.platform.store import Store


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "platform.db")


@pytest.fixture
def auth(store) -> AuthService:
    return AuthService(store)


@pytest.fixture
def site(store) -> str:
    with store.write() as conn:
        conn.execute(
            """
            INSERT INTO sites (site_id, name, region, created_at, updated_at)
            VALUES ('S1', '測試廠', '中區', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """
        )
    return "S1"


@pytest.fixture
def platform(tmp_path):
    """不自動啟動站點的平台實例：測試要能決定何時推進時間。"""
    p = build_platform(tmp_path / "p.db", autostart=False)
    yield p
    p.shutdown()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FG_PLATFORM_DB", str(tmp_path / "api.db"))
    app = create_app(platform_autostart=False)
    with TestClient(app) as c:
        yield c


def login(client: TestClient, username: str, password: str) -> TestClient:
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return client


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------
def test_store_creates_schema_and_is_idempotent(tmp_path):
    from factory_guardian.platform.store import SCHEMA_VERSION

    path = tmp_path / "db.sqlite"
    first = Store(path)
    assert first.scalar("SELECT version FROM schema_meta") == SCHEMA_VERSION
    # 重新開啟同一個檔案不應該重跑 migration 或炸掉
    second = Store(path)
    assert second.scalar("SELECT version FROM schema_meta") == SCHEMA_VERSION
    tables = {r["name"] for r in second.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sites", "alarms", "work_orders", "users", "audit_events"} <= tables


def test_store_migrates_existing_database_forward(tmp_path):
    """既有資料庫要能升級到新版 schema，而且資料不能掉。"""
    from factory_guardian.platform import store as store_mod

    path = tmp_path / "old.db"
    # 先建一個「只跑到第 1 版」的資料庫
    original = store_mod._MIGRATIONS
    try:
        store_mod._MIGRATIONS = original[:1]
        old = Store(path)
        assert old.scalar("SELECT version FROM schema_meta") == 1
        with old.write() as conn:
            conn.execute(
                "INSERT INTO sites (site_id, name, created_at, updated_at) "
                "VALUES ('S1', '舊廠', '2026-01-01', '2026-01-01')"
            )
        old.close()
    finally:
        store_mod._MIGRATIONS = original

    upgraded = Store(path)
    assert upgraded.scalar("SELECT version FROM schema_meta") == len(original)
    assert upgraded.query_one("SELECT name FROM sites WHERE site_id='S1'")["name"] == "舊廠"
    cols = {r["name"] for r in upgraded.query("PRAGMA table_info(work_orders)")}
    assert "source_key" in cols, "v2 遷移應該補上 source_key 欄位"


def test_store_write_rolls_back_on_error(store, site):
    with pytest.raises(RuntimeError):
        with store.write() as conn:
            conn.execute(
                "INSERT INTO work_orders (work_order_id, site_id, title, created_at, updated_at) "
                "VALUES ('WO-X', 'S1', 'x', '2026-01-01', '2026-01-01')"
            )
            raise RuntimeError("boom")
    assert store.scalar("SELECT COUNT(*) FROM work_orders") == 0


def test_data_survives_reopen(tmp_path, ):
    """正式平台的核心差異：重啟之後資料還在。"""
    path = tmp_path / "persist.db"
    store = Store(path)
    with store.write() as conn:
        conn.execute(
            "INSERT INTO sites (site_id, name, created_at, updated_at) "
            "VALUES ('S9', '持久廠', '2026-01-01', '2026-01-01')"
        )
    AlarmService(store).raise_alarm("S9", title="測試告警", severity="warning", machine_id="M-A")
    store.close()

    reopened = Store(path)
    assert reopened.scalar("SELECT COUNT(*) FROM alarms") == 1
    assert reopened.query_one("SELECT name FROM sites WHERE site_id='S9'")["name"] == "持久廠"


# --------------------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------------------
def test_password_hash_is_salted_and_verifiable():
    h1, s1 = hash_password("correct horse battery")
    h2, s2 = hash_password("correct horse battery")
    assert s1 != s2 and h1 != h2, "相同密碼要有不同 salt，否則能用彩虹表反推"
    assert verify_password("correct horse battery", h1, s1)
    assert not verify_password("wrong password", h1, s1)


def test_login_rejects_bad_credentials_and_issues_token(auth):
    auth.create_user("op1", "operator123", "操作員", "operator")
    with pytest.raises(AuthError):
        auth.login("op1", "wrongpassword")
    with pytest.raises(AuthError):
        auth.login("nosuchuser", "operator123")
    token, principal = auth.login("op1", "operator123")
    assert principal.role == "operator"
    assert auth.resolve(token).username == "op1"


def test_logout_and_disable_revoke_tokens(auth):
    auth.create_user("op1", "operator123", "操作員", "operator")
    token, _ = auth.login("op1", "operator123")
    auth.logout(token)
    assert auth.resolve(token) is None

    token2, _ = auth.login("op1", "operator123")
    auth.set_enabled("op1", False)
    assert auth.resolve(token2) is None, "停用帳號必須立即讓既有 token 失效"


def test_password_change_revokes_sessions(auth):
    auth.create_user("op1", "operator123", "操作員", "operator")
    token, _ = auth.login("op1", "operator123")
    auth.set_password("op1", "brandnewpass")
    assert auth.resolve(token) is None


def test_role_permissions_are_strictly_increasing():
    """角色權限必須是包含關係，避免出現「工程師不能做操作員能做的事」。"""
    viewer = set(PERMISSIONS["viewer"])
    operator = set(PERMISSIONS["operator"])
    engineer = set(PERMISSIONS["engineer"])
    admin = set(PERMISSIONS["admin"])
    assert viewer < operator < engineer < admin
    # 關鍵邊界
    assert "approval:decide" not in operator
    assert "approval:decide" in engineer
    assert "user:manage" not in engineer
    assert "user:manage" in admin


def test_ensure_default_admin_only_once(auth):
    assert ensure_default_admin(auth, "admin", "admin12345") is True
    assert ensure_default_admin(auth, "admin", "admin12345") is False


# --------------------------------------------------------------------------------------
# 告警
# --------------------------------------------------------------------------------------
def test_alarm_dedup_and_severity_escalation(store, site):
    svc = AlarmService(store)
    first = svc.raise_alarm(site, title="振動異常", severity="warning",
                            machine_id="M-A", dedup_key="M-A:vib")
    second = svc.raise_alarm(site, title="振動異常", severity="critical",
                             machine_id="M-A", dedup_key="M-A:vib")
    assert second["alarm_id"] == first["alarm_id"], "同一個持續異常不該產生第二筆告警"
    assert second["occurrence_count"] == 2
    assert second["severity"] == "critical", "嚴重度升高時要升級既有告警"
    assert store.scalar("SELECT COUNT(*) FROM alarms") == 1


def test_alarm_lifecycle_state_machine(store, site):
    svc = AlarmService(store)
    alarm = svc.raise_alarm(site, title="溫度過高", severity="warning", machine_id="M-B")
    aid = alarm["alarm_id"]
    assert alarm["state"] == "open"

    acked = svc.acknowledge(aid, "operator1", "operator")
    assert acked["state"] == "acknowledged" and acked["acked_by"] == "operator1"

    # 已確認的告警不能再確認一次
    with pytest.raises(ValueError):
        svc.acknowledge(aid, "operator2", "operator")

    resolved = svc.resolve(aid, "operator1", "更換冷卻液", "operator")
    assert resolved["state"] == "resolved" and resolved["resolution"] == "更換冷卻液"

    # 已結案的告警不能重複結案
    with pytest.raises(ValueError):
        svc.resolve(aid, "operator1", "again", "operator")


def test_resolved_alarm_frees_dedup_key(store, site):
    """結案後同一個 dedup key 要能再次告警——否則機器再壞就沒人知道。"""
    svc = AlarmService(store)
    first = svc.raise_alarm(site, title="振動異常", severity="warning", dedup_key="M-A:vib")
    svc.resolve(first["alarm_id"], "op", "已修復")
    second = svc.raise_alarm(site, title="振動異常", severity="warning", dedup_key="M-A:vib")
    assert second["alarm_id"] != first["alarm_id"]


def test_alarm_marks_stale_after_threshold(store, site):
    svc = AlarmService(store)
    alarm = svc.raise_alarm(site, title="久未確認", severity="warning")
    assert alarm["stale"] is False
    # 把發生時間往回推 30 分鐘
    with store.write() as conn:
        conn.execute(
            "UPDATE alarms SET raised_at = datetime('now','-30 minutes') WHERE alarm_id = ?",
            (alarm["alarm_id"],),
        )
    assert svc.get(alarm["alarm_id"])["stale"] is True


def test_alarm_filters(store, site):
    svc = AlarmService(store)
    svc.raise_alarm(site, title="A", severity="critical", machine_id="M-A", dedup_key="a")
    svc.raise_alarm(site, title="B", severity="warning", machine_id="M-B", dedup_key="b")
    done = svc.raise_alarm(site, title="C", severity="info", machine_id="M-C", dedup_key="c")
    svc.resolve(done["alarm_id"], "op", "ok")

    assert svc.list(site_id=site, active_only=True)["total"] == 2
    assert svc.list(site_id=site, severity="critical")["total"] == 1
    assert svc.list(site_id=site, machine_id="M-B")["total"] == 1
    assert svc.list(site_id=site, state="resolved")["total"] == 1


def test_describe_trigger_translates_machine_strings():
    assert describe_trigger("threshold:vibration=WARNING") == "振動超出警告門檻"
    assert describe_trigger("threshold:temperature=CRITICAL") == "溫度超出危險門檻"
    assert "持續惡化" in describe_trigger("trend:+0.120/tick")
    # 無法辨識的格式要原樣保留，不能吃掉資訊
    assert describe_trigger("weird-format") == "weird-format"


# --------------------------------------------------------------------------------------
# 工單
# --------------------------------------------------------------------------------------
def test_work_order_lifecycle_records_timestamps(store, site):
    svc = WorkOrderService(store)
    wo = svc.create(site, title="更換軸承", machine_id="M-A", priority="high",
                    created_by="operator1")
    wid = wo["work_order_id"]
    assert wo["state"] == "open" and wo["started_at"] is None

    started = svc.update(wid, "tech1", state="in_progress", assignee="tech1")
    assert started["state"] == "in_progress" and started["started_at"] is not None

    done = svc.update(wid, "tech1", state="completed", completion_note="已更換")
    assert done["state"] == "completed" and done["completed_at"] is not None
    assert done["duration_min"] is not None, "完成的工單要算得出實際工時"


def test_work_order_from_loop_maps_agent_priority(store, site):
    """MaintenanceAgent 產出的是 P1/P2/P3；對應錯會讓所有工單都變成一般優先度。"""
    from factory_guardian.domain import WorkOrder
    from factory_guardian.orchestrator import LoopResult

    svc = WorkOrderService(store)
    for agent_priority, expected in (("P1", "urgent"), ("P2", "high"), ("P3", "normal")):
        wo = WorkOrder(
            work_order_id=f"WO-001-{agent_priority}", machine_id="M-A",
            problem="主軸軸承劣化", priority=agent_priority, required_skill="機械",
            suggested_parts=(), estimated_repair_min=40.0, sop_refs=(),
            evidence=[], safety_precautions=(),
        )
        result = LoopResult(triggered=True)
        result.work_order = wo
        created = svc.create_from_loop(site, result)
        assert created["priority"] == expected, f"{agent_priority} 應對應 {expected}"


def test_work_order_from_loop_is_deduplicated(store, site):
    """閉環重試會重複呼叫；同一張工單不能建成兩筆。"""
    from factory_guardian.domain import WorkOrder
    from factory_guardian.orchestrator import LoopResult

    svc = WorkOrderService(store)

    def make(wo_id: str, problem: str) -> LoopResult:
        result = LoopResult(triggered=True)
        result.work_order = WorkOrder(
            work_order_id=wo_id, machine_id="M-A", problem=problem, priority="P2",
            required_skill="機械", suggested_parts=(), estimated_repair_min=40.0,
            sop_refs=(), evidence=[], safety_precautions=(),
        )
        return result

    first = svc.create_from_loop(site, make("WO-9-1", "軸承劣化"))
    again = svc.create_from_loop(site, make("WO-9-1", "軸承劣化"))
    assert again["work_order_id"] == first["work_order_id"]
    assert store.scalar("SELECT COUNT(*) FROM work_orders") == 1

    # WO-9-10 不該被 WO-9-1 的比對吃掉（舊版用 LIKE 就會誤判）
    other = svc.create_from_loop(site, make("WO-9-10", "冷卻異常"))
    assert other["work_order_id"] != first["work_order_id"]
    assert store.scalar("SELECT COUNT(*) FROM work_orders") == 2


def test_concurrent_loops_cannot_both_claim_site(store):
    """背景迴圈與手動觸發同時搶閉環時，只能有一個跑起來。"""
    fleet = FleetManager(store)
    fleet.register_site("A1", "甲廠", adapter_config={"seed": 7})
    runtime = fleet.runtime("A1")

    results: list[bool] = []
    barrier = threading.Barrier(8)

    def claim():
        barrier.wait()
        results.append(runtime._claim())

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 1, "同一時間只能有一個執行緒取得閉環權"
    runtime._release()
    assert runtime._claim() is True, "釋放後應該可以再次取得"


def test_work_order_rejects_invalid_values(store, site):
    svc = WorkOrderService(store)
    with pytest.raises(ValueError):
        svc.create(site, title="x", priority="超級急")
    with pytest.raises(ValueError):
        svc.create(site, title="x", kind="不存在的類型")
    wo = svc.create(site, title="ok")
    with pytest.raises(ValueError):
        svc.update(wo["work_order_id"], "op", state="幻想狀態")


# --------------------------------------------------------------------------------------
# 稽核與分析
# --------------------------------------------------------------------------------------
def test_audit_records_actor_identity(store, site):
    audit = AuditService(store)
    audit.record("engineer1", "approval.decide", actor_role="engineer", site_id=site,
                 target="PLAN-D", outcome="approved", detail={"reason": "確認安全"})
    records = audit.list()["records"]
    assert len(records) == 1
    row = records[0]
    assert row["actor"] == "engineer1" and row["actor_role"] == "engineer"
    assert row["outcome"] == "approved" and row["detail"]["reason"] == "確認安全"


def test_analytics_computes_mtta_and_mttr(store, site):
    alarms = AlarmService(store)
    alarm = alarms.raise_alarm(site, title="X", severity="warning")
    aid = alarm["alarm_id"]
    # 人工造出 10 分鐘確認、30 分鐘結案的時間差
    with store.write() as conn:
        conn.execute(
            """
            UPDATE alarms SET raised_at = datetime('now','-30 minutes'),
                              acked_at  = datetime('now','-20 minutes'),
                              resolved_at = datetime('now'),
                              state = 'resolved'
            WHERE alarm_id = ?
            """,
            (aid,),
        )
    summary = AnalyticsService(store).summary(site, days=7)
    assert summary["alarms"]["total"] == 1
    assert 9 <= summary["alarms"]["mtta_min"] <= 11
    assert 29 <= summary["alarms"]["mttr_min"] <= 31


def test_handover_auto_collects_open_items(store, site):
    AlarmService(store).raise_alarm(site, title="未結告警", severity="warning", dedup_key="k1")
    WorkOrderService(store).create(site, title="未結工單")
    handover = HandoverService(store).create(site, shift="早班", author="op1", summary="交班")
    assert len(handover["open_items"]) == 2
    assert any("告警" in i for i in handover["open_items"])
    assert any("工單" in i for i in handover["open_items"])


# --------------------------------------------------------------------------------------
# Fleet
# --------------------------------------------------------------------------------------
def test_fleet_registers_and_isolates_sites(store):
    fleet = FleetManager(store)
    fleet.register_site("A1", "甲廠", region="北區", adapter_config={"seed": 111})
    fleet.register_site("B1", "乙廠", region="南區", adapter_config={"seed": 222})
    assert {r["site_id"] for r in fleet.site_records()} == {"A1", "B1"}

    a, b = fleet.runtime("A1"), fleet.runtime("B1")
    assert a.twin is not b.twin, "每個站點必須有自己的孿生體，不能共用狀態"
    assert a.adapter.seed != b.adapter.seed, "不同站點要用不同 seed，否則劇本一模一樣"

    a.adapter.start()
    a._cycle()
    assert a.twin.tick == 1 and b.twin.tick == 0, "推進一個站點不該影響另一個站點"


def test_fleet_overview_aggregates(store, site):
    fleet = FleetManager(store)
    fleet.register_site("A1", "甲廠", adapter_config={"seed": 111})
    AlarmService(store).raise_alarm("A1", title="X", severity="critical", dedup_key="x")
    overview = fleet.overview()
    assert overview["totals"]["site_count"] >= 1
    a1 = next(s for s in overview["sites"] if s["site_id"] == "A1")
    assert a1["alarms"]["critical"] == 1


def test_unknown_site_raises(store):
    with pytest.raises(KeyError):
        FleetManager(store).runtime("NOPE")


def test_simulated_adapter_is_flagged(store):
    fleet = FleetManager(store)
    fleet.register_site("A1", "甲廠", adapter_config={"seed": 5})
    runtime = fleet.runtime("A1")
    assert runtime.adapter.simulated is True, "模擬站點必須標示出來，正式介面才誠實"
    assert runtime.adapter.descriptor()["kind"] == "simulated"


def test_unimplemented_adapter_does_not_crash_fleet(store):
    """尚未實作的真實 adapter 不能讓整個平台起不來。"""
    fleet = FleetManager(store)
    fleet.register_site("OPC", "真實廠", adapter_kind="opcua")
    with pytest.raises(NotImplementedError):
        fleet.runtime("OPC")
    fleet.start_all()  # 應該安靜跳過，不拋例外
    overview = fleet.overview()
    assert any(s["site_id"] == "OPC" and s["status"] == "stopped" for s in overview["sites"])


def test_site_loop_produces_alarm_and_work_order(store):
    """端到端：注入故障 → 偵測 → 告警落庫 → 閉環完成 → 工單落庫。

    conftest 設定 FG_REQUIRE_APPROVAL=0，所以這裡走的是自動核准路徑；
    需要人工核准的路徑由 test_site_loop_waits_for_human_approval 覆蓋。
    """
    fleet = FleetManager(store)
    fleet.register_site("A1", "甲廠",
                        adapter_config={"seed": 20260809, "scenario_id": "bearing-degradation",
                                        "poll_interval_s": 0.02})
    runtime = fleet.runtime("A1")
    runtime.start()
    try:
        deadline = time.time() + 90
        while time.time() < deadline and runtime.last_loop is None:
            time.sleep(0.1)
        assert runtime.last_loop is not None, "閉環應該要跑完"
        assert store.scalar("SELECT COUNT(*) FROM alarms WHERE site_id='A1'") >= 1
    finally:
        runtime.stop()

    time.sleep(0.5)
    assert store.scalar("SELECT COUNT(*) FROM work_orders WHERE site_id='A1'") >= 1
    wo = store.query_one("SELECT created_by FROM work_orders WHERE site_id='A1' LIMIT 1")
    assert wo["created_by"] == "agent-loop"


def test_site_loop_waits_for_human_approval(tmp_path, monkeypatch):
    """開啟核准要求時，閉環必須停在人工核准，不能自己執行下去。"""
    monkeypatch.setenv("FG_REQUIRE_APPROVAL", "1")
    from factory_guardian.config import get_settings

    settings = get_settings(refresh=True)
    assert settings.require_approval is True
    store = Store(tmp_path / "approval.db")
    fleet = FleetManager(store, settings=settings)
    fleet.register_site("A1", "甲廠",
                        adapter_config={"seed": 20260809, "scenario_id": "bearing-degradation",
                                        "poll_interval_s": 0.02})
    runtime = fleet.runtime("A1")
    runtime.start()
    try:
        deadline = time.time() + 90
        while time.time() < deadline and runtime.pending is None:
            time.sleep(0.1)
        assert runtime.pending is not None, "高風險方案必須停下來等人工核准"
        assert runtime.last_loop is None, "還沒核准就不該有執行結果"

        plan_id = runtime.pending.plan.plan_id
        assert runtime.submit_approval(plan_id, True, "engineer1", "測試核准")

        deadline = time.time() + 90
        while time.time() < deadline and runtime.last_loop is None:
            time.sleep(0.1)
        assert runtime.last_loop is not None, "核准後閉環應該繼續執行完"
    finally:
        runtime.stop()
        get_settings(refresh=True)


# --------------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------------
def test_api_requires_authentication(client):
    for path in ("/api/v1/fleet/overview", "/api/v1/alarms", "/api/v1/work-orders",
                 "/api/v1/audit", "/api/v1/admin/users"):
        assert client.get(path).status_code == 401, f"{path} 不該讓未登入者讀取"


def test_api_login_and_me(client):
    login(client, "admin", "admin12345")
    me = client.get("/api/v1/auth/me").json()["user"]
    assert me["username"] == "admin" and me["role"] == "admin"


def test_api_viewer_is_read_only(client):
    login(client, "viewer", "viewer12345")
    assert client.get("/api/v1/alarms").status_code == 200
    assert client.get("/api/v1/fleet/overview").status_code == 200
    # 寫入一律擋掉
    assert client.post("/api/v1/work-orders",
                       json={"site_id": "TC-01", "title": "x"}).status_code == 403
    assert client.get("/api/v1/admin/users").status_code == 403
    assert client.post("/api/v1/sites/TC-01/approval",
                       json={"plan_id": "X", "approved": True}).status_code == 403


def test_api_operator_cannot_approve_but_engineer_can(client):
    login(client, "operator", "operator123")
    assert client.post("/api/v1/sites/TC-01/approval",
                       json={"plan_id": "X", "approved": True}).status_code == 403
    client.post("/api/v1/auth/logout")

    login(client, "engineer", "engineer123")
    # 沒有等待中的核准 → 409（而不是 403），代表權限有過、只是沒東西可核准
    assert client.post("/api/v1/sites/TC-01/approval",
                       json={"plan_id": "X", "approved": True}).status_code == 409


def test_api_work_order_flow_records_real_actor(client):
    login(client, "operator", "operator123")
    created = client.post("/api/v1/work-orders", json={
        "site_id": "TC-01", "title": "更換濾網", "machine_id": "M-B", "priority": "high",
    })
    assert created.status_code == 200
    wo = created.json()
    assert wo["created_by"] == "operator", "工單要記真實操作者，不是 'system'"

    updated = client.patch(f"/api/v1/work-orders/{wo['work_order_id']}",
                           json={"state": "in_progress", "assignee": "operator"})
    assert updated.json()["state"] == "in_progress"

    audit = client.get("/api/v1/audit").json()["records"]
    assert any(r["action"] == "workorder.create" and r["actor"] == "operator" for r in audit)


def test_api_alarm_ack_and_resolve(client, tmp_path):
    login(client, "admin", "admin12345")
    platform = client.app.state.platform
    alarm = platform.alarms.raise_alarm("TC-01", title="測試告警", severity="warning",
                                        machine_id="M-A", dedup_key="test:1")
    aid = alarm["alarm_id"]

    acked = client.post(f"/api/v1/alarms/{aid}/acknowledge", json={"note": "已知悉"})
    assert acked.status_code == 200 and acked.json()["acked_by"] == "admin"

    # 重複確認要回 409，不能靜默成功
    assert client.post(f"/api/v1/alarms/{aid}/acknowledge", json={}).status_code == 409

    resolved = client.post(f"/api/v1/alarms/{aid}/resolve", json={"resolution": "已排除"})
    assert resolved.status_code == 200 and resolved.json()["state"] == "resolved"


def test_api_resolve_requires_resolution_text(client):
    login(client, "admin", "admin12345")
    platform = client.app.state.platform
    alarm = platform.alarms.raise_alarm("TC-01", title="X", severity="warning", dedup_key="t2")
    res = client.post(f"/api/v1/alarms/{alarm['alarm_id']}/resolve", json={"resolution": ""})
    assert res.status_code == 422, "結案必須填寫處理方式"


def test_api_admin_cannot_lock_itself_out(client):
    login(client, "admin", "admin12345")
    assert client.patch("/api/v1/admin/users/admin", json={"role": "viewer"}).status_code == 400
    assert client.patch("/api/v1/admin/users/admin", json={"enabled": False}).status_code == 400
    assert client.delete("/api/v1/admin/users/admin").status_code == 400


def test_api_user_management(client):
    login(client, "admin", "admin12345")
    created = client.post("/api/v1/admin/users", json={
        "username": "tech9", "password": "tech123456", "display_name": "技師九", "role": "operator",
    })
    assert created.status_code == 200

    # 重複帳號要擋
    dup = client.post("/api/v1/admin/users", json={
        "username": "tech9", "password": "tech123456", "display_name": "x", "role": "operator",
    })
    assert dup.status_code == 409

    # 短密碼要擋
    weak = client.post("/api/v1/admin/users", json={
        "username": "tech8", "password": "short", "display_name": "x", "role": "operator",
    })
    assert weak.status_code == 422

    assert client.delete("/api/v1/admin/users/tech9").status_code == 200


def test_api_logout_invalidates_session(client):
    login(client, "admin", "admin12345")
    assert client.get("/api/v1/auth/me").status_code == 200
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_api_meta_exposes_options(client):
    login(client, "admin", "admin12345")
    meta = client.get("/api/v1/meta").json()
    assert meta["work_order_priorities"] and meta["scenarios"]
    assert any(a["kind"] == "simulated" and a["implemented"] for a in meta["adapter_kinds"])
    assert any(not a["implemented"] for a in meta["adapter_kinds"]), \
        "未實作的 adapter 要誠實標示，不能假裝可用"


def test_console_page_and_assets_served(client):
    assert client.get("/console").status_code == 200
    assert "console.js" in client.get("/console").text
    assert client.get("/static/console.js").status_code == 200
    assert client.get("/static/console.css").status_code == 200


def test_demo_pages_still_work(client):
    """平台層不能把原本的 Demo 頁面弄壞。"""
    for path in ("/", "/benchmark", "/system", "/factory", "/api/health", "/api/state"):
        assert client.get(path).status_code == 200, f"{path} 壞了"
