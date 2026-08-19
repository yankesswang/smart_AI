"""AgentGate 核心單元測試:本體論、G0/G1/G2/G3/G5 各關卡。"""

from __future__ import annotations

import pytest

from agentgate.gates.g0_provenance import evaluate_trust
from agentgate.gates.g1_resolution import resolve_action, validate_request
from agentgate.gates.g2_policy import ACTION_POLICY, PolicyEngine
from agentgate.gates.g3_projection import project_consequences
from agentgate.gates.g5_audit import AuditChain, GENESIS_HASH
from agentgate.ontology import (
    ActionKind,
    ActionRequest,
    Channel,
    PrincipalRole,
    Provenance,
    risk_exceeds,
    risk_max,
)
from agentgate.shadow import (
    BULK_FORBIDDEN_COUNT,
    REFUND_MONTHLY_LIMIT,
    REFUND_SINGLE_LIMIT,
    ShadowTelecomEnv,
)


@pytest.fixture
def shadow() -> ShadowTelecomEnv:
    return ShadowTelecomEnv()


def _req(kind: ActionKind, params: dict, principal: str = "ACC-1001",
         role: PrincipalRole = PrincipalRole.CUSTOMER,
         chain: list[Provenance] | None = None) -> ActionRequest:
    return ActionRequest(
        kind=kind, params=params, principal=principal, principal_role=role,
        agent_id="test-agent",
        provenance_chain=chain if chain is not None
        else [Provenance(Channel.USER_VERIFIED, "chat:test")],
    )


# --------------------------------------------------------------------------- 風險序
class TestRiskOrder:
    def test_risk_max(self):
        assert risk_max("low", "high") == "high"
        assert risk_max("medium", "forbidden") == "forbidden"

    def test_risk_exceeds(self):
        assert risk_exceeds("high", "low")
        assert not risk_exceeds("low", "low")
        assert risk_exceeds("forbidden", "high")


# --------------------------------------------------------------------------- G0
class TestG0Provenance:
    def test_verified_chain_caps_high(self):
        trust = evaluate_trust([Provenance(Channel.USER_VERIFIED, "chat:1")])
        assert trust.risk_cap == "high"
        assert trust.min_trust == "verified"

    def test_tool_output_is_weakest_link(self):
        trust = evaluate_trust([
            Provenance(Channel.USER_VERIFIED, "chat:1"),
            Provenance(Channel.TOOL_OUTPUT, "upload:evil.pdf"),
        ])
        assert trust.risk_cap == "low"
        assert trust.min_trust == "untrusted"
        assert trust.weakest_link.source_ref == "upload:evil.pdf"

    def test_empty_chain_is_untrusted(self):
        trust = evaluate_trust([])
        assert trust.risk_cap == "low"
        assert trust.weakest_link is None

    def test_memory_caps_medium(self):
        trust = evaluate_trust([Provenance(Channel.MEMORY, "mem:conv-3")])
        assert trust.risk_cap == "medium"


# --------------------------------------------------------------------------- G1
class TestG1Resolution:
    def test_resolve_valid_payload(self):
        request, errors = resolve_action({
            "kind": "issue_refund",
            "params": {"account_id": "ACC-1001", "amount": 500},
            "principal": "ACC-1001", "agent_id": "a1",
            "provenance_chain": [{"channel": "user_verified", "source_ref": "chat:1"}],
        })
        assert errors == []
        assert request.kind is ActionKind.ISSUE_REFUND

    def test_unknown_kind_rejected(self):
        request, errors = resolve_action({"kind": "drop_database"})
        assert request is None
        assert "未知動作種類" in errors[0]

    def test_refund_requires_amount(self):
        request, errors = resolve_action({
            "kind": "issue_refund", "params": {},
            "principal": "ACC-1001", "agent_id": "a1", "provenance_chain": [],
        })
        assert request is None
        assert any("amount" in e for e in errors)

    def test_negative_amount_rejected(self):
        req = _req(ActionKind.ISSUE_REFUND, {"amount": -100})
        assert any("正數" in e for e in validate_request(req))

    def test_malformed_channel_rejected(self):
        request, errors = resolve_action({
            "kind": "read_account", "params": {},
            "principal": "ACC-1001", "agent_id": "a1",
            "provenance_chain": [{"channel": "carrier_pigeon", "source_ref": "x"}],
        })
        assert request is None


# --------------------------------------------------------------------------- G2
class TestG2Policy:
    def setup_method(self):
        self.engine = PolicyEngine()

    def test_read_self_is_low_auto(self, shadow):
        d = self.engine.adjudicate(_req(ActionKind.READ_ACCOUNT, {"account_id": "ACC-1001"}), shadow)
        assert (d.allowed, d.requires_approval, d.risk) == (True, False, "low")

    def test_policy_override_forbidden(self, shadow):
        d = self.engine.adjudicate(_req(ActionKind.POLICY_OVERRIDE, {}), shadow)
        assert not d.allowed
        assert d.risk == "forbidden"

    def test_customer_cross_subject_blocked(self, shadow):
        d = self.engine.adjudicate(
            _req(ActionKind.READ_ACCOUNT, {"account_id": "ACC-1002"}), shadow)
        assert not d.allowed
        assert any(f.rule_id == "AG-01" for f in d.findings)

    def test_csr_cross_subject_escalates(self, shadow):
        d = self.engine.adjudicate(
            _req(ActionKind.READ_ACCOUNT, {"account_id": "ACC-1002"},
                 principal="CSR-1", role=PrincipalRole.CSR), shadow)
        assert d.allowed and d.requires_approval and d.risk == "high"
        assert any(f.rule_id == "AG-02" for f in d.findings)

    def test_refund_over_single_limit_escalates(self, shadow):
        d = self.engine.adjudicate(
            _req(ActionKind.ISSUE_REFUND,
                 {"account_id": "ACC-1001", "amount": REFUND_SINGLE_LIMIT + 1}), shadow)
        assert d.risk == "high"
        assert any(f.rule_id == "AG-21" for f in d.findings)

    def test_refund_cumulative_escalates(self, shadow):
        # ACC-1005 30 日內已退 15,000
        d = self.engine.adjudicate(
            _req(ActionKind.ISSUE_REFUND, {"account_id": "ACC-1005", "amount": 7000},
                 principal="ACC-1005"), shadow)
        assert d.risk == "high"
        assert any(f.rule_id == "AG-22" for f in d.findings)
        assert sum(shadow.get_account("ACC-1005").refunds_30d) + 7000 > REFUND_MONTHLY_LIMIT

    def test_refund_redirect_escalates(self, shadow):
        d = self.engine.adjudicate(
            _req(ActionKind.ISSUE_REFUND,
                 {"account_id": "ACC-1001", "amount": 100, "refund_to": "ACC-9999"}), shadow)
        assert d.risk == "high"
        assert any(f.rule_id == "AG-23" for f in d.findings)

    def test_bulk_over_limit_forbidden(self, shadow):
        d = self.engine.adjudicate(
            _req(ActionKind.READ_BULK, {"declared_count": BULK_FORBIDDEN_COUNT + 1},
                 principal="OPS-1", role=PrincipalRole.OPS), shadow)
        assert not d.allowed
        assert d.risk == "forbidden"

    def test_bulk_undeclared_forbidden(self, shadow):
        d = self.engine.adjudicate(
            _req(ActionKind.READ_BULK, {}, principal="OPS-1", role=PrincipalRole.OPS), shadow)
        assert not d.allowed

    def test_plan_change_in_contract_escalates(self, shadow):
        # ACC-1002 綁約 18 個月
        d = self.engine.adjudicate(
            _req(ActionKind.CHANGE_PLAN, {"account_id": "ACC-1002", "new_plan_id": "5G-1399"},
                 principal="ACC-1002"), shadow)
        assert d.risk == "high" and d.requires_approval
        assert any(f.rule_id == "AG-24" for f in d.findings)

    def test_plan_upgrade_no_contract_auto(self, shadow):
        # ACC-1003 無綁約 4G-599 → 升級
        d = self.engine.adjudicate(
            _req(ActionKind.CHANGE_PLAN, {"account_id": "ACC-1003", "new_plan_id": "5G-799"},
                 principal="ACC-1003"), shadow)
        assert d.allowed and not d.requires_approval and d.risk == "medium"

    def test_suspend_and_sim_always_need_approval(self, shadow):
        for kind in (ActionKind.SUSPEND_SERVICE, ActionKind.REISSUE_SIM):
            d = self.engine.adjudicate(_req(kind, {"account_id": "ACC-1001"}), shadow)
            assert d.requires_approval and d.risk == "high"

    def test_describe_exports_rules_and_policy(self):
        desc = self.engine.describe()
        assert desc["llm_involved"] is False
        assert len(desc["action_policy"]) == len(ACTION_POLICY)
        assert any(r["rule_id"] == "AG-01" for r in desc["rules"])


# --------------------------------------------------------------------------- G3
class TestG3Projection:
    def test_refund_projection_reversible(self, shadow):
        proj, findings = project_consequences(
            _req(ActionKind.ISSUE_REFUND, {"account_id": "ACC-1001", "amount": 5000}), shadow)
        assert proj.financial_delta == -5000
        assert proj.reversible and proj.reversal_window_hours == 72
        assert findings == []

    def test_bulk_actual_count_measured_not_declared(self, shadow):
        # 申報 50 筆,但過濾條件實際命中遠超過 → AG-31 攔下
        proj, findings = project_consequences(
            _req(ActionKind.READ_BULK,
                 {"filters": {"plan_id": "5G-999"}, "declared_count": 50},
                 principal="OPS-1", role=PrincipalRole.OPS), shadow)
        assert proj.affected_count > 50
        assert any(f.rule_id == "AG-31" for f in findings)

    def test_sim_reissue_irreversible_needs_approval(self, shadow):
        proj, findings = project_consequences(
            _req(ActionKind.REISSUE_SIM, {"account_id": "ACC-1001"}), shadow)
        assert not proj.reversible
        assert any(f.rule_id == "AG-30" for f in findings)

    def test_projection_does_not_mutate_shadow(self, shadow):
        before = sum(shadow.get_account("ACC-1001").refunds_30d)
        project_consequences(
            _req(ActionKind.ISSUE_REFUND, {"account_id": "ACC-1001", "amount": 5000}), shadow)
        assert sum(shadow.get_account("ACC-1001").refunds_30d) == before


# --------------------------------------------------------------------------- G5
class TestG5AuditChain:
    def test_chain_links_and_verifies(self):
        chain = AuditChain()
        r1 = chain.append(trace_id="t1", action_id="a1", stage="g2_adjudication",
                          actor="x", requires_approval=False)
        r2 = chain.append(trace_id="t1", action_id="a1", stage="g5_executed", actor="x")
        assert r1.prev_hash == GENESIS_HASH
        assert r2.prev_hash == r1.hash
        assert chain.verify()["ok"]

    def test_tamper_detected(self):
        chain = AuditChain()
        chain.append(trace_id="t1", action_id="a1", stage="g2_adjudication", actor="x")
        chain.append(trace_id="t1", action_id="a1", stage="g5_executed", actor="x")
        assert chain.tamper_for_demo(0, {"note": "被偷改"})
        verdict = chain.verify()
        assert not verdict["ok"]
        assert verdict["broken_at_seq"] == 0

    def test_completeness_flags_untraceable_execution(self):
        chain = AuditChain()
        # 正常:裁決 → 執行
        chain.append(trace_id="t1", action_id="a1", stage="g2_adjudication",
                     actor="x", requires_approval=False)
        chain.append(trace_id="t1", action_id="a1", stage="g5_executed", actor="x")
        # 缺口:需核准卻沒有核准紀錄就執行
        chain.append(trace_id="t2", action_id="a2", stage="g2_adjudication",
                     actor="x", requires_approval=True)
        chain.append(trace_id="t2", action_id="a2", stage="g5_executed", actor="x")
        report = chain.completeness()
        assert report["executed"] == 2
        assert report["traceable"] == 1
        assert report["gaps"] == ["a2"]

    def test_persist_jsonl(self, tmp_path):
        chain = AuditChain(persist_dir=tmp_path)
        chain.append(trace_id="t", action_id="a", stage="g0_trust", actor="x")
        lines = chain.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
