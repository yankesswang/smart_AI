"""多時段推演：把「現在最好的方案」與「整場事件最好的方案」分開。

單次規劃只看得到當下，而災害的後半場往往才是最需要備援的時候。這個模組
把情境沿時間軸跑完，每個時段重新規劃、實際消耗衛星配額，最後用一個數字
回答「這套策略總共讓救命服務正常運作了幾小時」。

這裡完全沒有 LLM。它是數位孿生的預測能力本身 —— 也是回答「為什麼不能
人工判斷就好」最直接的證據：配速決策的代價要到十幾小時後才浮現。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .domain import NetworkSnapshot, RecoveryPlan, Scenario
from .optimizer import STRATEGY_LABELS, generate_plans, score_plan
from .policy.engine import DENY, PolicyEngine
from .twin.engine import DigitalTwin

# 對照組：資深維運人員在現場最可能採取的經驗法則 ——
# 「救命的先救，備援有多少用多少」。它在每個當下都是對的，
# 錯只錯在沒有人能在腦中把配額攤平到未來 18 小時。
HUMAN_HEURISTIC = "human"
AEGIS = "aegis"

POLICY_LABELS = {
    HUMAN_HEURISTIC: "人工經驗法則（救命優先，備援有多少用多少）",
    AEGIS: "AegisMesh（配速 ＋ 治理紅線）",
}


@dataclass
class EpisodeStep:
    hour: float
    duration_h: float
    label: str
    strategy: str | None
    critical_availability_pct: float
    slo_compliance_pct: float
    quota_left_gb: float
    snapshot: NetworkSnapshot = field(repr=False)

    @property
    def critical_hours(self) -> float:
        """這個時段貢獻的「救命服務正常小時數」。"""
        return self.critical_availability_pct / 100.0 * self.duration_h


@dataclass
class EpisodeResult:
    scenario: Scenario
    policy: str
    steps: list[EpisodeStep]

    @property
    def critical_service_hours(self) -> float:
        """整場事件中救命服務正常運作的累計小時數 —— 唯一的總分。"""
        return sum(s.critical_hours for s in self.steps)

    @property
    def quota_exhausted_at(self) -> float | None:
        """配額歸零的時刻；None 表示撐完了整場事件。"""
        for s in self.steps:
            if s.quota_left_gb <= 1e-6:
                return s.hour + s.duration_h
        return None

    def summary(self) -> dict:
        return {
            "scenario": self.scenario.id,
            "policy": self.policy,
            "critical_service_hours": round(self.critical_service_hours, 2),
            "event_hours": self.scenario.duration_hours,
            "quota_exhausted_at": self.quota_exhausted_at,
            "steps": [
                {
                    "hour": s.hour,
                    "strategy": s.strategy,
                    "critical_availability_pct": round(s.critical_availability_pct, 1),
                    "quota_left_gb": round(s.quota_left_gb, 1),
                }
                for s in self.steps
            ],
        }


def _select(
    policy: str,
    plans: list[RecoveryPlan],
    twin: DigitalTwin,
    before: NetworkSnapshot,
    engine: PolicyEngine,
) -> RecoveryPlan | None:
    if not plans:
        return None

    if policy == HUMAN_HEURISTIC:
        # 經驗法則不看配額、也不經治理層 —— 這正是要對照的東西
        protect = [p for p in plans if p.strategy == "protect_critical"]
        if protect:
            return protect[0]
        return max(plans, key=lambda p: p.projected.critical_availability_pct if p.projected else 0.0)

    for plan in plans:
        engine.apply_to(twin, plan, before)
        plan.score = score_plan(before, plan.projected, plan, twin)
    eligible = [p for p in plans if p.policy_decision != DENY]
    return max(eligible, key=lambda p: p.score) if eligible else None


def run_episode(scenario: Scenario, policy: str = AEGIS) -> EpisodeResult:
    """沿時間軸推演整場事件，回傳每個時段的結果。"""
    twin = DigitalTwin()
    twin.apply_scenario(scenario)
    engine = PolicyEngine()
    steps = scenario.steps()
    results: list[EpisodeStep] = []

    for i, step in enumerate(steps):
        end = steps[i + 1].hour if i + 1 < len(steps) else scenario.duration_hours
        duration = max(end - step.hour, 0.0)

        twin.advance_to(step)
        before = twin.evaluate(f"h{step.hour:g}-incident")
        chosen = _select(policy, generate_plans(twin), twin, before, engine)
        if chosen is not None:
            twin.apply_plan(chosen)

        after = twin.evaluate(f"h{step.hour:g}-applied")
        twin.consume_quota(duration)
        left = min(
            (
                max(l.quota_gb - twin.quota_used_gb.get(lid, 0.0), 0.0)
                for lid, l in twin.links.items()
                if l.quota_gb is not None
            ),
            default=float("inf"),
        )

        results.append(EpisodeStep(
            hour=step.hour,
            duration_h=duration,
            label=step.label,
            strategy=chosen.strategy if chosen else None,
            critical_availability_pct=after.critical_availability_pct,
            slo_compliance_pct=after.slo_compliance_pct,
            quota_left_gb=left,
            snapshot=after,
        ))

    return EpisodeResult(scenario=scenario, policy=policy, steps=results)


def compare(scenario: Scenario) -> dict[str, EpisodeResult]:
    """同一場災害，兩種策略各跑一次。"""
    return {p: run_episode(scenario, p) for p in (HUMAN_HEURISTIC, AEGIS)}


__all__ = [
    "EpisodeResult", "EpisodeStep", "run_episode", "compare",
    "HUMAN_HEURISTIC", "AEGIS", "POLICY_LABELS", "STRATEGY_LABELS",
]
