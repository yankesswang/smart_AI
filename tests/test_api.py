"""儀表板 API 與 WebSocket 閉環測試。

重點在於驗證那條「同步閉環 ↔ 非同步 WebSocket」的橋接真的沒問題：
人工核准會阻塞 worker thread，瀏覽器回覆才解除；連線中斷時不能讓執行緒卡死。
"""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("httpx", reason="FastAPI TestClient 需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402

from aegismesh.api.server import LAYOUT, app  # noqa: E402
from aegismesh.twin import SCENARIOS  # noqa: E402

client = TestClient(app)


def test_topology_endpoint_includes_layout_for_every_node():
    data = client.get("/api/topology").json()
    assert len(data["nodes"]) == len(LAYOUT)
    for node in data["nodes"]:
        assert node["id"] in LAYOUT, f"{node['id']} 缺少版面座標，前端會畫不出來"
        assert (node["x"], node["y"]) == LAYOUT[node["id"]]


def test_topology_links_reference_existing_nodes():
    data = client.get("/api/topology").json()
    ids = {n["id"] for n in data["nodes"]}
    for link in data["links"]:
        assert link["src"] in ids and link["dst"] in ids


def test_topology_baseline_is_healthy():
    baseline = client.get("/api/topology").json()["baseline"]
    assert baseline["critical_availability_pct"] == 100.0
    assert all(s["slo_met"] for s in baseline["services"].values())


def test_snapshot_exposes_link_ids_for_frontend_path_rendering():
    """前端靠 link_ids 判斷業務走哪種 WAN，缺了就只能自己猜路徑。"""
    baseline = client.get("/api/topology").json()["baseline"]
    for sid, st in baseline["services"].items():
        assert st["link_ids"], f"{sid} 沒有回傳 link_ids"


def test_scenarios_endpoint_returns_runnable_netem():
    scs = client.get("/api/scenarios").json()
    assert {s["id"] for s in scs} == set(SCENARIOS)
    for s in scs:
        for f in s["faults"]:
            cmd = f["netem"]
            assert cmd.startswith(("ip link set", "tc qdisc"))
            assert "基準容量" not in cmd, "API 必須帶入容量，不該吐出佔位符"


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_episode_endpoint_is_self_describing(scenario_id):
    """推演分頁只靠這一個回應作畫，缺任何一格畫面上就會出現空白或 undefined。"""
    d = client.get(f"/api/episode/{scenario_id}").json()
    assert d["scenario"]["id"] == scenario_id
    assert [p["key"] for p in d["policies"]] == ["human", "aegis"]

    for policy in d["policies"]:
        assert policy["label"], "車道標題需要策略名稱"
        assert policy["steps"], "每個策略至少要有一個時段"
        for step in policy["steps"]:
            assert step["end_hour"] > step["hour"]
            assert step["services"], "沒有服務清單就畫不出「誰讓出了頻寬」"
            for svc in step["services"]:
                # 完整服務／降速／暫停三種狀態全靠這兩個數字比出來
                assert svc["required_mbps"] > 0
                assert svc["mbps"] >= 0
        hours = [s["end_hour"] for s in policy["steps"]]
        assert hours[-1] == pytest.approx(d["scenario"]["duration_hours"])


def test_episode_endpoint_uses_per_step_demand_for_required_bandwidth():
    """需求會隨時段變動（避難人潮、傷患湧入）。拿平常的需求對照，
    嚴重的降速會被標成「完整服務」—— 那是會誤導值班人員的畫面。"""
    steps = client.get("/api/episode/typhoon-fiber-cut").json()["policies"][0]["steps"]
    guest = [next(s for s in st["services"] if s["id"] == "svc-guest") for st in steps]
    assert [g["required_mbps"] for g in guest] == [500.0, 600.0, 400.0]


def test_episode_endpoint_shows_pacing_beating_the_human_heuristic():
    """這一頁的結論本身：颱風情境裡 AegisMesh 必須多撐出關鍵服務小時數，
    而且決策分岔的時刻要早於代價浮現的時刻 —— 否則整頁的論述不成立。"""
    d = client.get("/api/episode/typhoon-fiber-cut").json()
    human, aegis = d["policies"]
    assert d["delta_hours"] > 0
    assert aegis["critical_service_hours"] > human["critical_service_hours"]
    assert d["diverge_hour"] is not None and d["outcome_hour"] is not None
    assert d["diverge_hour"] < d["outcome_hour"], "代價若當場浮現，就不需要推演了"
    assert human["quota_exhausted_at"] < d["scenario"]["duration_hours"]


def test_episode_endpoint_rejects_unknown_scenario():
    r = client.get("/api/episode/does-not-exist")
    assert r.status_code == 404
    assert "未知情境" in r.json()["error"]


def test_health_endpoint():
    h = client.get("/api/health").json()
    assert h["status"] == "ok"
    assert isinstance(h["llm_online"], bool)


def _drain(ws, until: str, max_msgs: int = 200) -> list[dict]:
    seen = []
    for _ in range(max_msgs):
        msg = ws.receive_json()
        seen.append(msg)
        if msg["kind"] == until:
            return seen
    raise AssertionError(f"未收到 {until}，只看到：{[m['kind'] for m in seen]}")


def test_websocket_closed_loop_with_browser_approval():
    with client.websocket_connect("/ws/demo") as ws:
        ws.send_json({"action": "run", "scenario": "typhoon-fiber-cut"})

        seen = _drain(ws, "approval_request")
        kinds = [m["kind"] for m in seen]
        assert kinds[0] == "started"
        assert "baseline" in kinds and "incident" in kinds and "plans" in kinds

        req = seen[-1]
        assert req["plan"]["policy_findings"], "需核准的計畫必須附上政策發現"
        assert req["plan"]["actions"], "核准畫面必須列出將執行的動作"

        ws.send_json({"action": "approve", "approved": True})
        rest = _drain(ws, "finished")

        assert any(m["kind"] == "execute" for m in rest)
        done = rest[-1]
        assert done["summary"]["succeeded"]
        assert done["summary"]["critical_availability_pct"]["final"] == 100.0
        assert done["audit"]["verified"]


def test_websocket_rejection_stops_before_execute():
    """瀏覽器按下「拒絕」→ 不得有任何 execute 事件。"""
    with client.websocket_connect("/ws/demo") as ws:
        ws.send_json({"action": "run", "scenario": "typhoon-fiber-cut"})
        _drain(ws, "approval_request")

        ws.send_json({"action": "approve", "approved": False})
        rest = _drain(ws, "finished")

        assert not any(m["kind"] == "execute" for m in rest), "拒絕後不該執行任何變更"
        done = rest[-1]
        assert not done["summary"]["succeeded"]
        assert done["summary"]["executed_plan"] is None


def test_websocket_incident_snapshot_shows_the_damage():
    """順便涵蓋「評審看完事故畫面就關掉分頁」—— 連線必須乾淨結束，不能卡住。"""
    with client.websocket_connect("/ws/demo") as ws:
        ws.send_json({"action": "run", "scenario": "earthquake-dual-loss"})
        seen = _drain(ws, "incident")

        incident = next(m for m in seen if m["kind"] == "incident")["snapshot"]
        assert incident["critical_availability_pct"] == 0.0
        down = [lid for lid, l in incident["links"].items() if l["state"] == "down"]
        assert set(down) == {"w-fiber", "w-5g"}
        # 這裡直接離開 with 區塊 = 在核准請求送達前就斷線


def test_disconnect_before_approval_does_not_leak_worker_threads():
    """瀏覽器在核准前關掉分頁時，等待核准的 worker thread 必須立刻被釋放。

    早期版本用 asyncio Future 跨執行緒傳遞核准結果，結果是連線一斷、
    事件迴圈關閉，執行緒就再也收不到結果，卡滿 5 分鐘逾時才醒來。
    反覆重連會累積殭屍執行緒 —— 舞台上按幾次重整就會出事。
    """
    before = threading.active_count()

    for _ in range(3):
        with client.websocket_connect("/ws/demo") as ws:
            ws.send_json({"action": "run", "scenario": "typhoon-fiber-cut"})
            _drain(ws, "approval_request")     # 走到核准閘門，然後直接斷線

    deadline = time.monotonic() + 10.0
    while threading.active_count() > before + 1 and time.monotonic() < deadline:
        time.sleep(0.1)

    leaked = threading.active_count() - before
    assert leaked <= 1, f"斷線後仍有 {leaked} 個執行緒未釋放（應在數毫秒內結束）"


def test_websocket_ping():
    with client.websocket_connect("/ws/demo") as ws:
        ws.send_json({"action": "ping"})
        assert ws.receive_json()["kind"] == "pong"


@pytest.mark.parametrize("value", [0, 11, "not-a-number"])
def test_websocket_rejects_invalid_max_rounds(value):
    with client.websocket_connect("/ws/demo") as ws:
        ws.send_json({"action": "run", "max_rounds": value})
        msg = ws.receive_json()
        assert msg["kind"] == "error"
        assert "max_rounds" in msg["message"]


def test_websocket_reports_unknown_scenario():
    with client.websocket_connect("/ws/demo") as ws:
        ws.send_json({"action": "run", "scenario": "does-not-exist"})
        msg = ws.receive_json()
        assert msg["kind"] == "error"
        assert "未知情境" in msg["message"]


def test_topology_nodes_fit_inside_the_canvas():
    """節點超出畫布會被 SVG 切掉 —— 最右一欄的 HiCloud 曾經就這樣被裁掉一半。"""
    data = client.get("/api/topology").json()
    c = data["canvas"]
    for n in data["nodes"]:
        assert n["x"] + c["node_w"] <= c["w"], f"{n['id']} 超出畫布右緣"
        assert n["y"] + c["node_h"] <= c["h"], f"{n['id']} 超出畫布下緣"
        assert n["short"], f"{n['id']} 缺少拓樸圖短名稱"
