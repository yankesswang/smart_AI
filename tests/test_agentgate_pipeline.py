"""AgentGate 管線端到端測試:五關卡編排、核准流程、A/B 對照、稽核完整性。"""

from __future__ import annotations

import pytest

from agentgate.ontology import ActionKind, ActionRequest, Channel, PrincipalRole, Provenance
from agentgate.pipeline import AgentGatePipeline, GateConfig
from agentgate.shadow import ShadowTelecomEnv


@pytest.fixture
def pipeline() -> AgentGatePipeline:
    return AgentGatePipeline(shadow=ShadowTelecomEnv())


def _req(kind: ActionKind, params: dict, principal: str = "ACC-1001",
         role: PrincipalRole = PrincipalRole.CUSTOMER,
         chain: list[Provenance] | None = None, reasoning: str = "") -> ActionRequest:
    return ActionRequest(
        kind=kind, params=params, principal=principal, principal_role=role,
        agent_id="test-agent", reasoning=reasoning,
        provenance_chain=chain if chain is not None
        else [Provenance(Channel.USER_VERIFIED, "chat:test")],
    )


INJECTED = [
    Provenance(Channel.USER_VERIFIED, "chat:test"),
    Provenance(Channel.TOOL_OUTPUT, "upload:evil.pdf#p3"),
]


class TestHappyPath:
    def test_low_risk_auto_executes(self, pipeline):
        v = pipeline.evaluate(_req(ActionKind.READ_ACCOUNT, {"account_id": "ACC-1001"}))
        assert v.status == "executed"
        assert v.gate_blocked_at is None
        assert v.execution_result["account"]["account_id"] == "ACC-1001"
        assert v.audit_ref  # 執行必有稽核參照

    def test_refund_full_approval_loop(self, pipeline):
        v = pipeline.evaluate(_req(ActionKind.ISSUE_REFUND,
                                   {"account_id": "ACC-1001", "amount": 5000}))
        assert v.status == "pending_approval"
        assert v.approval_id is not None
        assert v.projection.financial_delta == -5000

        approved, err = pipeline.decide_approval(
            v.approval_id, True, "MGR-001", "sim-0911-000-111", "客訴成立")
        assert err == ""
        assert approved.status == "executed"
        assert sum(pipeline.shadow.get_account("ACC-1001").refunds_30d) == 5000
        # 稽核鏈可完整回溯
        assert pipeline.audit.completeness()["audit_completeness"] == 1.0

    def test_reject_requires_reason_and_leaves_trace(self, pipeline):
        v = pipeline.evaluate(_req(ActionKind.SUSPEND_SERVICE, {"account_id": "ACC-1001"}))
        _, err = pipeline.decide_approval(v.approval_id, False, "MGR-001",
                                          "sim-0911-000-111", "")
        assert "理由" in err

        rejected, err = pipeline.decide_approval(v.approval_id, False, "MGR-001",
                                                 "sim-0911-000-111", "資訊不足")
        assert err == ""
        assert rejected.status == "rejected"
        assert rejected.gate_blocked_at == "G4"
        stages = [r.stage for r in pipeline.audit.records]
        assert "g4_rejected" in stages
        # 未執行:帳戶狀態不變
        assert pipeline.shadow.get_account("ACC-1001").service_status == "active"

    def test_wrong_credential_rejected(self, pipeline):
        v = pipeline.evaluate(_req(ActionKind.ISSUE_REFUND,
                                   {"account_id": "ACC-1001", "amount": 100}))
        verdict, err = pipeline.decide_approval(v.approval_id, True, "MGR-001",
                                                "wrong-code", "ok")
        assert verdict is None
        assert "驗證失敗" in err

    def test_double_decision_conflict(self, pipeline):
        v = pipeline.evaluate(_req(ActionKind.ISSUE_REFUND,
                                   {"account_id": "ACC-1001", "amount": 100}))
        pipeline.decide_approval(v.approval_id, True, "MGR-001", "sim-0911-000-111", "ok")
        verdict, err = pipeline.decide_approval(v.approval_id, True, "MGR-001",
                                                "sim-0911-000-111", "再一次")
        assert verdict is None and "已於" in err


class TestBlocking:
    def test_injection_blocked_at_g0(self, pipeline):
        v = pipeline.evaluate(_req(
            ActionKind.READ_BULK, {"declared_count": 500}, chain=INJECTED))
        assert v.status == "blocked"
        assert v.gate_blocked_at == "G0"
        assert any(f.rule_id == "G0-R1" for f in v.findings)

    def test_injection_low_risk_probe_passes(self, pipeline):
        # 不可信來源鏈仍可授權 low 風險 —— G0 不過度反應
        v = pipeline.evaluate(_req(
            ActionKind.READ_ACCOUNT, {"account_id": "ACC-1001"}, chain=INJECTED))
        assert v.status == "executed"

    def test_cross_subject_blocked_at_g2(self, pipeline):
        v = pipeline.evaluate(_req(ActionKind.READ_ACCOUNT, {"account_id": "ACC-1002"}))
        assert v.status == "blocked"
        assert v.gate_blocked_at == "G2"

    def test_scope_escape_blocked_at_g3(self, pipeline):
        v = pipeline.evaluate(_req(
            ActionKind.READ_BULK,
            {"filters": {"plan_id": "5G-999"}, "declared_count": 50},
            principal="OPS-1", role=PrincipalRole.OPS,
            chain=[Provenance(Channel.SYSTEM, "batch:test")]))
        assert v.status == "blocked"
        assert v.gate_blocked_at == "G3"

    def test_blocked_action_leaves_full_audit(self, pipeline):
        pipeline.evaluate(_req(
            ActionKind.READ_BULK, {"declared_count": 500}, chain=INJECTED))
        stages = [r["stage"] for r in pipeline.audit.to_list()]
        assert "g0_trust" in stages and "g5_blocked" in stages
        assert pipeline.audit.verify()["ok"]

    def test_invalid_request_blocked_at_g1(self, pipeline):
        bad = _req(ActionKind.ISSUE_REFUND, {})   # 缺 amount
        v = pipeline.evaluate(bad)
        assert v.status == "blocked"
        assert v.gate_blocked_at == "G1"


class TestAblationAndAB:
    def test_gate_disabled_executes_attack(self):
        pipeline = AgentGatePipeline(
            shadow=ShadowTelecomEnv(), config=GateConfig(gate_enabled=False))
        v = pipeline.evaluate(_req(
            ActionKind.READ_BULK, {"declared_count": 500}, chain=INJECTED))
        assert v.status == "executed"
        assert v.risk == "ungoverned"
        assert v.execution_result["exported_count"] > 500

    def test_no_g0_injection_reaches_queue(self):
        pipeline = AgentGatePipeline(
            shadow=ShadowTelecomEnv(), config=GateConfig(enable_g0=False))
        v = pipeline.evaluate(_req(
            ActionKind.REISSUE_SIM, {"account_id": "ACC-1001", "ship_to": "轉運倉"},
            chain=INJECTED))
        # 沒有 G0,注入的 SIM 補發變成例行核准案
        assert v.status == "pending_approval"

    def test_no_g3_scope_escape_not_caught(self):
        pipeline = AgentGatePipeline(
            shadow=ShadowTelecomEnv(), config=GateConfig(enable_g3=False))
        v = pipeline.evaluate(_req(
            ActionKind.READ_BULK,
            {"filters": {"plan_id": "5G-999"}, "declared_count": 50},
            principal="OPS-1", role=PrincipalRole.OPS,
            chain=[Provenance(Channel.SYSTEM, "batch:test")]))
        assert v.status == "pending_approval"   # 低報筆數騙過了申報審查
        assert v.projection is None


class TestEvidenceAndMetrics:
    def test_evidence_package_content(self, pipeline):
        v = pipeline.evaluate(_req(
            ActionKind.ISSUE_REFUND, {"account_id": "ACC-1001", "amount": 5000},
            reasoning="用戶反映重複扣款。"))
        pending = pipeline.queue.get(v.approval_id)
        package = pending.evidence_package()
        assert package["risk"] == "medium"
        assert package["projection"]["reversible"] is True
        assert package["provenance"]["risk_cap"] == "high"
        assert "未經驗證" in package["agent_reasoning"]["warning"]
        assert any(f["rule_id"] == "AG-20" for f in package["findings"])

    def test_metrics_accumulate(self, pipeline):
        pipeline.evaluate(_req(ActionKind.READ_ACCOUNT, {"account_id": "ACC-1001"}))
        pipeline.evaluate(_req(ActionKind.READ_BULK, {"declared_count": 500},
                               chain=INJECTED))
        summary = pipeline.metrics.summary()
        assert summary["decisions"] == 2
        assert summary["status_counts"]["executed"] == 1
        assert summary["blocked_by_gate"]["G0"] == 1
        assert summary["decision_latency_ms"]["p95"] >= 0

    def test_simulate_does_not_enqueue(self, pipeline):
        result = pipeline.simulate(_req(
            ActionKind.ISSUE_REFUND, {"account_id": "ACC-1001", "amount": 5000}))
        assert result["ok"]
        assert result["projection"]["financial_delta"] == -5000
        assert pipeline.queue.pending() == []
