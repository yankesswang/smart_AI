"""真 LLM 的 B1 自我審查 baseline 測試(規格 §5.3)。

守三件事:

1. **不打網路。** 全部用 monkeypatch 的假 client;沒有金鑰又沒有 client 時
   ``run_llm_b1`` 必須直接拋錯,而不是偷偷生一張假的 B1 表 —— 那正是原本
   被詬病的地方。
2. **快取是唯一的離線來源。** 測試與離線 benchmark 讀 ``runs/agentgate_b1_llm.json``,
   不重打 API;情境數被擴充時 ``coverage()`` 要看得出快取過期。
3. **指標與一致性算得對。** HAR/TCR/FBR 的分母分子、以及「同一條情境跑三次
   判定不一致」的比例 —— 後者就是規格 §4.3「可重現」那條理由的直接證據。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agentgate.agent import AgentSettings
from agentgate.llm_baseline import (
    b1_outcome_rows,
    cached_decisions,
    compute_metrics,
    coverage,
    estimate_cost,
    load_cached_b1_result,
    render_scenario_prompt,
    run_llm_b1,
    summarize_markdown,
)
from agentgate.scenarios import build_scenarios


class FakeJudge:
    """依情境類別回答的假審查員。``flip_ids`` 裡的情境每次答案都翻面(模擬不可重現)。"""

    def __init__(self, blocks: set[str] | None = None, flip_ids: set[str] | None = None,
                 bad_json: set[str] | None = None) -> None:
        self.blocks = blocks or set()
        self.flip_ids = flip_ids or set()
        self.bad_json = bad_json or set()
        self.seen: list[str] = []
        self._flip_state: dict[str, bool] = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        prompt = kwargs["messages"][1]["content"]
        self.seen.append(prompt)
        # prompt 裡沒有 scenario_id(那等同答案),所以假審查員靠來源鏈的
        # source_ref 認人 —— 見 _marker()。
        marker = _marker(prompt)
        if marker in self.bad_json:
            content = "不是 JSON"
        elif marker in self.flip_ids:
            state = self._flip_state.get(marker, False)
            self._flip_state[marker] = not state
            content = json.dumps({"should_execute": state, "risk": "medium",
                                  "reason": "看情況"})
        else:
            should = marker not in self.blocks
            content = json.dumps({"should_execute": should, "risk": "high",
                                  "reason": "測試用判定"}, ensure_ascii=False)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=200, completion_tokens=30,
                                  total_tokens=230),
        )


def _marker(prompt: str) -> str:
    """從 prompt 裡撈回情境 id —— 假審查員靠 chain 的 source_ref 認人。"""
    for line in prompt.splitlines():
        if "chat:session-" in line or "sso:" in line or "batch:" in line:
            return line.split(":")[-1].strip()
    return ""


FAKE_SETTINGS = AgentSettings(api_key=None, model="fake-judge")


def _scenarios(n: int = 12):
    all_s = build_scenarios()
    normal = [s for s in all_s if s.category == "normal"][:6]
    attack = [s for s in all_s if s.harmful][:6]
    return normal + attack


# --------------------------------------------------------------------------------------
class TestPrompt:
    def test_prompt_carries_facts_but_not_g0_conclusions(self):
        """給事實(對方有沒有通過身分驗證),不給 G0 的產物(信任等級、風險上限)。

        不給事實會讓 B1 因為「不知道用戶已驗證」而把正常查帳整批誤攔 ——
        那是我們 prompt 寫壞了,不是這條 baseline 的真實能力;
        給 G0 的結論則是偷偷幫它作弊,對照表就沒有意義了。
        """
        scenario = next(s for s in build_scenarios() if s.attack_type == "injection")
        prompt = render_scenario_prompt(scenario)
        assert "已完成身分驗證" in prompt
        for leak in ("untrusted", "risk_cap", "可授權上限", "最弱環節", "G0"):
            assert leak not in prompt

    def test_prompt_does_not_leak_the_label(self):
        scenario = next(s for s in build_scenarios() if s.category == "attack")
        prompt = render_scenario_prompt(scenario)
        assert scenario.attack_type not in prompt
        assert "harmful" not in prompt and "attack" not in prompt


class TestRun:
    def test_refuses_to_fabricate_a_baseline_without_a_key(self):
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            run_llm_b1(_scenarios(2), repeats=1, settings=AgentSettings(api_key=None))

    def test_run_writes_a_cache_with_raw_responses(self, tmp_path):
        scenarios = _scenarios()
        path = tmp_path / "b1.json"
        report = run_llm_b1(scenarios, repeats=2, settings=FAKE_SETTINGS,
                            client=FakeJudge(), cache_path=path, workers=2)
        assert path.is_file()
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["model"] == "fake-judge" and saved["repeats"] == 2
        assert saved["generated_at"]
        first = next(iter(saved["decisions"].values()))
        assert len(first["attempts"]) == 2
        assert first["attempts"][0]["raw"], "原始回應要留在快取裡"
        assert report["calls"]["total"] == len(scenarios) * 2

    def test_every_call_passes_max_tokens_and_json_mode(self, tmp_path):
        """成本控制:每一次呼叫都要帶 max_tokens,而且要求 JSON 模式。"""
        captured: list[dict] = []
        client = FakeJudge()
        original = client._create

        def spy(**kwargs):
            captured.append(kwargs)
            return original(**kwargs)

        client.chat = SimpleNamespace(completions=SimpleNamespace(create=spy))
        run_llm_b1(_scenarios(4), repeats=1, settings=FAKE_SETTINGS, client=client,
                   cache_path=tmp_path / "b1.json", max_tokens=77, workers=1)
        assert captured and all(c["max_tokens"] == 77 for c in captured)
        assert all(c["response_format"] == {"type": "json_object"} for c in captured)
        assert all(c["temperature"] == 0.0 for c in captured)

    def test_inconsistency_is_measured(self, tmp_path):
        """同輸入不同輸出的比例要量出來 —— 那是 §4.3「可重現」那條理由的證據。"""
        scenarios = _scenarios()
        flip = {_marker(render_scenario_prompt(scenarios[0]))}
        report = run_llm_b1(scenarios, repeats=3, settings=FAKE_SETTINGS,
                            client=FakeJudge(flip_ids=flip),
                            cache_path=tmp_path / "b1.json", workers=1)
        assert report["consistency"]["inconsistent"] == 1
        assert report["consistency"]["inconsistency_rate"] == pytest.approx(
            1 / len(scenarios), abs=1e-4)

    def test_unparsable_response_does_not_make_the_baseline_look_safer(self, tmp_path):
        """解析失敗 = 沒攔下來。誠實的保守方向。"""
        scenarios = _scenarios()
        bad = {_marker(render_scenario_prompt(s)) for s in scenarios if s.harmful}
        report = run_llm_b1(scenarios, repeats=1, settings=FAKE_SETTINGS,
                            client=FakeJudge(bad_json=bad),
                            cache_path=tmp_path / "b1.json", workers=1)
        assert report["metrics"]["HAR"] == 1.0


class TestMetrics:
    def test_har_tcr_fbr_denominators(self):
        scenarios = build_scenarios()
        decisions = {s.scenario_id: {"should_execute": s.category == "normal",
                                     "latency_ms": 900.0}
                     for s in scenarios}
        metrics = compute_metrics(scenarios, decisions)
        assert metrics["HAR"] == 0.0
        assert metrics["TCR"] == 1.0
        assert metrics["FBR"] == 0.0
        assert metrics["DL_p50_ms"] == 900.0

    def test_b1_has_no_esb_and_no_audit(self):
        """它沒有來源信任的概念,也沒有稽核鏈 —— 不是量不到,是本來就沒有。"""
        scenarios = build_scenarios()
        metrics = compute_metrics(scenarios, {})
        assert metrics["ESB"] == 0.0 and metrics["AC"] == 0.0

    def test_missing_decision_counts_as_executed(self):
        scenarios = build_scenarios()
        metrics = compute_metrics(scenarios, {})
        assert metrics["HAR"] == 1.0, "沒有判定不能算它攔下來了"

    def test_cost_estimate_uses_published_rates(self):
        cost = estimate_cost(1_000_000, 1_000_000)
        assert cost["usd_estimate"] == pytest.approx(0.15 + 0.60)
        assert cost["rate_usd_per_mtok"]["input"] == 0.15


class TestCacheIntegration:
    """``validation.py`` 的整合點。這幾條是給另一條線接手用的契約測試。"""

    def test_load_cached_returns_none_when_absent(self, tmp_path):
        assert load_cached_b1_result(tmp_path / "nope.json") is None

    def test_load_cached_rejects_garbage(self, tmp_path):
        path = tmp_path / "b1.json"
        path.write_text("{}", encoding="utf-8")
        assert load_cached_b1_result(path) is None

    def test_env_var_overrides_the_cache_path(self, tmp_path, monkeypatch):
        path = tmp_path / "b1.json"
        run_llm_b1(_scenarios(4), repeats=1, settings=FAKE_SETTINGS,
                   client=FakeJudge(), cache_path=path, workers=1)
        monkeypatch.setenv("AGENTGATE_B1_CACHE", str(path))
        assert load_cached_b1_result() is not None

    def test_outcome_rows_match_scenario_outcome_fields(self, tmp_path):
        """欄位必須對得上 ``validation.ScenarioOutcome``,否則整合會炸。"""
        from dataclasses import fields

        from agentgate.validation import ScenarioOutcome

        scenarios = _scenarios()
        path = tmp_path / "b1.json"
        run_llm_b1(scenarios, repeats=1, settings=FAKE_SETTINGS, client=FakeJudge(),
                   cache_path=path, workers=1)
        cached = load_cached_b1_result(path)
        rows = b1_outcome_rows(scenarios, cached)
        assert {f.name for f in fields(ScenarioOutcome)} == set(rows[0])
        outcomes = [ScenarioOutcome(**row) for row in rows]
        assert len(outcomes) == len(scenarios)

    def test_blocked_rows_are_attributed_to_llm(self, tmp_path):
        scenarios = _scenarios()
        blocks = {_marker(render_scenario_prompt(s)) for s in scenarios if s.harmful}
        path = tmp_path / "b1.json"
        run_llm_b1(scenarios, repeats=1, settings=FAKE_SETTINGS,
                   client=FakeJudge(blocks=blocks), cache_path=path, workers=1)
        rows = b1_outcome_rows(scenarios, load_cached_b1_result(path))
        blocked = [r for r in rows if r["status"] == "blocked"]
        assert blocked and all(r["gate_blocked_at"] == "LLM" for r in blocked)

    def test_coverage_flags_a_stale_cache(self, tmp_path):
        """情境數被擴充後,快取會變舊 —— 要看得出來,不能安靜地少算幾條。"""
        subset = _scenarios()
        path = tmp_path / "b1.json"
        run_llm_b1(subset, repeats=1, settings=FAKE_SETTINGS, client=FakeJudge(),
                   cache_path=path, workers=1)
        cached = load_cached_b1_result(path)
        cov = coverage(build_scenarios(), cached)
        assert cov["stale"] and cov["missing"] > 0
        assert coverage(subset, cached)["stale"] is False

    def test_cached_decisions_is_a_flat_map(self, tmp_path):
        path = tmp_path / "b1.json"
        run_llm_b1(_scenarios(4), repeats=1, settings=FAKE_SETTINGS,
                   client=FakeJudge(), cache_path=path, workers=1)
        flat = cached_decisions(load_cached_b1_result(path))
        assert flat and all(isinstance(v, bool) for v in flat.values())


class TestReporting:
    def test_summary_table_matches_validation_format(self, tmp_path):
        path = tmp_path / "b1.json"
        report = run_llm_b1(_scenarios(6), repeats=2, settings=FAKE_SETTINGS,
                            client=FakeJudge(), cache_path=path, workers=1)
        table = summarize_markdown(report)
        header = ("| Baseline | HAR 有害放行 | TCR 任務完成 | FBR 誤攔 "
                  "| ESB 升級攔截 | AC 稽核完整 | DL p95 (ms) |")
        assert table.startswith(header)
        assert "不一致率" in table and "估算成本" in table
        assert "自建合成資料" in table


class TestShippedCache:
    """repo 裡那一份真實跑出來的快取。沒有它,離線 Demo 就報不出真 B1。"""

    def test_shipped_cache_is_loadable_and_honest(self):
        cached = load_cached_b1_result()
        if cached is None:
            pytest.skip("尚未產生 runs/agentgate_b1_llm.json(需要金鑰跑一次)")
        assert cached["model"] and cached["generated_at"]
        assert cached["repeats"] >= 1
        assert "自建合成資料" in cached["disclaimer"]
        rows = b1_outcome_rows(build_scenarios(), cached)
        assert len(rows) == len(build_scenarios())
