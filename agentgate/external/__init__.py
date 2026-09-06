"""外部效度驗證 —— 把 AgentGate 的 G0 放到別人做的公開 benchmark 上跑。

規格 §5.1 把「公開 agent benchmark 的可得性」列為 **唯一會讓整個題目重新評估的風險**
(§10 W1 第一天必須完成的資料可得性驗證)。這個套件就是那項作業的產出。

目前只有一條路徑:[`agentdojo_eval`](agentdojo_eval.py) ——
ETH Zurich 的 [AgentDojo](https://github.com/ethz-spylab/agentdojo)(MIT 授權),
專為**間接提示注入**設計的 agent 評測環境。

這裡刻意不放任何「AgentGate 在公開 benchmark 上得幾分」的結論字串。
結論寫在 [`docs/agentgate_external_validation.md`](../../docs/agentgate_external_validation.md),
而且第一節是「這驗證的是什麼、不是什麼」。
"""

from .agentdojo_eval import (
    AGENTDOJO_AVAILABLE,
    ATTRIBUTION_MODES,
    DEFAULT_SUBSET,
    GENERIC_TOOL_RISK,
    SubsetSpec,
    ToolRiskSpec,
    default_trace_path,
    gate_action,
    load_traces,
    replay_report,
    run_agentdojo_eval,
    tool_risk,
)

__all__ = [
    "AGENTDOJO_AVAILABLE",
    "ATTRIBUTION_MODES",
    "DEFAULT_SUBSET",
    "GENERIC_TOOL_RISK",
    "SubsetSpec",
    "ToolRiskSpec",
    "default_trace_path",
    "gate_action",
    "load_traces",
    "replay_report",
    "run_agentdojo_eval",
    "tool_risk",
]
