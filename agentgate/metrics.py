"""即時指標累積(規格 §5.2 的線上可觀測部分)。

HAR / TCR / FBR / ESB 需要情境標註(ground truth),只能由驗證管線
(validation.py)在測試集上計算;這裡累積的是線上就能算的:
決策延遲 DL(p50/p95)、各狀態計數、各關卡攔截數。
"""

from __future__ import annotations

import statistics
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .ontology import GateVerdict


class LiveMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.latencies_ms: list[float] = []
        self.status_counts: dict[str, int] = {}
        self.blocked_by_gate: dict[str, int] = {}
        self.risk_counts: dict[str, int] = {}

    def record(self, verdict: "GateVerdict", is_approval_outcome: bool = False) -> None:
        with self._lock:
            if not is_approval_outcome:
                # 核准後的執行不計入決策延遲(那是人的時間,不是治理層的)
                self.latencies_ms.append(verdict.decision_latency_ms)
            self.status_counts[verdict.status] = self.status_counts.get(verdict.status, 0) + 1
            self.risk_counts[verdict.risk] = self.risk_counts.get(verdict.risk, 0) + 1
            if verdict.gate_blocked_at:
                self.blocked_by_gate[verdict.gate_blocked_at] = (
                    self.blocked_by_gate.get(verdict.gate_blocked_at, 0) + 1
                )

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
        return ordered[idx]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            lat = list(self.latencies_ms)
            return {
                "decisions": len(lat),
                "decision_latency_ms": {
                    "p50": round(self._percentile(lat, 50), 3),
                    "p95": round(self._percentile(lat, 95), 3),
                    "mean": round(statistics.fmean(lat), 3) if lat else 0.0,
                },
                "status_counts": dict(self.status_counts),
                "blocked_by_gate": dict(self.blocked_by_gate),
                "risk_counts": dict(self.risk_counts),
            }

    def reset(self) -> None:
        with self._lock:
            self.latencies_ms = []
            self.status_counts = {}
            self.blocked_by_gate = {}
            self.risk_counts = {}


__all__ = ["LiveMetrics"]
