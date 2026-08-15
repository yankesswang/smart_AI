"""可靠性 harness：回答評審那句「你的 Demo 穩不穩」。

研究文件 §5.3 的「Demo 穩定」那一列要求量三件事，這裡逐項對上：

============  ==================================================================
連續成功次數    :attr:`ReliabilityReport.longest_streak`（外加總成功率與失敗清單）
離線備援時間    :attr:`ReliabilityReport.offline_failover_p95_s`（``offline=True`` 時）
資料遺失率      :attr:`ReliabilityReport.data_loss_pct_max`
============  ==================================================================

外加一項文件沒寫、但競賽現場更致命的：**決策一致性**。
連續跑 30 次都「沒有例外」是很低的標準 —— 每次都推薦不同方案一樣會在台上翻車。
所以每一輪都會算決策指紋，最後檢查它們是不是同一個值。

harness 走的是和舞台完全相同的 :class:`~factory_guardian.stage.director.StageDirector`，
只是把節奏設成 ``fast``、不落地稽核檔。跑的是真的閉環，不是模擬跑。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .director import DEFAULT_SCENARIO, STAGE_SEED, StageDirector, StageRun


def percentile(values: list[float], q: float) -> float:
    """線性內插的百分位數（``q`` 為 0~100）。

    刻意自己算而不用 ``statistics.quantiles``：後者在 n < 2 時直接丟例外，
    而 harness 必須能跑 ``--runs 1``（現場只想確認一次能不能跑）。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * max(0.0, min(100.0, q)) / 100.0
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


@dataclass
class RunOutcome:
    """一輪的結果。"""

    index: int
    ok: bool
    seconds: float                    # 不含節奏停頓的閉環運算耗時
    fingerprint_hash: str
    data_loss_pct: float
    offline_failover_s: float | None
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "ok": self.ok,
            "seconds": round(self.seconds, 4),
            "fingerprint_hash": self.fingerprint_hash,
            "data_loss_pct": round(self.data_loss_pct, 2),
            "offline_failover_s": None if self.offline_failover_s is None else round(self.offline_failover_s, 4),
            "failures": self.failures,
        }


@dataclass
class ReliabilityReport:
    scenario_id: str
    runs: int
    offline: bool
    outcomes: list[RunOutcome] = field(default_factory=list)
    wall_s: float = 0.0

    # --- 成功率 -----------------------------------------------------------------------
    @property
    def successes(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def failures(self) -> int:
        return len(self.outcomes) - self.successes

    @property
    def success_rate_pct(self) -> float:
        return 100.0 * self.successes / len(self.outcomes) if self.outcomes else 0.0

    @property
    def failure_rate_pct(self) -> float:
        return 100.0 - self.success_rate_pct

    @property
    def longest_streak(self) -> int:
        """最長連續成功次數 —— 文件 §5.3 指名要的那個數字。"""
        best = current = 0
        for outcome in self.outcomes:
            current = current + 1 if outcome.ok else 0
            best = max(best, current)
        return best

    # --- 耗時 -------------------------------------------------------------------------
    def _durations(self) -> list[float]:
        return [o.seconds for o in self.outcomes]

    @property
    def p50_s(self) -> float:
        return percentile(self._durations(), 50)

    @property
    def p95_s(self) -> float:
        return percentile(self._durations(), 95)

    @property
    def max_s(self) -> float:
        return max(self._durations(), default=0.0)

    @property
    def mean_s(self) -> float:
        values = self._durations()
        return sum(values) / len(values) if values else 0.0

    # --- 一致性與資料完整度 --------------------------------------------------------------
    @property
    def fingerprints(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.fingerprint_hash] = counts.get(outcome.fingerprint_hash, 0) + 1
        return counts

    @property
    def decisions_consistent(self) -> bool:
        """每一輪的決策指紋都一樣才算一致（沒跑過不算一致）。"""
        return bool(self.outcomes) and len(self.fingerprints) == 1

    @property
    def data_loss_pct_max(self) -> float:
        return max((o.data_loss_pct for o in self.outcomes), default=0.0)

    @property
    def data_loss_pct_mean(self) -> float:
        values = [o.data_loss_pct for o in self.outcomes]
        return sum(values) / len(values) if values else 0.0

    @property
    def offline_failover_p95_s(self) -> float | None:
        values = [o.offline_failover_s for o in self.outcomes if o.offline_failover_s is not None]
        return percentile(values, 95) if values else None

    @property
    def offline_failover_max_s(self) -> float | None:
        values = [o.offline_failover_s for o in self.outcomes if o.offline_failover_s is not None]
        return max(values) if values else None

    def failure_reasons(self) -> list[str]:
        out: list[str] = []
        for outcome in self.outcomes:
            for reason in outcome.failures:
                out.append(f"#{outcome.index}: {reason}")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "runs": self.runs,
            "offline": self.offline,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate_pct": round(self.success_rate_pct, 2),
            "failure_rate_pct": round(self.failure_rate_pct, 2),
            "longest_streak": self.longest_streak,
            "seconds": {
                "p50": round(self.p50_s, 4),
                "p95": round(self.p95_s, 4),
                "max": round(self.max_s, 4),
                "mean": round(self.mean_s, 4),
            },
            "decisions_consistent": self.decisions_consistent,
            "fingerprints": self.fingerprints,
            "data_loss_pct_max": round(self.data_loss_pct_max, 3),
            "data_loss_pct_mean": round(self.data_loss_pct_mean, 3),
            "offline_failover_p95_s": (
                None if self.offline_failover_p95_s is None else round(self.offline_failover_p95_s, 4)
            ),
            "offline_failover_max_s": (
                None if self.offline_failover_max_s is None else round(self.offline_failover_max_s, 4)
            ),
            "wall_s": round(self.wall_s, 3),
            "failure_reasons": self.failure_reasons(),
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


ProgressCallback = Callable[[RunOutcome], None]


def run_reliability(
    runs: int = 30,
    scenario_id: str = DEFAULT_SCENARIO,
    offline: bool = False,
    seed: int = STAGE_SEED,
    on_run: ProgressCallback | None = None,
    persist_audit: bool = False,
) -> ReliabilityReport:
    """連續跑 ``runs`` 次完整舞台劇本，統計穩定度。"""
    if runs < 1:
        raise ValueError("runs 至少要 1")
    report = ReliabilityReport(scenario_id=scenario_id, runs=runs, offline=offline)
    started = time.perf_counter()
    for index in range(1, runs + 1):
        director = StageDirector(
            scenario_id=scenario_id,
            speed="fast",
            offline=offline,
            persist_audit=persist_audit,
            seed=seed,
        )
        run: StageRun = director.run()
        outcome = RunOutcome(
            index=index,
            ok=run.ok,
            seconds=run.compute_s,
            fingerprint_hash=run.fingerprint_hash,
            data_loss_pct=run.data_loss_pct,
            offline_failover_s=run.offline_failover_s,
            failures=run.failures(),
        )
        report.outcomes.append(outcome)
        if on_run:
            on_run(outcome)
    report.wall_s = time.perf_counter() - started
    return report


__all__ = ["ReliabilityReport", "RunOutcome", "percentile", "run_reliability"]
