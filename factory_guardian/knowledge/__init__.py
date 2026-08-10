"""設備知識庫：Demo Equipment Manual、SOP 與 Maintenance History（皆為合成資料）。"""

from .corpus import (
    MAINTENANCE_HISTORY,
    MANUALS,
    MaintenanceCase,
    ManualDoc,
    manual_by_ref,
)
from .retriever import KnowledgeBase, RetrievedChunk

__all__ = [
    "MANUALS",
    "MAINTENANCE_HISTORY",
    "ManualDoc",
    "MaintenanceCase",
    "manual_by_ref",
    "KnowledgeBase",
    "RetrievedChunk",
]
