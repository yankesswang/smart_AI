"""實測層 —— 從 console 的回填當班資料直接數,不假設。

規格 §8.2 說第一項「可從 §5 的測試集直接量出高風險比例,不需假設」。
這裡就是那一句話的實作,而且量的不是測試集,是**營運流量**:
``console.OpsSimulator.warm_start()`` 會回填一整段當班歷史(預設 45 分鐘、約 400 件),
每一件都真的走過 G0→G5,留下 status 與 risk。我們直接數它。

三個會進 ROI 公式的比例:

============================  ==================================================
自動放行率 ``auto_pass``      裁決 = ``executed`` 的比例(沒有人碰過)
送核准比例 ``approval``       裁決 = ``pending_approval`` 的比例(仍要人簽)
攔截比例 ``blocked``          裁決 = ``blocked`` 的比例(治理層自己擋掉)
============================  ==================================================

以及兩個風險比例:``high_risk``(risk ∈ {high, forbidden})與
``review_needed``(risk ≥ medium)。後者是**現況分層覆核的覆核覆蓋率** ——
一個謹慎的企業在沒有治理層時,會讓 Agent 自動做唯讀查詢,任何會改動狀態的動作退回人工。

---
為什麼要跑多個 seed
---
一個 45 分鐘視窗約 400 件,比例的抽樣誤差有一到兩個百分點。跑 8 個 seed 取平均,
並把 min/max 一起報出來,讓人看得到這個數字有多穩。時鐘固定在同一個時間點,
所以**同樣的參數必然得到同樣的數字**(``tests/test_agentgate_business.py`` 守住這件事)。

---
誠實邊界
---
流量的**組成**(19 個情境樣板的權重、攻擊佔約 12%)是 ``console.py`` 裡寫死的設計,
是假設不是實測。所以正確的說法是:

> 在這個假設的流量組成下,治理層的自動放行率是**實測**的 X%。

攻擊比例刻意高於真實環境(為了讓四分鐘 Demo 看得到東西),這會**壓低**自動放行率、
**推高**攔截比例 —— 對效益是保守方向。
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from .assumptions import Basis

#: 量測用的固定時鐘。不是「現在」—— 現在會讓班別、SLA 與亂數落點跟著日期漂。
MEASUREMENT_CLOCK = datetime(2026, 8, 17, 6, 0, tzinfo=timezone.utc)

#: 預設 seed。從 console 的預設 seed(20260817)往後數 8 個。
DEFAULT_SEEDS: tuple[int, ...] = tuple(range(20260817, 20260825))

#: 回填視窗長度(分鐘)。與 `agentgate serve` 的預設一致。
DEFAULT_WINDOW_MINUTES: int = 45


@dataclass(frozen=True)
class ShiftSample:
    """一個 seed、一段當班視窗的量測結果。"""

    seed: int
    actions: int
    window_minutes: int
    auto_pass: float
    approval: float
    blocked: float
    high_risk: float
    review_needed: float
    audit_records_per_action: float
    # 決策延遲是牆鐘時間,**不進報表** —— 它每次跑都不一樣,放進去就毀了
    # 「同樣的 seed 必然得到同樣的數字」這條承諾。要看延遲請用 metrics.summary()。
    latency_p50_ms: float
    latency_p95_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": Basis.MEASURED.value,
            "seed": self.seed,
            "actions": self.actions,
            "window_minutes": self.window_minutes,
            "auto_pass": round(self.auto_pass, 4),
            "approval": round(self.approval, 4),
            "blocked": round(self.blocked, 4),
            "high_risk": round(self.high_risk, 4),
            "review_needed": round(self.review_needed, 4),
            "audit_records_per_action": round(self.audit_records_per_action, 3),
        }


@dataclass(frozen=True)
class OpsMeasurement:
    """多個 seed 的彙總。ROI 公式吃的是 ``*_mean``,``*_min/max`` 用來說明它有多穩。"""

    samples: list[ShiftSample]
    window_minutes: int
    kind_mix: dict[str, int] = field(default_factory=dict)
    gate_mix: dict[str, int] = field(default_factory=dict)

    # ---- 進 ROI 公式的三個比例 ----------------------------------------------------
    @property
    def actions_per_minute(self) -> float:
        return statistics.fmean(s.actions / s.window_minutes for s in self.samples)

    @property
    def auto_pass_rate(self) -> float:
        return statistics.fmean(s.auto_pass for s in self.samples)

    @property
    def approval_rate(self) -> float:
        return statistics.fmean(s.approval for s in self.samples)

    @property
    def blocked_rate(self) -> float:
        return statistics.fmean(s.blocked for s in self.samples)

    @property
    def high_risk_share(self) -> float:
        return statistics.fmean(s.high_risk for s in self.samples)

    @property
    def review_needed_share(self) -> float:
        """風險 ≥ medium 的比例 = 現況「分層覆核」下會被退回人工的比例。"""
        return statistics.fmean(s.review_needed for s in self.samples)

    @property
    def audit_records_per_action(self) -> float:
        return statistics.fmean(s.audit_records_per_action for s in self.samples)

    @property
    def latency_p95_ms(self) -> float:
        return statistics.fmean(s.latency_p95_ms for s in self.samples)

    def spread(self, attr: str) -> tuple[float, float]:
        values = [getattr(s, attr) for s in self.samples]
        return min(values), max(values)

    def to_dict(self) -> dict[str, Any]:
        def stat(attr: str) -> dict[str, float]:
            lo, hi = self.spread(attr)
            return {
                "mean": round(statistics.fmean(getattr(s, attr) for s in self.samples), 4),
                "min": round(lo, 4),
                "max": round(hi, 4),
            }

        return {
            "basis": Basis.MEASURED.value,
            "source": "console.OpsSimulator.warm_start()",
            "seeds": [s.seed for s in self.samples],
            "window_minutes": self.window_minutes,
            "actions_total": sum(s.actions for s in self.samples),
            "actions_per_minute": round(self.actions_per_minute, 4),
            "auto_pass_rate": stat("auto_pass"),
            "approval_rate": stat("approval"),
            "blocked_rate": stat("blocked"),
            "high_risk_share": stat("high_risk"),
            "review_needed_share": stat("review_needed"),
            "audit_records_per_action": round(self.audit_records_per_action, 3),
            "action_kind_mix": dict(self.kind_mix),
            "blocked_by_gate": dict(self.gate_mix),
            "samples": [s.to_dict() for s in self.samples],
            "deterministic": True,
            "caveat": (
                "流量的組成(19 個情境樣板的權重、攻擊約佔 12%)是 console.py 寫死的設計,"
                "屬假設;在該組成下的裁決結果才是實測。攻擊比例刻意高於真實環境,"
                "會壓低自動放行率 —— 對效益是保守方向。"
            ),
        }


def measure_ops(
    seeds: Sequence[int] = DEFAULT_SEEDS,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    now: datetime = MEASUREMENT_CLOCK,
) -> OpsMeasurement:
    """跑 ``len(seeds)`` 次回填當班歷史,直接數裁決結果。

    每個 seed 都是一個**全新的**影子後台 + 稽核鏈 + 佇列,彼此不互相污染。
    """
    from ..console import OpsSimulator
    from ..pipeline import AgentGatePipeline, GateConfig
    from ..shadow import ShadowTelecomEnv

    samples: list[ShiftSample] = []
    kind_mix: Counter[str] = Counter()
    gate_mix: Counter[str] = Counter()

    for seed in seeds:
        shadow = ShadowTelecomEnv()
        pipeline = AgentGatePipeline(shadow=shadow, config=GateConfig())
        ops = OpsSimulator(pipeline=pipeline, shadow=shadow, seed=seed,
                           keep_cases=1_000_000)
        ops.warm_start(minutes=window_minutes, now=now)

        records = ops.records
        total = len(records) or 1
        status = Counter(r.verdict.get("status") for r in records)
        risk = Counter(r.verdict.get("risk") for r in records)
        kind_mix.update(r.request_dict["kind"] for r in records)
        gate_mix.update(r.verdict["gate_blocked_at"] for r in records
                        if r.verdict.get("gate_blocked_at"))
        latency = pipeline.metrics.summary()["decision_latency_ms"]

        samples.append(
            ShiftSample(
                seed=seed,
                actions=len(records),
                window_minutes=window_minutes,
                auto_pass=status.get("executed", 0) / total,
                approval=status.get("pending_approval", 0) / total,
                blocked=status.get("blocked", 0) / total,
                high_risk=(risk.get("high", 0) + risk.get("forbidden", 0)) / total,
                review_needed=(risk.get("medium", 0) + risk.get("high", 0)
                               + risk.get("forbidden", 0)) / total,
                audit_records_per_action=len(pipeline.audit.records) / total,
                latency_p50_ms=latency["p50"],
                latency_p95_ms=latency["p95"],
            )
        )
    return OpsMeasurement(samples=samples, window_minutes=window_minutes,
                          kind_mix=dict(kind_mix), gate_mix=dict(gate_mix))


# ======================================================================================
# 有害動作放行率(HAR)—— 誤動作損失那一項的乘數,直接讀驗證管線
# ======================================================================================
@dataclass(frozen=True)
class HarmMeasurement:
    """B0(無治理)與 B3(完整五道關卡)的 HAR。**不是假設,是 §5.3 的實測。**"""

    har_ungoverned: float
    har_governed: float
    scenarios: int

    @property
    def prevented_share(self) -> float:
        return max(0.0, self.har_ungoverned - self.har_governed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": Basis.MEASURED.value,
            "source": "agentgate.validation.run_validation()(自建 120 條情境測試集)",
            "har_ungoverned_b0": round(self.har_ungoverned, 4),
            "har_governed_b3": round(self.har_governed, 4),
            "prevented_share": round(self.prevented_share, 4),
            "attack_scenarios": self.scenarios,
            "caveat": "測試集為自建合成資料;外部效度另見 docs/agentgate/agentgate_external_validation.md。",
        }


def measure_harm_prevention() -> HarmMeasurement:
    """跑一次 §5.3 的 baseline 對照,取 B0 與 B3 的 HAR。"""
    from ..validation import run_validation

    report = run_validation()
    by_id = {b["baseline"]: b for b in report["baselines"]}
    b0 = by_id.get("B0", {}).get("metrics", {})
    b3 = by_id.get("B3", {}).get("metrics", {})
    scenarios = int(report.get("scenario_stats", {}).get("attack", 0) or 0)
    return HarmMeasurement(
        har_ungoverned=float(b0.get("HAR", 1.0)),
        har_governed=float(b3.get("HAR", 0.0)),
        scenarios=scenarios,
    )


__all__ = [
    "DEFAULT_SEEDS",
    "DEFAULT_WINDOW_MINUTES",
    "MEASUREMENT_CLOCK",
    "HarmMeasurement",
    "OpsMeasurement",
    "ShiftSample",
    "measure_harm_prevention",
    "measure_ops",
]
