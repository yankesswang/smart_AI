"""API 與 Dashboard 後端。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from factory_guardian.api.server import create_app


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_health_and_config(client):
    body = client.get("/api/health").json()
    assert body["ok"] and body["disclaimer"]
    config = client.get("/api/config").json()
    assert config["settings"]["llm_mode"]
    assert "LLM" in config["llm_role"]


def test_dashboard_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "FACTORY GUARDIAN AI" in response.text
    assert "SYNTHETIC DEMO DATA" in response.text


def test_scenarios_and_topology(client):
    scenarios = client.get("/api/scenarios").json()
    assert len(scenarios["scenarios"]) >= 4
    assert len(scenarios["faults"]) == 3

    topology = client.get("/api/topology").json()
    assert {m["machine_id"] for m in topology["machines"]} == {"M-A", "M-B", "M-C"}
    assert topology["graph"]["nodes"] and topology["graph"]["edges"]


def test_policy_endpoint_exposes_the_governance_table(client):
    policy = client.get("/api/policy").json()
    actions = {a["action"]: a for a in policy["action_policy"]}
    assert actions["safety_override"]["forbidden"]
    assert actions["stop_machine"]["requires_approval"]
    assert not actions["raise_alert"]["requires_approval"]
    assert len(policy["rules"]) >= 8


def test_knowledge_endpoints(client):
    knowledge = client.get("/api/knowledge").json()
    assert knowledge["stats"]["manuals"] >= 5
    assert knowledge["stats"]["history_cases"] >= 20

    hits = client.get("/api/knowledge/search", params={"q": "振動上升 軸承", "top_k": 3}).json()
    assert hits["hits"]
    assert any("bearing_degradation" in h["fault_ids"] for h in hits["hits"])


def test_state_has_no_ground_truth(client):
    state = client.get("/api/state").json()
    assert "fault_progress" not in str(state["snapshot"])
    assert set(state["snapshot"]["machines"]) == {"M-A", "M-B", "M-C"}
    assert state["kpi"]["production_pct"] > 0


def test_tick_advances_simulation(client):
    before = client.get("/api/state").json()["snapshot"]["tick"]
    after = client.post("/api/session/tick", json={"count": 5}).json()["snapshot"]["tick"]
    assert after == before + 5


def test_machine_logs_are_generated_without_ground_truth(client):
    client.post("/api/session/reset", json={"scenario_id": "bearing-degradation"})
    state = client.post("/api/session/tick", json={"count": 4}).json()
    logs = client.get("/api/logs", params={"machine_id": "M-A", "since_tick": 1}).json()

    assert logs["schema"] == "synthetic-machine-telemetry/v1"
    assert logs["logs"]
    assert {row["machine_id"] for row in logs["logs"]} == {"M-A"}
    assert logs["logs"][-1]["tick"] == state["snapshot"]["tick"]
    assert "fault_progress" not in str(logs)
    assert {"timestamp", "health", "readings", "worst_band", "message"} <= set(logs["logs"][-1])


def test_temporal_forecast_uses_observable_history_and_reports_runtime(client):
    client.post("/api/session/reset", json={"scenario_id": "bearing-degradation"})
    client.post("/api/session/tick", json={"count": 12})
    models = client.get("/api/prediction/models").json()
    response = client.post("/api/prediction/forecast", json={
        "machine_id": "M-A", "target": "health", "horizon": 8,
        "context_window": 60, "model": "ridge",
    })
    body = response.json()

    assert response.status_code == 200
    assert models["default"] == "auto"
    assert {model["id"] for model in models["models"]} == {"auto", "tabfm", "ridge"}
    assert body["runtime"] == "ridge-fallback"
    assert body["fallback"] is False
    assert body["context_rows"] >= 8
    assert len(body["forecast"]) == 8
    assert {"tick", "value", "lower", "upper", "risk"} <= set(body["forecast"][0])
    assert "fault_progress" not in str(body)


def test_temporal_forecast_rejects_unknown_machine_and_short_context(client):
    unknown = client.post("/api/prediction/forecast", json={"machine_id": "M-X"})
    short = client.post("/api/prediction/forecast", json={"machine_id": "M-A"})
    assert unknown.status_code == 404
    assert short.status_code == 422
    assert "8 個時間點" in short.json()["detail"]


def test_explicit_tabfm_does_not_silently_masquerade_as_fallback(client):
    client.post("/api/session/tick", json={"count": 10})
    models = client.get("/api/prediction/models").json()["models"]
    tabfm = next(model for model in models if model["id"] == "tabfm")
    if not tabfm["available"]:
        response = client.post("/api/prediction/forecast", json={
            "machine_id": "M-A", "model": "tabfm",
        })
        assert response.status_code == 422
        assert "未安裝" in response.json()["detail"]


def test_inject_then_signals_move(client):
    client.post("/api/session/reset", json={"scenario_id": None})
    client.post("/api/session/inject", json={"scenario_id": "bearing-degradation"})
    state = client.post("/api/session/tick", json={"count": 18}).json()
    assert state["snapshot"]["machines"]["M-A"]["readings"]["vibration"]["value"] > 4.0
    assert state["snapshot"]["machines"]["M-A"]["health"] < 95


def test_unknown_scenario_is_rejected(client):
    assert client.post("/api/session/inject", json={"scenario_id": "nope"}).status_code == 404


def test_events_stream_is_readable(client):
    client.post("/api/session/tick", json={"count": 2})
    events = client.get("/api/events", params={"since": 0}).json()
    assert events["events"]
    assert all("seq" in e and "stage" in e for e in events["events"])


def test_audit_endpoint(client):
    client.post("/api/session/tick", json={"count": 2})
    audit = client.get("/api/audit").json()
    assert audit["run_id"].startswith("fg-")
    assert isinstance(audit["records"], list)


def test_approval_without_pending_request_is_rejected(client):
    response = client.post("/api/session/approve", json={"plan_id": "PLAN-D", "approved": True})
    assert response.status_code == 409


def test_full_loop_over_the_api_reaches_verification(client):
    """跑 Dashboard 上「執行 Agent 閉環」那顆按鈕的完整路徑。"""
    client.post("/api/session/reset", json={"scenario_id": "bearing-degradation"})
    assert client.post("/api/session/run", json={"max_ticks": 40}).json()["started"]

    deadline = time.time() + 60
    while time.time() < deadline:
        state = client.get("/api/state").json()
        if state["pending_approval"]:
            plan_id = state["pending_approval"]["plan"]["plan_id"]
            client.post("/api/session/approve",
                        json={"plan_id": plan_id, "approved": True, "approver": "test"})
        if not state["running"] and state["last_loop"]:
            break
        time.sleep(0.2)

    loop = client.get("/api/state").json()["last_loop"]
    assert loop is not None, "閉環未在時限內完成"
    assert loop["diagnosis"]["top_fault_id"] == "bearing_degradation"
    assert loop["executed_plan_id"]
    assert loop["verified"]


def test_benchmark_endpoint(client):
    report = client.post("/api/benchmark", json={"scenario_ids": ["bearing-degradation"]}).json()
    assert len(report["rows"]) == 3
    assert report["aggregate"]["guardian"]["production_attainment_pct"] > \
           report["aggregate"]["baseline-a"]["production_attainment_pct"]


# --------------------------------------------------------------------------------------
# 前端頁面與離線機台判讀
# --------------------------------------------------------------------------------------
def test_static_css_is_served(client):
    css = client.get("/static/hmi.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    # HMI 設計系統的兩條硬規則：直角、單一 hazard red 強調色
    assert "border-radius:0" in css.text.replace(" ", "")
    assert "--red:" in css.text.replace(" ", "")


def test_factory_cctv_demo_asset_is_served_and_disclosed(client):
    page = client.get("/")
    script = client.get("/static/camview.js")
    video = client.get("/static/factory-cctv-demo.mp4")
    hazard_video = client.get("/static/factory-cctv-hazard-demo.mp4")
    webm = client.get("/static/factory-cctv-demo.webm")
    hazard_webm = client.get("/static/factory-cctv-hazard-demo.webm")

    assert page.status_code == script.status_code == video.status_code == hazard_video.status_code == 200
    assert webm.status_code == hazard_webm.status_code == 200
    assert "DEMO FOOTAGE" in page.text
    assert "/static/factory-cctv-demo.mp4" in script.text
    assert "/static/factory-cctv-hazard-demo.mp4" in script.text
    assert "EVENT-MATCHED STOCK CLIP" in script.text
    assert "NO HI-VIS" in script.text
    assert "DEMO FOOTAGE / NOT LIVE" in script.text
    assert video.headers["content-type"] == "video/mp4"
    assert hazard_video.headers["content-type"] == "video/mp4"
    assert webm.headers["content-type"] == "video/webm"
    assert hazard_webm.headers["content-type"] == "video/webm"
    assert len(video.content) > 100_000
    assert len(hazard_video.content) > 100_000


def test_war_room_uses_light_industrial_palette(client):
    page = client.get("/")
    compact = page.text.replace(" ", "")
    assert page.status_code == 200
    assert '<metaname="color-scheme"content="light">' in compact
    assert "--bg:#E9EEF1" in compact
    assert "--fg:#163044" in compact
    assert "--accent:#0B756E" in compact


def test_war_room_groups_dense_panels_into_accessible_views(client):
    page = client.get("/")
    assert page.status_code == 200
    assert page.text.count('role="tab"') == 5
    assert 'data-console-tab="overview"' in page.text
    assert 'data-console-tab="equipment"' in page.text
    assert 'data-console-tab="decision"' in page.text
    assert 'data-console-tab="prediction"' in page.text
    assert 'data-console-tab="evidence"' in page.text
    assert page.text.count("data-console-panel=") == 11


def test_prediction_workbench_is_rendered(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="forecastForm"' in page.text
    assert 'id="forecastChart"' in page.text
    assert "/api/prediction/forecast" in page.text
    assert "Temporal Foundation Forecast" in page.text


def test_guided_demo_highlights_and_holds_each_agent_step(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="demoSpotlight"' in page.text
    assert "DEMO_STEP_HOLD_MS=1400" in page.text.replace(" ", "")
    assert 'aria-current","step"' in page.text
    assert "presentDemoThrough" in page.text


def test_default_demo_opens_hardware_and_agent_waterfall(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="demoHardwareRows"' in page.text
    assert 'id="demoHardwareLog"' in page.text
    assert 'id="demoSteps"' in page.text
    assert "grid-template-columns:1fr" in page.text
    assert "--red:#C43D35" in page.text.replace(" ", "")


def test_factory_briefing_is_a_standalone_page(client):
    factory = client.get("/factory")
    assert factory.status_code == 200
    assert "搞懂產線配置" in factory.text
    assert "M-A / PRIMARY CNC" in factory.text
    assert "M-B / FLEX CNC" in factory.text
    assert "M-C / PACKAGING" in factory.text
    assert "額定產能" in factory.text
    assert "120 U/HR" in factory.text
    assert "140 U/HR" in factory.text
    assert "200 U/HR" in factory.text
    assert '<img src="/static/factory-line-real.webp"' in factory.text
    assert "兩台封閉式 CNC 加工中心" in factory.text

    photo = client.get("/static/factory-line-real.webp")
    assert photo.status_code == 200
    assert photo.headers["content-type"] == "image/webp"

    machine_photos = (
        "factory-machine-a-real.webp",
        "factory-machine-b-real.webp",
        "factory-machine-c-real.webp",
    )
    for filename in machine_photos:
        assert f'<img src="/static/{filename}"' in factory.text
        response = client.get(f"/static/{filename}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/webp"


def test_favicon_is_served_and_linked(client):
    icon = client.get("/static/favicon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    link = '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">'
    assert link in client.get("/").text
    assert link in client.get("/benchmark").text


def test_benchmark_page_renders(client):
    page = client.get("/benchmark")
    assert page.status_code == 200
    assert "BENCHMARK REPORT" in page.text
    assert "SYNTHETIC DEMO DATA" in page.text
    assert "/static/hmi.css" in page.text


def test_both_pages_cross_link(client):
    assert 'href="/benchmark"' in client.get("/").text
    assert 'href="/"' in client.get("/benchmark").text


def test_agent_system_page_renders_and_is_linked(client):
    page = client.get("/system")
    hero = client.get("/static/agent-system-light.webp")
    assert page.status_code == 200
    assert hero.status_code == 200
    assert hero.headers["content-type"].startswith("image/webp")
    assert "六個 Agent" in page.text
    assert "LLM 負責解釋" in page.text
    assert page.text.count('role="img"') == 5
    assert "Agent control loop" in page.text
    assert "Authority map" in page.text
    assert "Safety decision tree" in page.text
    assert "/static/agent-system-light.webp" in page.text
    assert 'href="/system"' in client.get("/").text
    assert 'href="/system"' in client.get("/benchmark").text


def test_offline_machine_is_not_flagged_as_abnormal(client):
    """維修中的機台，感測器歸零不該被判成 WARNING / CRITICAL。"""
    client.post("/api/session/reset", json={"scenario_id": None})
    client.post("/api/session/tick", json={"count": 3})

    from factory_guardian.domain import Action, ActionKind

    session = client.app.state.session
    session.twin.apply(Action(ActionKind.STOP_MACHINE, "M-A"))
    session.twin.apply(Action(ActionKind.START_MAINTENANCE, "M-A", {"duration_min": 40}))
    state = client.post("/api/session/tick", json={"count": 5}).json()

    machine = state["snapshot"]["machines"]["M-A"]
    assert machine["online"] is False
    assert machine["state"] == "maintenance"
    assert all(r["band"] == "normal" for r in machine["readings"].values()), \
        "停機機台的讀值歸零是因為沒在跑，不是異常"
    assert machine["worst_band"] == "normal"


def test_monitoring_ignores_offline_machines(client):
    """正在被維修的機台不該持續觸發告警。"""
    from factory_guardian.domain import Action, ActionKind

    client.post("/api/session/reset", json={"scenario_id": "bearing-degradation"})
    session = client.app.state.session
    client.post("/api/session/tick", json={"count": 12})
    session.twin.apply(Action(ActionKind.STOP_MACHINE, "M-A"))
    session.twin.apply(Action(ActionKind.START_MAINTENANCE, "M-A", {"duration_min": 40}))

    before = len(session.orch.monitoring.events)
    client.post("/api/session/tick", json={"count": 15})
    assert len(session.orch.monitoring.events) == before


def test_live_state_survives_a_page_reload_mid_loop(client):
    """閉環進行中重新整理頁面，面板不能全部變空白。"""
    import time

    client.post("/api/session/reset", json={"scenario_id": "bearing-degradation"})
    client.post("/api/session/run", json={"max_ticks": 40})

    saw_live = False
    deadline = time.time() + 60
    while time.time() < deadline:
        state = client.get("/api/state").json()
        if state["live"].get("diagnose") or state["live"].get("plan"):
            saw_live = True
        if state["pending_approval"]:
            client.post("/api/session/approve",
                        json={"plan_id": state["pending_approval"]["plan"]["plan_id"], "approved": True})
        if not state["running"] and state["last_loop"]:
            break
        time.sleep(0.2)

    assert saw_live, "閉環期間 /api/state 應保留各階段產出供頁面還原"
    final = client.get("/api/state").json()
    assert final["live"].get("verify"), "驗證結果應留存於 live 狀態"
