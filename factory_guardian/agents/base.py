"""Agent 基底：共用的稽核、計時與工具呼叫統計。

Task Completion / Tool Success / Decision Latency 這幾個 §10 的 KPI
就是從這裡累積出來的，不是事後估的。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..audit import AuditLog
from ..config import Settings, get_settings
from ..knowledge.retriever import KnowledgeBase, default_kb
from ..llm import LLMClient
from ..policy.engine import PolicyEngine


@dataclass
class AgentContext:
    """所有 Agent 共用的執行環境。"""

    settings: Settings = field(default_factory=get_settings)
    audit: AuditLog | None = None
    kb: KnowledgeBase = field(default_factory=default_kb)
    policy: PolicyEngine | None = None
    llm: LLMClient | None = None

    def __post_init__(self) -> None:
        if self.policy is None:
            self.policy = PolicyEngine(require_approval=self.settings.require_approval)
        if self.llm is None:
            self.llm = LLMClient(settings=self.settings, audit=self.audit)


@dataclass
class ToolCall:
    name: str
    ok: bool
    latency_ms: float
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "latency_ms": round(self.latency_ms, 2), "detail": self.detail}


class Agent:
    """Agent 基底類別。"""

    name: str = "agent"
    role: str = ""

    def __init__(self, ctx: AgentContext | None = None) -> None:
        self.ctx = ctx or AgentContext()
        self.tool_calls: list[ToolCall] = []
        self.decisions: int = 0
        self.total_latency_ms: float = 0.0

    # -- 工具呼叫統計 -------------------------------------------------------------------
    @contextmanager
    def tool(self, name: str, detail: str = "") -> Iterator[None]:
        start = time.perf_counter()
        ok = True
        try:
            yield
        except Exception as exc:
            ok = False
            detail = f"{detail} | error={type(exc).__name__}: {exc}".strip(" |")
            raise
        finally:
            self.tool_calls.append(ToolCall(name, ok, (time.perf_counter() - start) * 1000, detail))

    @contextmanager
    def timed(self) -> Iterator[dict[str, float]]:
        holder: dict[str, float] = {}
        start = time.perf_counter()
        try:
            yield holder
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            holder["latency_ms"] = elapsed
            self.total_latency_ms += elapsed
            self.decisions += 1

    # -- 稽核 -------------------------------------------------------------------------
    def log(self, stage: str, **detail: Any) -> None:
        if self.ctx.audit is not None:
            self.ctx.audit.log(stage, self.name, **detail)

    # -- KPI --------------------------------------------------------------------------
    def metrics(self) -> dict[str, Any]:
        total = len(self.tool_calls)
        ok = sum(1 for c in self.tool_calls if c.ok)
        return {
            "agent": self.name,
            "tool_calls": total,
            "tool_success_pct": round(100.0 * ok / total, 1) if total else 100.0,
            "decisions": self.decisions,
            "avg_decision_latency_ms": round(self.total_latency_ms / self.decisions, 2) if self.decisions else 0.0,
        }


__all__ = ["Agent", "AgentContext", "ToolCall"]
