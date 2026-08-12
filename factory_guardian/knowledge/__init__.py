"""設備知識庫：Demo Equipment Manual、SOP、交機驗收記錄與 Maintenance History。

（皆為合成資料，不代表任何真實設備商規格或真實維修紀錄。）

Diagnosis Agent 的判斷依據**全部**來自這個套件，不得讀取 ``twin/faults.py``
的模擬器參數：

* ``commissioning`` —— 每台機器交機試車時量到的基準值（判讀基準）
* ``symptom_spec`` —— 手冊徵兆區間與鑑別診斷規則（排名依據）
* ``corpus`` —— 手冊/SOP 全文與維修歷史（Evidence 與先驗）
"""

from .commissioning import (
    COMMISSIONING,
    CommissioningRecord,
    SignalBaseline,
    commissioning_by_doc,
    commissioning_for,
)
from .corpus import (
    MAINTENANCE_HISTORY,
    MANUALS,
    MaintenanceCase,
    ManualDoc,
    manual_by_ref,
)
from .retriever import KnowledgeBase, RetrievedChunk
from .symptom_spec import (
    SYMPTOM_SPECS,
    DifferentialRule,
    FaultSymptomSpec,
    SymptomRange,
    spec_for,
)

__all__ = [
    "MANUALS",
    "MAINTENANCE_HISTORY",
    "ManualDoc",
    "MaintenanceCase",
    "manual_by_ref",
    "COMMISSIONING",
    "CommissioningRecord",
    "SignalBaseline",
    "commissioning_for",
    "commissioning_by_doc",
    "SYMPTOM_SPECS",
    "FaultSymptomSpec",
    "SymptomRange",
    "DifferentialRule",
    "spec_for",
    "KnowledgeBase",
    "RetrievedChunk",
]
