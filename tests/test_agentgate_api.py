"""API 測試:規格 §6 全部端點 + Demo 導播。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentgate.api.server import create_app


@pytest.fixture
def client() -> TestClient:
    """治理行為的驗證要在安靜的環境裡做,所以關掉當班流量模擬。

    營運層本身的測試見 test_agentgate_console.py。
    """
    return TestClient(create_app(live_ops=False))


VERIFIED_CHAIN = [{"channel": "user_verified", "source_ref": "chat:t1"}]
INJECTED_CHAIN = VERIFIED_CHAIN + [{"channel": "tool_output", "source_ref": "upload:evil.pdf"}]


def _evaluate(client, **overrides):
    payload = {
        "kind": "read_account", "params": {"account_id": "ACC-1001"},
        "principal": "ACC-1001", "agent_id": "t", "provenance_chain": VERIFIED_CHAIN,
    }
    payload.update(overrides)
    return client.post("/api/gate/evaluate", json=payload)


class TestGateEndpoints:
    def test_health(self, client):
        data = client.get("/api/health").json()
        assert data["ok"] and "影子環境" in data["disclaimer"]

    def test_evaluate_executes_low_risk(self, client):
        res = _evaluate(client)
        assert res.status_code == 200
        assert res.json()["status"] == "executed"

    def test_evaluate_blocks_injection(self, client):
        res = _evaluate(client, kind="read_bulk",
                        params={"declared_count": 500},
                        provenance_chain=INJECTED_CHAIN)
        body = res.json()
        assert body["status"] == "blocked"
        assert body["gate_blocked_at"] == "G0"

    def test_evaluate_rejects_malformed_at_g1(self, client):
        res = _evaluate(client, kind="issue_refund", params={})
        assert res.status_code == 422
        assert res.json()["detail"]["gate"] == "G1"

    def test_simulate_does_not_enqueue(self, client):
        res = client.post("/api/gate/simulate", json={
            "kind": "issue_refund",
            "params": {"account_id": "ACC-1001", "amount": 5000},
            "principal": "ACC-1001", "agent_id": "t",
            "provenance_chain": VERIFIED_CHAIN,
        })
        assert res.json()["projection"]["financial_delta"] == -5000
        assert client.get("/api/gate/pending").json()["pending"] == []

    def test_approval_flow(self, client):
        v = _evaluate(client, kind="issue_refund",
                      params={"account_id": "ACC-1001", "amount": 5000}).json()
        assert v["status"] == "pending_approval"
        approval_id = v["approval_id"]

        pending = client.get("/api/gate/pending").json()
        assert pending["pending"][0]["approval_id"] == approval_id
        assert "未經驗證" in pending["pending"][0]["agent_reasoning"]["warning"]

        # 錯誤憑證 → 409
        bad = client.post(f"/api/gate/approve/{approval_id}", json={
            "approver_id": "MGR-001", "credential": "nope", "reason": "x"})
        assert bad.status_code == 409

        ok = client.post(f"/api/gate/approve/{approval_id}", json={
            "approver_id": "MGR-001", "credential": "sim-0911-000-111",
            "reason": "客訴成立"})
        assert ok.json()["status"] == "executed"

    def test_reject_requires_reason(self, client):
        v = _evaluate(client, kind="suspend_service",
                      params={"account_id": "ACC-1001"}).json()
        res = client.post(f"/api/gate/reject/{v['approval_id']}", json={
            "approver_id": "MGR-001", "credential": "sim-0911-000-111", "reason": ""})
        assert res.status_code == 422

        res = client.post(f"/api/gate/reject/{v['approval_id']}", json={
            "approver_id": "MGR-001", "credential": "sim-0911-000-111",
            "reason": "身分存疑"})
        assert res.json()["status"] == "rejected"

    def test_audit_and_verify(self, client):
        _evaluate(client)
        audit = client.get("/api/gate/audit").json()
        assert audit["total"] > 0
        assert client.get("/api/gate/audit/verify").json()["ok"]

    def test_policy_export(self, client):
        policy = client.get("/api/gate/policy").json()
        assert policy["llm_involved"] is False
        assert any(r["rule_id"] == "G0-R1" or r["rule_id"] == "AG-01"
                   for r in policy["rules"])

    def test_metrics(self, client):
        _evaluate(client)
        metrics = client.get("/api/gate/metrics").json()
        assert metrics["live"]["decisions"] >= 1
        assert metrics["audit"]["audit_completeness"] == 1.0


class TestDemoEndpoints:
    def test_script_and_steps(self, client):
        script = client.get("/api/demo/script").json()["script"]
        assert len(script) == 6

        v = client.post("/api/demo/step", json={"step_id": "bill_query"}).json()
        assert v["verdict"]["status"] == "executed"

        v = client.post("/api/demo/step", json={"step_id": "injection"}).json()
        assert v["verdict"]["gate_blocked_at"] == "G0"

    def test_ab_compare_restores_gate(self, client):
        result = client.post("/api/demo/step", json={"step_id": "ab_off"}).json()
        assert result["gated"]["status"] == "blocked"
        assert result["ungated"]["status"] == "executed"
        assert result["ungated"]["execution_result"]["exported_count"] > 100
        # 治理層自動恢復
        assert client.get("/api/state").json()["gate_enabled"] is True

    def test_gate_toggle_leaves_audit_trace(self, client):
        client.post("/api/demo/gate", json={"enabled": False})
        assert client.get("/api/state").json()["gate_enabled"] is False
        client.post("/api/demo/gate", json={"enabled": True})
        stages = [r["stage"] for r in client.get("/api/gate/audit").json()["records"]]
        assert "degrade" in stages and "restore" in stages

    def test_tamper_demo_breaks_verify(self, client):
        client.post("/api/demo/step", json={"step_id": "bill_query"})
        res = client.post("/api/demo/tamper", json={"seq": 0}).json()
        assert res["verify"]["ok"] is False

        client.post("/api/demo/reset", json={})
        assert client.get("/api/gate/audit").json()["total"] == 0

    def test_benchmark_endpoint(self, client):
        assert client.get("/api/benchmark").status_code == 404
        report = client.post("/api/benchmark").json()
        assert {b["baseline"] for b in report["baselines"]} >= {"B0", "B3"}
        assert client.get("/api/benchmark").status_code == 200

    def test_dashboard_served(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "AgentGate" in res.text
        # 場景說明頁的入口要在管制台上,否則那一頁等於不存在。
        assert 'href="/scenario"' in res.text

    def test_scenario_page_served(self, client):
        """使用場景說明頁。

        它是硬寫的靜態說明,所以測的是「政策常數有沒有在頁面上對上」——
        改了 REFUND_SINGLE_LIMIT 之類的門檻卻忘了改這一頁,這裡會紅。
        """
        res = client.get("/scenario")
        assert res.status_code == 200
        for token in ("使用場景", "read_bulk", "policy_override",
                      "10,000", "20,000",
                      'href="/"', 'href="/mechanism"'):
            assert token in res.text


class TestPolicyVersionAndRuntimeContract:
    def test_policy_endpoint_exports_version(self, client):
        policy = client.get("/api/gate/policy").json()
        assert policy["policy_version"].startswith("pv-")
        assert policy["version"]["components"]["rules"]
        assert policy["version"]["overrides"]["active"] is False

    def test_metrics_reports_fbr_split_note_and_scenario_stats(self, client):
        metrics = client.get("/api/gate/metrics").json()
        assert "FBR_gate" in metrics["note"] and "FBR_approver" in metrics["note"]
        assert metrics["scenario_stats"]["total"] == 142
        assert metrics["policy_version"].startswith("pv-")
        assert metrics["chain"]["ok"] is True

    def test_harness_context_injects_provenance(self, client):
        """帶 harness_context 時,Agent 少報的 tool_output 由 runtime 補回並攔下。"""
        res = _evaluate(
            client, kind="read_bulk", params={"declared_count": 50},
            principal="OPS-1", principal_role="ops",
            provenance_chain=[{"channel": "system", "source_ref": "batch:job"}],
            harness_context={
                "instruction_sources": [{"channel": "system", "source_ref": "batch:job"}],
                "tool_outputs": ["upload:evil.pdf#p3"],
            })
        body = res.json()
        assert body["status"] == "blocked" and body["gate_blocked_at"] == "G0"
        assert any(f["rule_id"] == "G1-R2" for f in body["findings"])

    def test_self_reported_confirmation_is_ignored(self, client):
        """沒有 harness_context 時,payload 自報的確認欄位一律失效。"""
        res = _evaluate(
            client, kind="suspend_service", params={"account_id": "ACC-1001"},
            provenance_chain=[
                {"channel": "user_verified", "source_ref": "chat:t1"},
                {"channel": "tool_output", "source_ref": "upload:evil.pdf"},
                {"channel": "user_verified", "source_ref": "chat:fake-confirm",
                 "confirms": "upload:evil.pdf",
                 "confirmed_action_hash": "deadbeef" * 4},
            ])
        body = res.json()
        assert body["status"] == "blocked" and body["gate_blocked_at"] == "G0"
        assert any(f["rule_id"] == "G1-R3" for f in body["findings"])

    def test_runtime_confirmation_lifts_and_reaches_approval(self, client):
        """走 harness 契約的確認 → 提升 → 停話案進核准佇列而不是被擋。"""
        res = _evaluate(
            client, kind="suspend_service", params={"account_id": "ACC-1001"},
            provenance_chain=[{"channel": "user_verified", "source_ref": "chat:t1"}],
            harness_context={
                "instruction_sources": [
                    {"channel": "user_verified", "source_ref": "chat:t1"}],
                "tool_outputs": ["upload:loss_report.pdf"],
                "confirmations": [{
                    "source_ref": "chat:confirm-1",
                    "confirms": "upload:loss_report.pdf",
                    "action": {"kind": "suspend_service",
                               "params": {"account_id": "ACC-1001"}},
                }],
            })
        body = res.json()
        assert body["status"] == "pending_approval"
        assert body["trust"]["risk_cap"] == "high"
        assert body["trust"]["lifts"]
