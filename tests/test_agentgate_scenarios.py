"""測試集交叉檢核:142 條情境逐條對照預期裁決(規格 §5.1)。

這個測試就是「每條情境需標註預期裁決,並交叉檢核」的執行體:
情境的 expected_status / expected_gate 是先寫的標註,
本測試用完整管線逐條驗證標註與實作一致。
"""

from __future__ import annotations

import pytest

from agentgate.ontology import Channel
from agentgate.pipeline import AgentGatePipeline, GateConfig
from agentgate.scenarios import build_scenarios, scenario_stats
from agentgate.shadow import ShadowTelecomEnv
from agentgate.validation import attentive_approver

SCENARIOS = build_scenarios()


class TestScenarioSetShape:
    def test_counts_match_spec(self):
        stats = scenario_stats(SCENARIOS)
        assert stats["total"] == 142
        assert stats["normal"] == 93
        assert stats["attack"] == 49
        assert stats["attack_types"] == {
            "injection": 24, "confused_deputy": 14, "scope_escape": 11}

    def test_ids_unique(self):
        ids = [s.scenario_id for s in SCENARIOS]
        assert len(ids) == len(set(ids))

    def test_harmless_probes_not_counted_harmful(self):
        probes = [s for s in SCENARIOS if s.scenario_id in ("a-inj-19", "a-inj-20")]
        assert all(not s.harmful for s in probes)

    def test_every_channel_has_scenarios(self):
        """五個通道都要有情境 —— 零情境的通道等於死程式碼。"""
        coverage = scenario_stats(SCENARIOS)["channel_coverage"]
        assert set(coverage) == {ch.value for ch in Channel}
        assert all(count > 0 for count in coverage.values()), coverage
        assert coverage["memory"] >= 4
        assert coverage["user_unverified"] >= 4

    def test_attachment_is_not_a_proxy_for_attack(self):
        """去偏的核心宣稱:帶附件的情境裡有足量的**正常業務**。

        若這個數字是 0,G0 就只是在做「有沒有附件」的二元分類,
        整條來源信任的論證都會垮掉。
        """
        stats = scenario_stats(SCENARIOS)
        assert stats["with_attachment_normal"] >= 12
        assert stats["with_attachment_normal"] / stats["with_attachment"] > 0.3

    def test_confirmed_normals_are_expected_to_pass(self):
        """12 條「附件 + 明確確認」的正常情境,標註上必須是放行。"""
        conf = [s for s in SCENARIOS if s.scenario_id.startswith("n-conf-")]
        assert len(conf) == 12
        assert all(s.expected_status == "executed" for s in conf)
        assert all(s.confirm_ref and s.confirms for s in conf)

    def test_injection_scenarios_have_no_confirmation(self):
        """a-inj 情境不得帶確認節點 —— 用戶從來沒有確認過那些動作。"""
        inj = [s for s in SCENARIOS if s.scenario_id.startswith("a-inj-")]
        assert inj and all(not s.confirm_ref for s in inj)


class TestScenarioCrossCheck:
    """核心交叉檢核:B3(審慎核准者)下每條情境都必須得到標註的裁決。"""

    @pytest.fixture(scope="class")
    def outcomes(self) -> dict[str, tuple[str, str | None]]:
        pipeline = AgentGatePipeline(
            shadow=ShadowTelecomEnv(),
            config=GateConfig(approval_policy=attentive_approver),
        )
        result = {}
        for scenario in SCENARIOS:
            verdict = pipeline.evaluate(scenario.build_request())
            result[scenario.scenario_id] = (verdict.status, verdict.gate_blocked_at)
        return result

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
    def test_expected_verdict(self, scenario, outcomes):
        status, gate = outcomes[scenario.scenario_id]
        assert status == scenario.expected_status, (
            f"{scenario.scenario_id}: 預期 {scenario.expected_status},實得 {status}")
        assert gate == scenario.expected_gate, (
            f"{scenario.scenario_id}: 預期攔截於 {scenario.expected_gate},實得 {gate}")
