"""測試集交叉檢核:120 條情境逐條對照預期裁決(規格 §5.1)。

這個測試就是「每條情境需標註預期裁決,並交叉檢核」的執行體:
情境的 expected_status / expected_gate 是先寫的標註,
本測試用完整管線逐條驗證標註與實作一致。
"""

from __future__ import annotations

import pytest

from agentgate.pipeline import AgentGatePipeline, GateConfig
from agentgate.scenarios import build_scenarios, scenario_stats
from agentgate.shadow import ShadowTelecomEnv
from agentgate.validation import attentive_approver

SCENARIOS = build_scenarios()


class TestScenarioSetShape:
    def test_counts_match_spec(self):
        stats = scenario_stats(SCENARIOS)
        assert stats["total"] == 120
        assert stats["normal"] == 80
        assert stats["attack"] == 40
        assert stats["attack_types"] == {
            "injection": 20, "confused_deputy": 10, "scope_escape": 10}

    def test_ids_unique(self):
        ids = [s.scenario_id for s in SCENARIOS]
        assert len(ids) == len(set(ids))

    def test_harmless_probes_not_counted_harmful(self):
        probes = [s for s in SCENARIOS if s.scenario_id in ("a-inj-19", "a-inj-20")]
        assert all(not s.harmful for s in probes)


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
