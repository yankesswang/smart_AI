"""AgentGate 核心單元測試:本體論、G0/G1/G2/G3/G5 各關卡。"""

from __future__ import annotations

import json

import pytest

from agentgate.gates.g0_provenance import evaluate_trust, trust_evidence
from agentgate.gates.g1_resolution import (
    attach_runtime_provenance,
    resolve_action,
    validate_request,
)
from agentgate.gates.g2_policy import ACTION_POLICY, PolicyEngine
from agentgate.gates.g3_projection import project_consequences
from agentgate.gates.g5_audit import ANCHOR_KEY_ENV, AuditChain, GENESIS_HASH
from agentgate.ontology import (
    ActionKind,
    ActionRequest,
    Channel,
    PrincipalRole,
    Provenance,
    action_fingerprint,
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


# --------------------------------------------------------------------------- G0-R2
class TestG0ConfirmationLifting:
    """G0-R2 確認提升:每一條規則都有一個具名測試守著。"""

    REFUND = (ActionKind.ISSUE_REFUND, {"account_id": "ACC-1001", "amount": 5000})

    def _chain(self, **confirm) -> list[Provenance]:
        chain = [
            Provenance(Channel.USER_VERIFIED, "chat:s-1"),
            Provenance(Channel.TOOL_OUTPUT, "upload:bill.png"),
        ]
        if confirm:
            chain.append(Provenance(**confirm))
        return chain

    def _fp(self) -> str:
        kind, params = self.REFUND
        return action_fingerprint(kind, params)

    def test_attachment_without_confirmation_stays_low(self):
        """用戶轉述 PDF 內容 ≠ 確認:沒有確認節點,tool_output 仍是 low。"""
        trust = evaluate_trust(self._chain(), self._fp())
        assert trust.risk_cap == "low"
        assert trust.lifts == []

    def test_matching_confirmation_lifts_to_high(self):
        trust = evaluate_trust(
            self._chain(channel=Channel.USER_VERIFIED, source_ref="chat:confirm-1",
                        confirms="upload:bill.png", confirmed_action_hash=self._fp()),
            self._fp())
        assert trust.risk_cap == "high"
        assert trust.lifts[0]["source_ref"] == "upload:bill.png"
        assert trust.lifts[0]["to_cap"] == "high"

    def test_confirmation_of_a_different_action_does_not_lift(self):
        """確認了退費 880,不能拿來授權退費 5,000 —— 指紋涵蓋全部參數。"""
        other = action_fingerprint(ActionKind.ISSUE_REFUND,
                                   {"account_id": "ACC-1001", "amount": 880})
        trust = evaluate_trust(
            self._chain(channel=Channel.USER_VERIFIED, source_ref="chat:confirm-1",
                        confirms="upload:bill.png", confirmed_action_hash=other),
            self._fp())
        assert trust.risk_cap == "low"
        assert trust.stale_confirmations and not trust.lifts

    def test_changed_param_breaks_the_fingerprint(self):
        """用戶確認「退回本人帳戶」,Agent 卻改成 refund_to=ACC-9999 → 提升不成立。"""
        confirmed = action_fingerprint(*self.REFUND)
        actual = action_fingerprint(ActionKind.ISSUE_REFUND,
                                    {**self.REFUND[1], "refund_to": "ACC-9999"})
        assert confirmed != actual
        trust = evaluate_trust(
            self._chain(channel=Channel.USER_VERIFIED, source_ref="chat:confirm-1",
                        confirms="upload:bill.png", confirmed_action_hash=confirmed),
            actual)
        assert trust.risk_cap == "low"

    def test_confirmation_must_name_the_source_it_lifts(self):
        """確認節點的 confirms 指向別的來源,就提升不了這一個。"""
        trust = evaluate_trust(
            self._chain(channel=Channel.USER_VERIFIED, source_ref="chat:confirm-1",
                        confirms="upload:other.pdf", confirmed_action_hash=self._fp()),
            self._fp())
        assert trust.risk_cap == "low"

    def test_untrusted_node_cannot_confirm_itself(self):
        """自我確認無效 —— 一份文件不能替自己背書。"""
        chain = [
            Provenance(Channel.USER_VERIFIED, "chat:s-1"),
            Provenance(Channel.TOOL_OUTPUT, "upload:bill.png",
                       confirms="upload:bill.png", confirmed_action_hash=self._fp()),
        ]
        assert evaluate_trust(chain, self._fp()).risk_cap == "low"

    def test_tool_output_cannot_confirm_another_tool_output(self):
        chain = [
            Provenance(Channel.USER_VERIFIED, "chat:s-1"),
            Provenance(Channel.TOOL_OUTPUT, "upload:bill.png"),
            Provenance(Channel.TOOL_OUTPUT, "upload:cover_letter.pdf",
                       confirms="upload:bill.png", confirmed_action_hash=self._fp()),
        ]
        assert evaluate_trust(chain, self._fp()).risk_cap == "low"

    def test_memory_is_not_liftable(self):
        """記憶是 Agent 自己寫的,確認它等於自己給自己背書。"""
        chain = [
            Provenance(Channel.MEMORY, "memory:thread-1"),
            Provenance(Channel.USER_VERIFIED, "chat:confirm-1",
                       confirms="memory:thread-1", confirmed_action_hash=self._fp()),
        ]
        assert evaluate_trust(chain, self._fp()).risk_cap == "medium"

    def test_no_fingerprint_means_no_lift(self):
        """runtime 沒給指紋 → fail-closed,一律不提升。"""
        trust = evaluate_trust(
            self._chain(channel=Channel.USER_VERIFIED, source_ref="chat:confirm-1",
                        confirms="upload:bill.png", confirmed_action_hash=self._fp()),
            "")
        assert trust.risk_cap == "low"

    def test_lift_shows_up_in_evidence(self):
        trust = evaluate_trust(
            self._chain(channel=Channel.USER_VERIFIED, source_ref="chat:confirm-1",
                        confirms="upload:bill.png", confirmed_action_hash=self._fp()),
            self._fp())
        statements = [e.statement for e in trust_evidence(trust)]
        assert any("確認提升" in st for st in statements)


# --------------------------------------------------------------------------- G1 契約
class TestRuntimeProvenanceContract:
    """來源鏈由 runtime 注入:被注入的 Agent 無法靠謊報來源鏈洗白。"""

    HARNESS = {
        "instruction_sources": [{"channel": "user_verified", "source_ref": "chat:s-1"}],
        "tool_outputs": ["upload:evil.pdf#p3"],
    }

    def _payload(self, chain):
        return {"kind": "read_bulk", "params": {"declared_count": 50},
                "principal": "OPS-1", "principal_role": "ops", "agent_id": "a1",
                "provenance_chain": chain}

    def test_omitted_tool_output_is_added_back(self):
        """Agent 少報了那份 PDF,runtime 照樣把它補進鏈裡。"""
        payload, findings = attach_runtime_provenance(
            self._payload([{"channel": "user_verified", "source_ref": "chat:s-1"}]),
            self.HARNESS)
        refs = {n["source_ref"]: n["channel"] for n in payload["provenance_chain"]}
        assert refs["upload:evil.pdf#p3"] == "tool_output"
        assert any(f.rule_id == "G1-R2" for f in findings)

    def test_claimed_upgrade_is_downgraded_to_runtime_record(self):
        """Agent 宣稱那份 PDF 是 user_verified —— 以 runtime 為準,降回 tool_output。"""
        payload, findings = attach_runtime_provenance(
            self._payload([{"channel": "user_verified", "source_ref": "upload:evil.pdf#p3"}]),
            self.HARNESS)
        refs = {n["source_ref"]: n["channel"] for n in payload["provenance_chain"]}
        assert refs["upload:evil.pdf#p3"] == "tool_output"
        assert any("runtime" in e.statement for f in findings for e in f.evidence)

    def test_unvouched_source_is_downgraded(self):
        """runtime 沒有紀錄的來源,不論 Agent 怎麼宣稱都降為 user_unverified。"""
        payload, _ = attach_runtime_provenance(
            self._payload([{"channel": "system", "source_ref": "sys:i-made-this-up"}]),
            self.HARNESS)
        refs = {n["source_ref"]: n["channel"] for n in payload["provenance_chain"]}
        assert refs["sys:i-made-this-up"] == "user_unverified"

    def test_narrowing_is_allowed(self):
        """自報比 runtime 更不可信 → 尊重它(縮小信任永遠安全)。"""
        payload, _ = attach_runtime_provenance(
            self._payload([{"channel": "tool_output", "source_ref": "chat:s-1"}]),
            self.HARNESS)
        refs = {n["source_ref"]: n["channel"] for n in payload["provenance_chain"]}
        assert refs["chat:s-1"] == "tool_output"

    def test_self_reported_confirmation_is_stripped(self):
        """核心:Agent 自報 confirms + 指紋,想把自己洗成已確認 —— 一律清空。"""
        fake = action_fingerprint(ActionKind.READ_BULK, {"declared_count": 50})
        request, errors = resolve_action(self._payload([
            {"channel": "user_verified", "source_ref": "chat:s-1"},
            {"channel": "tool_output", "source_ref": "upload:evil.pdf#p3"},
            {"channel": "user_verified", "source_ref": "chat:i-say-they-confirmed",
             "confirms": "upload:evil.pdf#p3", "confirmed_action_hash": fake},
        ]))
        assert errors == []
        assert all(not n.confirmed_action_hash for n in request.provenance_chain)
        assert any(f.rule_id == "G1-R3" for f in request.runtime_findings)
        assert evaluate_trust(request.provenance_chain, request.fingerprint()).risk_cap == "low"

    def test_self_reported_acknowledgements_are_stripped(self):
        payload = self._payload([{"channel": "user_verified", "source_ref": "chat:s-1"}])
        payload["acknowledgements"] = {"early_termination_fee": True}
        request, _ = resolve_action(payload)
        assert request.acknowledgements == {}

    def test_runtime_attested_confirmation_survives(self):
        """走 harness 契約進來的確認節點,指紋由 runtime 算,保留。"""
        harness = {
            **self.HARNESS,
            "confirmations": [{
                "source_ref": "chat:confirm-1", "confirms": "upload:evil.pdf#p3",
                "action": {"kind": "read_bulk", "params": {"declared_count": 50}},
            }],
        }
        payload, _ = attach_runtime_provenance(
            self._payload([{"channel": "user_verified", "source_ref": "chat:s-1"}]), harness)
        request, errors = resolve_action(payload)
        assert errors == []
        confirms = [n for n in request.provenance_chain if n.confirmed_action_hash]
        assert len(confirms) == 1
        assert confirms[0].confirmed_action_hash == request.fingerprint()


# --------------------------------------------------------------------------- 政策版本
class TestPolicyVersioning:
    def test_version_is_deterministic(self):
        assert PolicyEngine().version() == PolicyEngine().version()

    def test_changing_a_limit_changes_the_version(self):
        base = PolicyEngine().version()["policy_version"]
        tweaked = PolicyEngine(limits={"refund_single_limit": 5_000})
        assert tweaked.version()["policy_version"] != base

    def test_statute_text_follows_the_active_limit(self):
        """限額被覆寫,條文原文要跟著變 —— 否則稽核紀錄自相矛盾。"""
        engine = PolicyEngine(limits={"refund_single_limit": 5_000})
        statute = next(r["statute"] for r in engine.describe()["rules"]
                       if r["rule_id"] == "AG-21")
        assert "5,000" in statute

    def test_overridden_limit_actually_binds(self, shadow):
        engine = PolicyEngine(limits={"refund_single_limit": 1_000})
        d = engine.adjudicate(
            _req(ActionKind.ISSUE_REFUND, {"account_id": "ACC-1001", "amount": 2_000}),
            shadow)
        assert any(f.rule_id == "AG-21" for f in d.findings)
        assert "1,000" in next(f.statute for f in d.findings if f.rule_id == "AG-21")

    def test_yaml_override_file_hash_enters_version(self, tmp_path):
        path = tmp_path / "policy.yaml"
        path.write_text("limits:\n  refund_single_limit: 3000\n", encoding="utf-8")
        engine = PolicyEngine(overrides_path=path)
        version = engine.version()
        assert engine.limits["refund_single_limit"] == 3_000
        assert version["overrides"]["active"] is True
        assert version["components"]["overrides_file"]
        assert version["policy_version"] != PolicyEngine().version()["policy_version"]

    def test_json_override_file(self, tmp_path):
        path = tmp_path / "policy.json"
        path.write_text('{"limits": {"bulk_forbidden_count": 20}}', encoding="utf-8")
        assert PolicyEngine(overrides_path=path).limits["bulk_forbidden_count"] == 20

    def test_unknown_limit_key_rejected(self, tmp_path):
        path = tmp_path / "policy.json"
        path.write_text('{"limits": {"allow_everything": 1}}', encoding="utf-8")
        with pytest.raises(ValueError):
            PolicyEngine(overrides_path=path)

    def test_decision_carries_policy_version(self, shadow):
        engine = PolicyEngine()
        d = engine.adjudicate(_req(ActionKind.READ_ACCOUNT, {"account_id": "ACC-1001"}),
                              shadow)
        assert d.policy_version == engine.version()["policy_version"]


# --------------------------------------------------------------------------- G3 連鎖
class TestG3Cascade:
    def test_refund_within_deposit_has_no_cascade(self, shadow):
        proj, findings = project_consequences(
            _req(ActionKind.ISSUE_REFUND, {"account_id": "ACC-1001", "amount": 5_000}),
            shadow)
        assert proj.cascade == []
        assert not any(f.rule_id == "AG-32" for f in findings)

    def test_refund_beyond_deposit_projects_service_interruption(self, shadow):
        # ACC-1004 預繳餘額 500 元
        proj, findings = project_consequences(
            _req(ActionKind.ISSUE_REFUND, {"account_id": "ACC-1004", "amount": 9_000},
                 principal="ACC-1004"), shadow)
        assert [c["process"] for c in proj.cascade] == [
            "negative_balance", "dunning", "auto_suspension"]
        assert any(c["service_interruption"] for c in proj.cascade)
        ag32 = next(f for f in findings if f.rule_id == "AG-32")
        assert ag32.escalate_to == "high"

    def test_cascade_does_not_mutate_shadow(self, shadow):
        before = shadow.get_account("ACC-1004").deposit_balance
        project_consequences(
            _req(ActionKind.ISSUE_REFUND, {"account_id": "ACC-1004", "amount": 9_000},
                 principal="ACC-1004"), shadow)
        assert shadow.get_account("ACC-1004").deposit_balance == before


# --------------------------------------------------------------------------- G5 錨定
class TestG5Anchoring:
    def _chain_with(self, n: int, **kw) -> AuditChain:
        chain = AuditChain(**kw)
        for i in range(n):
            chain.append(trace_id="t", action_id=f"a{i}",
                         stage="g2_adjudication", actor="x", seqno=i)
        return chain

    def test_anchor_records_head_and_length(self):
        chain = self._chain_with(3)
        anchor = chain.anchor()
        assert anchor["chain_length"] == 3
        assert anchor["head_hash"] == chain.records[-1].hash
        assert anchor["demo_key"] is True          # 未設環境變數 → 標示為 demo 金鑰
        assert chain.verify()["ok"]

    def test_mid_chain_tamper_detected_by_chain_itself(self):
        chain = self._chain_with(4)
        chain.anchor()
        chain.tamper_for_demo(1, {"note": "被偷改"})
        verdict = chain.verify()
        assert not verdict["ok"] and verdict["broken_at_seq"] == 1

    def test_full_rewrite_defeats_chain_but_not_anchor(self):
        """管理員改一筆再把整條鏈重算 —— 鏈內驗證會通過,只有錨點抓得到。"""
        chain = self._chain_with(5)
        chain.anchor()
        assert chain.rewrite_for_demo(2, {"seqno": 999})
        # 鏈內自洽:每一筆的雜湊都對得上自己的內容與前一筆
        prev = "0" * 64
        for record in chain.records:
            assert record.prev_hash == prev
            assert record.compute_hash() == record.hash
            prev = record.hash
        verdict = chain.verify()
        assert not verdict["ok"]
        assert "整條重寫" in verdict["reason"]
        assert verdict["anchor_seq"] == 0

    def test_forged_anchor_signature_detected(self):
        chain = self._chain_with(3)
        chain.anchor()
        chain.anchors[0]["head_hash"] = "f" * 64      # 想讓錨點對上被改過的鏈
        verdict = chain.verify()
        assert not verdict["ok"] and "簽章" in verdict["reason"]

    def test_deleted_records_detected(self):
        chain = self._chain_with(4)
        chain.anchor()
        del chain.records[-1]
        verdict = chain.verify()
        assert not verdict["ok"] and "刪除" in verdict["reason"]

    def test_anchor_file_is_append_only(self, tmp_path):
        chain = self._chain_with(2, persist_dir=tmp_path)
        chain.anchor()
        chain.append(trace_id="t", action_id="a9", stage="g5_executed", actor="x")
        chain.anchor()
        lines = chain.anchor_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["anchor_seq"] == 0
        assert json.loads(lines[1])["chain_length"] == 3

    def test_env_key_is_not_flagged_as_demo(self, monkeypatch):
        monkeypatch.setenv(ANCHOR_KEY_ENV, "a-real-secret")
        chain = self._chain_with(2)
        assert chain.anchor()["demo_key"] is False
        assert chain.verify()["ok"]
