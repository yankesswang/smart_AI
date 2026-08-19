"""運作機制視角的測試(前端「運作機制」分頁的資料來源)。

這一頁的主張是「說明由生效中的程式碼匯出,不是另外維護的一份敘述」,
所以測試的重點不是欄位長相,而是:

1. 說明裡的數字真的跟著程式碼走(通道信任表、規則條數、必填欄位)。
2. **每一個探針宣告的預期結果,和管線實際做的事一致** —— 規則改了而說明沒改,
   這裡就會紅。畫面上寫「預期:攔在 G3」卻放行,是比沒有這一頁更糟的事。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentgate.api.server import create_app
from agentgate.gates.g1_resolution import _REQUIRED_PARAMS
from agentgate.gates.g2_policy import ACTION_POLICY, GATE_RULES
from agentgate.mechanism import GATES, describe
from agentgate.ontology import CHANNEL_MAX_RISK, CHANNEL_TRUST, ActionKind, Channel


@pytest.fixture
def client() -> TestClient:
    """和其他 API 測試一樣關掉當班流量:探針的結果要在安靜的環境裡判讀。"""
    return TestClient(create_app(live_ops=False))


class TestMechanismEndpoint:
    def test_exports_six_gates_in_order(self, client):
        data = client.get("/api/gate/mechanism").json()
        assert [g["gate"] for g in data["gates"]] == ["G0", "G1", "G2", "G3", "G4", "G5"]
        assert len(data["principles"]) == 3
        assert "影子環境" in data["disclaimer"]

    def test_flow_order_is_execution_order_not_gate_order(self):
        """G1 在 G0 之前跑,G0-R1 的檢查點在 G2 之後 —— 這是這一頁的主要論點。"""
        order = describe()["flow"]["order"]
        assert order.index("G1") < order.index("G0")
        assert order.index("G0-R1") > order.index("G2")

    def test_every_gate_has_decision_and_probe(self):
        for g in GATES:
            assert g["decision"]["title"] and g["decision"]["body"], g["gate"]
            assert g["probe"]["payload"]["kind"], g["gate"]
            assert g["probe"]["note"], g["gate"]

    def test_only_g2_is_marked_never_llm(self):
        """『政策裁決刻意不用 LLM』是差異化主張,不能被別的關卡稀釋。"""
        assert [g["gate"] for g in GATES if g["llm"] == "never"] == ["G2"]
        assert [g["gate"] for g in GATES if g["llm"] == "mapping"] == ["G1"]


class TestReferenceDataFollowsCode:
    """說明裡的表格必須是讀常數來的,不是手抄的。"""

    def test_trust_table_matches_channel_constants(self):
        rows = next(g for g in GATES if g["gate"] == "G0")["reference"]["rows"]
        assert len(rows) == len(Channel)
        for row in rows:
            ch = Channel(row["channel"])
            assert row["trust"] == CHANNEL_TRUST[ch]
            assert row["max_risk"] == CHANNEL_MAX_RISK[ch]

    def test_schema_table_matches_required_params(self):
        rows = next(g for g in GATES if g["gate"] == "G1")["reference"]["rows"]
        assert {r["action"]: tuple(r["required"]) for r in rows} == {
            k.value: tuple(v) for k, v in _REQUIRED_PARAMS.items()}

    def test_policy_counts_match_engine(self):
        ref = next(g for g in GATES if g["gate"] == "G2")["reference"]
        assert ref["actions"] == len(ACTION_POLICY) == len(ActionKind)
        assert ref["rules"] == len(GATE_RULES)


class TestProbesTellTheTruth:
    """探針宣告的預期,必須和管線實際做的事一致。"""

    @pytest.mark.parametrize("gate", [g["gate"] for g in GATES])
    def test_probe_outcome_matches_declared_expectation(self, client, gate):
        g = next(x for x in GATES if x["gate"] == gate)
        probe = g["probe"]
        res = client.post("/api/gate/evaluate", json=probe["payload"])

        # G1 的攔截發生在 API 邊界(解析失敗,沒有 GateVerdict 可回)。
        if res.status_code == 422:
            assert probe["expect"] == "blocked"
            assert res.json()["detail"]["gate"] == probe["expect_gate"] == "G1"
            return

        assert res.status_code == 200
        verdict = res.json()
        assert verdict["status"] == probe["expect"], probe["label"]
        assert verdict["gate_blocked_at"] == probe["expect_gate"], probe["label"]

    def test_g3_probe_really_is_a_scope_escape(self, client):
        """G3 的說服力全在「申報 50、實測遠不止」這個落差上,落差沒了就不必演。"""
        probe = next(g for g in GATES if g["gate"] == "G3")["probe"]
        declared = probe["payload"]["params"]["declared_count"]
        sim = client.post("/api/gate/simulate", json=probe["payload"]).json()
        assert sim["projection"]["affected_count"] > declared

    def test_probes_leave_audit_trail(self, client):
        """被擋下的探針一樣要留痕 —— 這是原則三,不能只在文字上成立。"""
        g0 = next(g for g in GATES if g["gate"] == "G0")["probe"]
        before = client.get("/api/gate/audit").json()["total"]
        client.post("/api/gate/evaluate", json=g0["payload"])
        after = client.get("/api/gate/audit").json()
        assert after["total"] > before
        assert any(r["stage"] == "g5_blocked" for r in after["records"])


class TestMechanismPage:
    def test_page_and_assets_served(self, client):
        page = client.get("/mechanism")
        assert page.status_code == 200
        assert "mechanism.js" in page.text
        for asset in ("/static/mechanism.js", "/static/mechanism.css"):
            assert client.get(asset).status_code == 200

    def test_module_does_not_leak_globals(self):
        """視角要能直接掛進 Dashboard,而 index.html 的 script 是頂層 const —— \
模組必須關在 IIFE 裡,只露出一個名字,否則重名會讓整頁 SyntaxError。"""
        import re
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "agentgate/api/static/mechanism.js").read_text(encoding="utf-8")
        assert "window.AgentGateMechanism = (function()" in src
        assert re.findall(r"^window\.(\w+)\s*=", src, re.M) == ["AgentGateMechanism"]
