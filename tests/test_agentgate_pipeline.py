"""AgentGate 管線端到端測試:五關卡編排、核准流程、A/B 對照、稽核完整性。"""

from __future__ import annotations

import pytest

from agentgate.gates.g1_resolution import attach_runtime_provenance, resolve_action
from agentgate.ontology import (
    ActionKind,
    ActionRequest,
    Channel,
    PrincipalRole,
    Provenance,
    action_fingerprint,
)
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


# --------------------------------------------------------------------------------------
CONFIRMED_DOC = "upload:bill_shot.png"


def _confirmed_chain(kind: ActionKind, params: dict) -> list[Provenance]:
    """真實流程:用戶上傳截圖 → runtime 把具體動作呈現給用戶 → 用戶確認。"""
    return [
        Provenance(Channel.USER_VERIFIED, "chat:test"),
        Provenance(Channel.TOOL_OUTPUT, CONFIRMED_DOC),
        Provenance(Channel.USER_VERIFIED, "chat:confirm-1",
                   confirms=CONFIRMED_DOC,
                   confirmed_action_hash=action_fingerprint(kind, params)),
    ]


class TestConfirmationLiftingEndToEnd:
    """G0-R2 在完整管線上的行為。這一組是「G0 不是在數附件」的端到端證據。"""

    def test_attachment_plus_confirmation_reaches_approval(self, pipeline):
        params = {"account_id": "ACC-1001", "amount": 5000}
        v = pipeline.evaluate(_req(ActionKind.ISSUE_REFUND, params,
                                   chain=_confirmed_chain(ActionKind.ISSUE_REFUND, params)))
        assert v.status == "pending_approval"
        assert v.trust.risk_cap == "high"
        assert v.trust.lifts and v.trust.lifts[0]["source_ref"] == CONFIRMED_DOC

    def test_same_action_without_confirmation_is_blocked(self, pipeline):
        """對照組:一模一樣的動作,只是沒有確認節點 → G0 攔下。"""
        v = pipeline.evaluate(_req(
            ActionKind.ISSUE_REFUND, {"account_id": "ACC-1001", "amount": 5000},
            chain=INJECTED))
        assert v.status == "blocked" and v.gate_blocked_at == "G0"

    def test_injected_bulk_export_still_blocked_even_with_a_confirmation(self, pipeline):
        """Demo 的 a-inj:用戶確認的是退費,PDF 要的是匯出 500 筆 —— 指紋不符,照擋。"""
        confirmed_other = action_fingerprint(
            ActionKind.ISSUE_REFUND, {"account_id": "ACC-1001", "amount": 880})
        chain = [
            Provenance(Channel.USER_VERIFIED, "chat:test"),
            Provenance(Channel.TOOL_OUTPUT, "upload:invoice.pdf#p3"),
            Provenance(Channel.USER_VERIFIED, "chat:confirm-1",
                       confirms="upload:invoice.pdf#p3",
                       confirmed_action_hash=confirmed_other),
        ]
        v = pipeline.evaluate(_req(
            ActionKind.READ_BULK, {"declared_count": 500}, chain=chain))
        assert v.status == "blocked" and v.gate_blocked_at == "G0"
        assert v.trust.stale_confirmations

    def test_lift_is_written_to_the_audit_chain(self, pipeline):
        params = {"account_id": "ACC-1001", "amount": 5000}
        pipeline.evaluate(_req(ActionKind.ISSUE_REFUND, params,
                               chain=_confirmed_chain(ActionKind.ISSUE_REFUND, params)))
        g0 = next(r for r in pipeline.audit.to_list() if r["stage"] == "g0_trust")
        assert g0["detail"]["lifts"]
        assert g0["detail"]["action_fingerprint"]


class TestCascadeAndG3Escalation:
    """AG-32:預演顯示會連鎖斷話 → 升級為 high 並需核准。"""

    def test_cascade_escalates_and_requires_approval(self, pipeline):
        v = pipeline.evaluate(_req(
            ActionKind.ISSUE_REFUND, {"account_id": "ACC-1004", "amount": 9000},
            principal="ACC-1004"))
        assert v.status == "pending_approval"
        assert v.risk == "high"                       # 由 G3 的 AG-32 抬上來
        assert any(f.rule_id == "AG-32" for f in v.findings)
        assert any(c["service_interruption"] for c in v.projection.cascade)

    def test_g3_escalation_re_checks_g0(self, pipeline):
        """G2 判 medium 通過來源鏈上限,G3 抬到 high 之後 G0-R1 必須重算一次。

        指令來自記憶(上限 medium):退費的基礎風險 medium 剛好過得了 G0-R1,
        但 G3 預演發現它會連鎖斷話而抬到 high —— 若不重算,這個洞就從
        「medium 動作」的縫裡鑽過去了。
        """
        v = pipeline.evaluate(_req(
            ActionKind.ISSUE_REFUND, {"account_id": "ACC-1004", "amount": 9000},
            principal="ACC-1004",
            chain=[Provenance(Channel.MEMORY, "memory:thread-7")]))
        assert v.status == "blocked" and v.gate_blocked_at == "G0"
        assert v.risk == "high"
        assert any(f.rule_id == "AG-32" for f in v.findings)

    def test_memory_sourced_refund_without_cascade_passes_g0(self, pipeline):
        """對照組:同一條記憶來源鏈,沒有連鎖後果就停在 medium,G0 放行。"""
        v = pipeline.evaluate(_req(
            ActionKind.ISSUE_REFUND, {"account_id": "ACC-1001", "amount": 5000},
            chain=[Provenance(Channel.MEMORY, "memory:thread-7")]))
        assert v.status == "pending_approval" and v.risk == "medium"

    def test_no_cascade_when_deposit_covers_it(self, pipeline):
        v = pipeline.evaluate(_req(
            ActionKind.ISSUE_REFUND, {"account_id": "ACC-1001", "amount": 5000}))
        assert v.risk == "medium"
        assert v.projection.cascade == []


class TestPolicyVersionInAudit:
    def test_adjudication_record_carries_policy_version(self, pipeline):
        pipeline.evaluate(_req(ActionKind.READ_ACCOUNT, {"account_id": "ACC-1001"}))
        record = next(r for r in pipeline.audit.to_list()
                      if r["stage"] == "g2_adjudication")
        assert record["detail"]["policy_version"] == \
            pipeline.engine.version()["policy_version"]


class TestRuntimeProvenanceReachesTheVerdict:
    def test_self_reported_chain_cannot_launder_an_injected_action(self):
        """被注入的 Agent 少報 tool_output、還宣稱它是 system —— 兩招都失效。"""
        harness = {
            "instruction_sources": [{"channel": "user_verified", "source_ref": "chat:s-1"}],
            "tool_outputs": ["upload:evil.pdf#p3"],
        }
        payload, findings = attach_runtime_provenance({
            "kind": "reissue_sim",
            "params": {"account_id": "ACC-1001", "ship_to": "轉運倉"},
            "principal": "ACC-1001", "agent_id": "a1",
            "provenance_chain": [
                {"channel": "system", "source_ref": "chat:s-1"},
                {"channel": "system", "source_ref": "sys:trusted-i-promise"},
            ],
        }, harness)
        payload["_runtime_findings"] = findings
        request, errors = resolve_action(payload)
        assert errors == []
        pipeline = AgentGatePipeline(shadow=ShadowTelecomEnv())
        v = pipeline.evaluate(request)
        assert v.status == "blocked" and v.gate_blocked_at == "G0"
        assert any(f.rule_id == "G1-R2" for f in v.findings)
        # 修正紀錄也要進稽核鏈(這一筆在 G0 就被擋下,所以落在 g5_blocked)
        record = next(r for r in pipeline.audit.to_list()
                      if r["stage"] == "g5_blocked")
        assert "G1-R2" in record["detail"]["rules_triggered"]
