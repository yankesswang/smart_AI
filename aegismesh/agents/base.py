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

# 五個 Agent 共用的用字規範。
#
# 敘述是戰情室畫面上字最多、最多人讀的區塊，而 facts 裡塞的是 w-fiber、
# svc-ed-vitals 這種內部代號 —— 不明講的話模型會直接照抄，最顯眼的地方
# 反而變成最看不懂的地方。規範集中在這裡，五個 Agent 才不會各自走調。
PLAIN_LANGUAGE = (
    "讀者是不具電信背景的醫院管理者與競賽評審。請全程使用一般人看得懂的繁體中文："
    "不可出現 w-fiber、svc-ed-vitals 這類內部代號（改用它們的中文名稱）；"
    "不要使用 SLA、SLO、CPE、VSAT、gNB、QoS、Mbps 以外的英文縮寫，"
    "必要時改說「服務品質標準」「對外設備」「衛星天線」「基地台」。"
    "「關鍵業務」請一律說成「救命服務」或「生命關鍵醫療服務」，"
    "「可用率」說成「正常率」，「延遲」說成「反應時間」。"
)


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
