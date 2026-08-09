"""Agent 共同骨架。

每個 Agent 都是兩層結構：
  facts     —— 由孿生引擎／最佳化器／政策引擎算出的決定性事實（可稽核、可重現）
  narrative —— LLM 對這些事實的解釋（可有可無，離線時由樣板產生）

這個分界是整套系統可信度的來源：LLM 說錯話不會導致錯誤的網路變更，
因為它從頭到尾沒有權限決定路徑、頻寬或核准與否。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..llm import LLMClient


@dataclass
class AgentResult:
    agent: str
    stage: str
    facts: dict[str, Any]
    narrative: str
    llm_mode: str = "offline"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "stage": self.stage,
            "facts": self.facts,
            "narrative": self.narrative,
            "llm_mode": self.llm_mode,
            "extras": self.extras,
        }


class Agent:
    name = "agent"
    stage = "stage"
    system_prompt = "You are an assistant."

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def _ask(self, user_prompt: str, fallback_fn, **kwargs) -> dict[str, Any]:
        return self.llm.complete_json(self.system_prompt, user_prompt, fallback_fn, **kwargs)
