"""API 與 Dashboard 後端。"""

from __future__ import annotations

import re
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


def test_deployment_endpoint_exposes_the_edge_cloud_split(client):
    body = client.get("/api/deployment").json()

    assert body["link"]["status"] == "up"
    assert body["tiers"]["summary"]["critical_path_is_edge_only"]
    assert body["tiers"]["summary"]["edge_count"] >= 10
    assert body["tiers"]["summary"]["cloud_count"] >= 4

    # 分層表的每一列都要能被追到程式碼，不能只是方塊圖上的名詞。
    assert all(component["module"] for component in body["tiers"]["components"])

    # 頻寬與延遲都要是數字，而且每個數字都標了來源。
    assert body["bandwidth_budget"]["total_uplink_mbps"] > 0
    assert body["latency_budget"]["within_target"]
    assert body["latency_budget"]["total_ms"] < body["latency_budget"]["target_ms"]
    assert body["edge_decision_measurement"]["iterations"] >= 20

    # OT 隔離：影像、波形與控制指令一律不出廠。
    egress = {rule["data_class"]: rule["allowed"] for rule in body["tiers"]["egress_policy"]}
    assert not egress["原始攝影機影格"]
    assert not egress["控制指令（stop_machine / derate / start_maintenance）"]


def test_deployment_endpoint_rejects_an_unknown_scale(client):
    assert client.get("/api/deployment", params={"scale": "nope"}).status_code == 404


def test_cloud_link_toggle_degrades_only_the_cloud_tier(client):
    down = client.post("/api/deployment/link",
                       json={"up": False, "reason": "評審現場斷網演練", "actor": "judge"}).json()
    assert down["link"]["status"] == "down"
    assert down["link"]["mode"] == "edge-autonomous"
    assert down["settings"]["llm_mode"] == "edge-autonomous"

    report = client.get("/api/deployment").json()
    assert report["degradation"]["active"]
    assert report["degradation"]["control_loop_impact"] == "none"
    assert "llm-narrative" in report["degradation"]["degraded_components"]
    assert "safety-agent" in report["degradation"]["unaffected_components"]

    # 狀態要同時反映在 /api/state（頁首那一格靠它）與事件串流上。
    assert client.get("/api/state").json()["settings"]["cloud_link"] == "down"
    assert any(event["stage"] == "cloud_link" for event in client.get("/api/events").json()["events"])
    assert any(record["stage"] == "cloud_link" for record in client.get("/api/audit").json()["records"])

    up = client.post("/api/deployment/link", json={"up": True, "reason": "恢復"}).json()
    assert up["link"]["status"] == "up"


def test_full_loop_over_the_api_survives_a_severed_cloud_link(client):
    """Demo 現場最有說服力的一段：拔掉雲端，閉環照樣跑完並通過驗證。"""
    client.post("/api/session/reset", json={"scenario_id": "bearing-degradation"})
    client.post("/api/deployment/link", json={"up": False, "reason": "模擬廠區對外鏈路中斷"})
    assert client.post("/api/session/run", json={"max_ticks": 40}).json()["started"]

    deadline = time.time() + 60
    while time.time() < deadline:
        state = client.get("/api/state").json()
        if state["pending_approval"]:
            client.post("/api/session/approve", json={
                "plan_id": state["pending_approval"]["plan"]["plan_id"],
                "approved": True, "approver": "test",
            })
        if not state["running"] and state["last_loop"]:
            break
        time.sleep(0.2)

    loop = client.get("/api/state").json()["last_loop"]
    assert loop is not None, "斷網下閉環未在時限內完成"
    assert loop["diagnosis"]["top_fault_id"] == "bearing_degradation"
    assert loop["executed_plan_id"] and loop["executed_plan_id"] != "PLAN-A"
    assert loop["verified"]

    # 降級必須留下稽核紀錄。
    records = client.get("/api/audit", params={"limit": 400}).json()["records"]
    assert any(record["stage"] == "degrade" for record in records)
    client.post("/api/deployment/link", json={"up": True, "reason": "恢復"})


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
    assert page.text.count('role="tab"') == 6
    assert 'data-console-tab="overview"' in page.text
    assert 'data-console-tab="equipment"' in page.text
    assert 'data-console-tab="decision"' in page.text
    assert 'data-console-tab="prediction"' in page.text
    assert 'data-console-tab="deployment"' in page.text
    assert 'data-console-tab="evidence"' in page.text
    assert page.text.count("data-console-panel=") == 12


def test_deployment_panel_reuses_the_existing_design_vocabulary(client):
    """部署面板不得帶進新的視覺語彙。"""
    page = client.get("/")
    assert 'data-console-panel="deployment"' in page.text
    assert 'id="deployTiers"' in page.text and 'id="btnCloudLink"' in page.text
    # 頁首常駐顯示鏈路狀態：現場切斷網時評審一眼就看得到。
    assert 'id="tLink"' in page.text
    # terminal green 全站只給 VERIFIED 鋼印，部署面板不准用 .tag.ok 搶語意。
    deployment_js = page.text.split("部署：Edge / Cloud")[1].split("工安 / 工單")[0]
    assert "tag ok" not in deployment_js
    # 沒有圓角、陰影、漸層，顏色一律走既有 token，不出現任何硬寫的色碼。
    for banned in ("border-radius", "box-shadow", "linear-gradient"):
        assert banned not in deployment_js
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", deployment_js)


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


def test_topology_and_snapshot_carry_plain_language_short_names(client):
    """白話短名由後端給，前端不切字串。

    「Machine A｜CNC 主要加工機」要在 46 個渲染點各自切一次的話，遲早會漏掉一處，
    那一處就會在評審面前冒出一個沒人看得懂的代號。所以只在後端推導一次。
    """
    topology = client.get("/api/topology").json()
    machines = {m["machine_id"]: m for m in topology["machines"]}
    assert machines["M-A"]["short_name"] == "主要加工機"
    assert machines["M-B"]["short_name"] == "替代加工機"
    assert machines["M-C"]["short_name"] == "包裝機"
    # 代號沒有被拿掉：稽核與跨畫面對照仍然需要它。
    assert machines["M-A"]["name"] == "Machine A｜CNC 主要加工機"

    products = {p["product_id"]: p for p in topology["products"]}
    assert products["P-100"]["short_name"] == "精密軸承座"
    assert products["P-200"]["short_name"] == "通用連接件"
    assert topology["lines"][0]["short_name"] == "精密零件產線"

    # 即時快照也帶一個可以直接顯示的名字，當 /api/topology 取不到時的降級來源。
    # 它由 name 推導（「Machine C｜後段包裝機」→「後段包裝機」），一定不含代號。
    snapshot = client.get("/api/state").json()["snapshot"]
    packer = snapshot["machines"]["M-C"]["short_name"]
    assert packer == "後段包裝機"
    assert "Machine" not in packer and "M-C" not in packer


def test_war_room_shows_plain_names_first_and_keeps_codes_as_secondary(client):
    page = client.get("/")
    assert page.status_code == 200

    # 對照表：收在 details 裡、預設收合，不強迫第一次看的人先讀一張表。
    assert 'id="glossary"' in page.text and 'id="glossaryGrid"' in page.text
    assert "名詞對照：畫面上的代號分別是什麼" in page.text
    assert "<details class=\"advanced-controls\" id=\"glossary\">" in page.text
    assert "<details open" not in page.text

    # 白話為主、代號為輔：代號一律走 .code 這個降級樣式，沒有被刪掉。
    assert ".code{" in page.text
    assert "machineName" in page.text and "orderName" in page.text and "planName" in page.text
    # 訂單／方案改成自帶語意的寫法，但可回溯的字母留著（ORD-A001 → 訂單 A001）。
    assert '"訂單 "' in page.text and '"方案 "' in page.text

    # 像素視圖畫不出中文（3×5 ASCII 字模、且刻意不載字型），
    # 所以圖上留代號、白話對照用 HTML 補在 canvas 正下方。
    assert 'id="arcadeNames"' in page.text
    assert ".fp-key .names{" in page.text


def test_plain_language_pass_keeps_the_design_vocabulary(client):
    """名詞白話化不得帶進新的視覺語彙。"""
    page = client.get("/")
    naming = page.text.split("---- 代號降級")[1].split("---- Guided Demo")[0]
    # terminal green 全站只給 VERIFIED 鋼印；新樣式不准用 .tag.ok 搶語意。
    assert "tag.ok" not in naming and "tag ok" not in naming
    # 沒有圓角、陰影、漸層，顏色一律走既有 token，不出現任何硬寫的色碼。
    for banned in ("border-radius", "box-shadow", "linear-gradient"):
        assert banned not in naming
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", naming)
    assert "var(--line)" in naming and "var(--dimmer)" in naming


def test_factory_briefing_is_a_standalone_page_linked_before_demo(client):
    page = client.get("/")
    assert page.status_code == 200
    assert page.text.index('class="factory-entry"') < page.text.index('id="demoLab"')
    assert '<a href="/factory">查看產線配置' in page.text

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


def test_console_agent_stages_are_drawn_as_a_closed_loop_diagram(client):
    """主控台的六列文字表格換成一張會轉的閉環流程圖。"""
    page = client.get("/")
    assert page.status_code == 200
    # 圖本身：inline SVG，不是 canvas（點陣字模畫不出中文），也不載任何外部函式庫。
    assert 'id="loopMap"' in page.text
    assert "<svg viewBox=" in page.text
    assert "<canvas" not in page.text.split('id="loopMap"')[1].split('id="demoSpotlight"')[0]
    # 八個節點只放動詞短語；核准與執行是原本被壓在「守住安全邊界／執行並驗證」裡的兩個真實環節。
    for verb in ("發現異常", "找出根因", "算出影響", "比較方案", "安全把關", "核准", "執行", "驗證"):
        assert verb in page.text
    node_ids = ("detect", "diagnose", "impact", "plan", "safety", "approve", "execute", "verify")
    for node_id in node_ids:
        assert f'{{id:"{node_id}"' in page.text.replace(" ", "")
    # 回饋線是這張圖的靈魂：驗證沒過退回比較方案，而且畫面上要講得出來。
    assert "退回重新規劃" in page.text
    assert "lm-edge back" in page.text and 'id="lmBack"' in page.text
    # 舊的六列步驟卡不該留下來跟流程圖並存。
    assert 'class="demo-step"' not in page.text
    assert 'class="ds-value"' not in page.text


def test_console_loop_diagram_is_bound_to_real_backend_state(client):
    """流程圖不得自己造一份狀態：每一格都要對得回 /api/state 的欄位。"""
    page = client.get("/")
    loop_js = page.text.split("================= 閉環流程圖 =================")[1] \
                       .split("================= 步驟推理 =================")[0]
    # 核准這一格看 pending_approval 與 LoopAttempt.approval，不是前端假造的旗標。
    assert "state.pending_approval" in loop_js
    assert "attempt.approval" in loop_js and "ap.approved" in loop_js
    # 執行看 LoopAttempt.executed / execution_effects；驗證看 verification.passed。
    assert "attempt.executed" in loop_js and "execution_effects" in loop_js
    assert "verification.passed" in loop_js
    # 「退回幾次」＝ attempts 多出來的那幾次，不是動畫寫死的數字。
    assert "attempts.length-1" in loop_js.replace(" ", "")
    # Safety 擋下方案才轉 hazard，其餘狀態靠亮度與反白分級。
    assert 'verdict==="BLOCK"' in loop_js


def test_console_loop_diagram_keeps_the_design_vocabulary(client):
    """流程圖不得帶進新的視覺語彙，也不准拿 terminal green 當「已完成」。"""
    page = client.get("/")
    loop_css = page.text.split("---- 閉環流程圖")[1].split(".demo-live{display:grid")[0]
    # terminal green 全站只給 VERIFIED 鋼印；已完成一律用亮度分級。
    assert "var(--green)" not in loop_css and "tag ok" not in loop_css
    # 沒有圓角、陰影、漸層，顏色一律走既有 token，不出現任何硬寫的色碼。
    for banned in ("border-radius", "box-shadow", "linear-gradient"):
        assert banned not in loop_css
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", loop_css)
    assert "var(--line)" in loop_css and "var(--red)" in loop_css
    # 窄螢幕改直排＋左側回饋軌，不靠把整張圖縮到看不清楚。
    assert "LM_TALL" in page.text and "(max-width:760px)" in page.text


AGENT_GLYPHS = ("agi-monitor", "agi-fingerprint", "agi-impact", "agi-rank",
                "agi-shield", "agi-human", "agi-wrench", "agi-measure")


def test_agent_identity_glyphs_are_inline_svg_symbols_shared_by_both_pages(client):
    """六個 Agent ＋『人』各有一個圖示，兩頁共用同一組 symbol 造型。"""
    console, system = client.get("/").text, client.get("/system").text
    for page in (console, system):
        assert '<svg class="ag-sprite"' in page
        for glyph in AGENT_GLYPHS:
            assert f'<symbol id="{glyph}" viewBox="0 0 24 24">' in page
    # 圖示是自己畫的 inline SVG：不載圖示函式庫、不載 web font、不新增圖檔資產。
    assert "<link rel=\"stylesheet\" href=\"http" not in console + system
    for banned in ("font-awesome", "material-icons", "lucide", "feather", "@font-face"):
        assert banned not in (console + system).lower()
    # /system 的三張說明圖仍然是三張：圖示走 <symbol>/<use>，不會被誤算成第四張圖。
    assert system.count("<svg viewBox=") == 3


def test_agent_glyphs_reach_every_surface_that_identifies_an_agent(client):
    """圖示要一致地出現在所有標示 Agent 的地方，不能只做流程圖那一處。"""
    console, system = client.get("/").text, client.get("/system").text
    # 戰情中心：八格流程圖節點各自綁定圖示，八個圖示都用到。
    assert 'class="lm-ic" href="#agi-${esc(n.icon)}"' in console
    for icon in ("monitor", "fingerprint", "impact", "rank", "shield", "human", "wrench", "measure"):
        assert f'icon:"{icon}"' in console.replace(" ", "")
    # 聚光燈列、步驟推理面板標頭、Agent 判讀列、人工核准框。
    assert 'id="spotMark"' in console and '$("spotMark").innerHTML=agGlyph(n.icon)' in console
    assert "agGlyph(icon)}${esc(who)}" in console
    assert 'id="agentGlyph"' in console and 'STAGE_ICON[p.stage]' in console
    assert 'agGlyph("human")} HUMAN APPROVAL REQUIRED' in console
    # /system：閉環總圖八格、七個步驟的 agent chip、案例時間軸的「誰」欄。
    assert system.count('<use class="ic" href="#agi-') == 8
    assert system.count('class="agent-chip"><svg class="ag"') == 5
    assert system.count('class="agent-chip solid"><svg class="ag"') == 2
    assert system.count('<div class="case-who"><svg class="ag"') == 9


def test_human_approval_glyph_is_visually_separated_from_the_agent_glyphs(client):
    """『人工核准』那一格是人不是 Agent，圖示語彙上要看得出差別。"""
    console = client.get("/").text
    human = console.split('<symbol id="agi-human"')[1].split("</symbol>")[0]
    # 人形是全套唯一實心填滿的圖示（頭、肩、簽名底線三塊），完全沒有線稿筆畫。
    assert human.count('stroke="none"') == 3 and "<path" not in human
    # 其餘 Agent 一律線稿：主體不整塊填滿。
    for glyph in ("agi-monitor", "agi-fingerprint", "agi-impact", "agi-shield", "agi-wrench"):
        body = console.split(f'<symbol id="{glyph}"')[1].split("</symbol>")[0]
        assert 'fill="currentColor"' not in body
    # 流程圖上那一格另外標成 human，左側標記改成分段的簽名式短線。
    assert 'actor:"human"' in console.replace(" ", "")
    assert 'n.actor==="human"' in console.replace(" ", "")


def test_agent_glyph_styles_keep_the_design_vocabulary(client):
    """圖示不得帶進新的視覺語彙，也不准用 terminal green 表示狀態。"""
    console = client.get("/").text
    glyph_css = console.split("---- Agent 視覺識別")[1].split(".demo-live{display:grid")[0]
    # terminal green 全站只給 VERIFIED 鋼印；圖示的狀態分級只走亮度與反白。
    assert "var(--green)" not in glyph_css and "tag ok" not in glyph_css
    assert "opacity" in glyph_css and "var(--accent-ink)" in glyph_css
    # 沒有圓角、陰影、漸層，顏色一律走既有 token，不出現任何硬寫的色碼。
    for banned in ("border-radius", "box-shadow", "linear-gradient"):
        assert banned not in glyph_css
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", glyph_css)
    # symbol 本身也不寫死顏色：一律 currentColor，才跟得上反白與 hazard 狀態。
    for page in (console, client.get("/system").text):
        sprite = page.split('<svg class="ag-sprite"')[1].split("</svg>")[0]
        assert "currentColor" in sprite
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", sprite)
        assert "fill=\"none\"" not in sprite  # 線稿靠 CSS 繼承，不在 symbol 裡寫死


def test_agent_system_page_explains_the_loop_with_diagrams(client):
    """/system 的三段純文字換成 inline SVG，而且圖是取代文字不是疊加。"""
    page = client.get("/system")
    assert page.status_code == 200
    # 三張圖：閉環總圖、安全裁決決策圖、人機權限軸。
    assert page.text.count("<svg viewBox=") == 3
    assert page.text.count("<figcaption>") == 3
    assert "<canvas" not in page.text

    # 1) 閉環總圖取代了「整條流程，一行看完」那七個文字方塊。
    assert 'class="chain"' not in page.text and 'class="chain-note"' not in page.text
    assert "整條流程，一張圖看完" in page.text
    assert "退回 04 重新規劃" in page.text
    for agent in ("MONITORING AGENT", "DIAGNOSIS AGENT", "PRODUCTION AGENT",
                  "SAFETY AGENT · POLICY ENGINE", "MAINTENANCE AGENT", "VERIFICATION AGENT"):
        assert agent in page.text

    # 2) 決策圖取代了三張裁決文字卡。
    assert 'class="guard block"' not in page.text and 'class="guards"' not in page.text
    assert "觸發安全硬規則？" in page.text and "會改變機台控制狀態？" in page.text

    # 3) 風險軸取代了七列權限表。
    assert "<table" not in page.text and 'class="tbl"' not in page.text
    assert "這條線以右，一定要人簽名" in page.text
    for action in ("RAISE ALERT", "CREATE WORK ORDER", "DERATE MACHINE",
                   "STOP MACHINE", "START MAINTENANCE", "SAFETY OVERRIDE"):
        assert action in page.text

    # 顏色一律走 token；新樣式不出現硬寫色碼、圓角、陰影、漸層。
    fig_css = page.text.split("/* ── 圖 ─")[1].split("@media(max-width:1080px)")[0]
    for banned in ("border-radius", "box-shadow", "linear-gradient"):
        assert banned not in fig_css
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", fig_css)
    # 窄螢幕讓圖在自己的容器裡橫捲，不把整個頁面撐寬。
    assert "overflow-x:auto" in fig_css
    assert ".system-page > *{min-width:0}" in page.text


def test_agent_system_page_renders_and_is_linked(client):
    page = client.get("/system")
    hero = client.get("/static/agent-system-light.webp")
    assert page.status_code == 200
    assert hero.status_code == 200
    assert hero.headers["content-type"].startswith("image/webp")
    assert "六個 Agent" in page.text
    # 七個步驟每一步都要在頁面上（step-no 是步驟編號欄）
    assert page.text.count('class="step-no"') == 7
    # 三個關鍵界線：權限表、LLM 能與不能、安全裁決
    assert "LLM 不能做的事" in page.text
    assert "沒有人能核准" in page.text          # Safety Override 禁止
    assert "一定要人核准" in page.text          # 高風險動作 human-in-the-loop
    for verdict in ("BLOCK", "APPROVAL", "ALLOW"):
        assert verdict in page.text
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
