"""Factory Guardian AI — Agentic AI 智慧工廠自主營運與風險管理平台。

競賽 MVP 的三個可信度原則（見 docs/Factory_Guardian_AI_競賽提案與Demo規格.docx §1.2）：

1. 所有資料都是 Synthetic / Simulation，並在輸出中明確標示。
2. Agent 看不到 Simulator 的 Ground Truth，只能從 Sensor / Manual / History / Rule 推論。
3. 執行後必須回到 Simulator 驗證，而不是停在「LLM 建議」。
"""

__version__ = "0.1.0"

DATA_DISCLAIMER = (
    "SYNTHETIC DEMO DATA：本平台之 Sensor / Orders / Maintenance History / Manual "
    "皆為競賽用合成資料，不代表任何真實工廠或設備商規格。"
)

__all__ = ["__version__", "DATA_DISCLAIMER"]
