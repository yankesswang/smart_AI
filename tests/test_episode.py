"""多時段推演的不變式。

這裡守住的是「為什麼不能人工判斷就好」這個主張本身：
配速決策的代價要到十幾小時後才浮現，而那時已經沒有回頭路。
"""

from __future__ import annotations

import pytest

from aegismesh.episode import AEGIS, HUMAN_HEURISTIC, compare, run_episode
from aegismesh.twin import SCENARIOS


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
@pytest.mark.parametrize("policy", [HUMAN_HEURISTIC, AEGIS])
def test_episode_covers_the_whole_event(scenario_id, policy):
    """時段必須連續且剛好蓋滿事件長度，不能有沒推演到的空窗。"""
    scenario = SCENARIOS[scenario_id]
    result = run_episode(scenario, policy)

    assert result.steps, "推演至少要有一個時段"
    total = sum(s.duration_h for s in result.steps)
    assert total == pytest.approx(scenario.duration_hours), (
        f"{scenario_id}：時段總長 {total} 小時，事件長度 {scenario.duration_hours} 小時"
    )
    hours = [s.hour for s in result.steps]
    assert hours == sorted(hours), "時段必須依時間排序"
    for prev, nxt in zip(result.steps, result.steps[1:]):
        assert prev.hour + prev.duration_h == pytest.approx(nxt.hour), "時段之間不能有缺口"


@pytest.mark.parametrize("policy", [HUMAN_HEURISTIC, AEGIS])
def test_quota_is_actually_consumed_not_merely_estimated(policy):
    """配額必須真的被扣掉：前面用得兇，後面就真的沒得用。"""
    result = run_episode(SCENARIOS["typhoon-fiber-cut"], policy)
    left = [s.quota_left_gb for s in result.steps]
    assert left == sorted(left, reverse=True), f"配額只能遞減，實際為 {left}"
    assert left[0] < 300.0, "第一個時段就該扣掉一些配額"


def test_life_critical_services_never_die():
    """生命關鍵（P0）的臨床底線不跟預算談判 —— 任何策略下都不得歸零。"""
    for policy in (HUMAN_HEURISTIC, AEGIS):
        result = run_episode(SCENARIOS["typhoon-fiber-cut"], policy)
        for step in result.steps:
            assert step.snapshot.life_critical_availability_pct == pytest.approx(100.0), (
                f"{policy} 在 h{step.hour:.0f} 讓生命關鍵服務斷線"
            )


def test_pacing_beats_the_human_heuristic():
    """整個系統存在的理由：同一場災害，配速能多換到幾小時的正常服務。

    人工經驗法則在每個當下都是對的（救命優先、備援有多少用多少），
    錯只錯在沒有人能在腦中把配額攤平到未來十幾個小時。
    """
    scenario = SCENARIOS["typhoon-fiber-cut"]
    results = compare(scenario)
    human = results[HUMAN_HEURISTIC]
    aegis = results[AEGIS]

    assert aegis.critical_service_hours > human.critical_service_hours, (
        f"配速沒有勝出：人工 {human.critical_service_hours:.1f}h、"
        f"AegisMesh {aegis.critical_service_hours:.1f}h"
    )
    # 人工法則提前把配額燒光，AegisMesh 撐完全程
    assert human.quota_exhausted_at is not None
    assert human.quota_exhausted_at < scenario.duration_hours
    assert (aegis.quota_exhausted_at is None
            or aegis.quota_exhausted_at >= scenario.duration_hours)


def test_aegis_switches_strategy_across_the_event():
    """配速不是選定一個策略就不動 —— 隨著剩餘時間與線路狀態改變會換檔。"""
    result = run_episode(SCENARIOS["typhoon-fiber-cut"], AEGIS)
    used = [s.strategy for s in result.steps if s.strategy]
    assert len(set(used)) > 1, f"整場只用了同一個策略：{used}"
