"""稽核軌跡（Audit Trail）。

治理層的要求：每一個 Agent 判斷、每一次 Policy 裁決、每一個人工核准與每一次
Simulator 狀態變更都要留下不可省略的紀錄，格式為一行一筆 JSONL，方便事後重播。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings, get_settings


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuditRecord:
    ts: str
    run_id: str
    stage: str
    actor: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "run_id": self.run_id,
            "stage": self.stage,
            "actor": self.actor,
            "detail": self.detail,
        }


class AuditLog:
    """執行緒安全的 JSONL 稽核記錄器，同時保留記憶體副本供 API / Dashboard 讀取。"""

    def __init__(self, settings: Settings | None = None, run_id: str | None = None, persist: bool = True):
        self.settings = settings or get_settings()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.run_id = run_id or f"fg-{stamp}-{uuid.uuid4().hex[:6]}"
        self.persist = persist
        self.records: list[AuditRecord] = []
        self._lock = threading.Lock()
        self._path: Path | None = None
        if persist:
            self.settings.audit_dir.mkdir(parents=True, exist_ok=True)
            self._path = self.settings.audit_dir / f"audit-{self.run_id}.jsonl"

    @property
    def path(self) -> Path | None:
        return self._path

    def log(self, stage: str, actor: str, **detail: Any) -> AuditRecord:
        record = AuditRecord(ts=utc_now_iso(), run_id=self.run_id, stage=stage, actor=actor, detail=detail)
        with self._lock:
            self.records.append(record)
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record.to_dict(), ensure_ascii=False, default=str) + "\n")
        return record

    def by_stage(self, stage: str) -> list[AuditRecord]:
        return [r for r in self.records if r.stage == stage]

    def to_list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.records]


class NullAuditLog(AuditLog):
    """單元測試用：不落地、不建立目錄。"""

    def __init__(self) -> None:
        super().__init__(persist=False)


def load_audit(path: str | Path) -> list[dict[str, Any]]:
    """讀回 JSONL 稽核檔。"""
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def summarize_audit(records: list[dict[str, Any]]) -> dict[str, Any]:
    """把稽核檔壓成一個可以直接印在 CLI 的摘要。"""
    stages: dict[str, int] = {}
    actors: dict[str, int] = {}
    for rec in records:
        stages[rec.get("stage", "?")] = stages.get(rec.get("stage", "?"), 0) + 1
        actors[rec.get("actor", "?")] = actors.get(rec.get("actor", "?"), 0) + 1
    return {
        "records": len(records),
        "first_ts": records[0]["ts"] if records else None,
        "last_ts": records[-1]["ts"] if records else None,
        "stages": stages,
        "actors": actors,
    }


__all__ = [
    "AuditLog",
    "AuditRecord",
    "NullAuditLog",
    "load_audit",
    "summarize_audit",
    "utc_now_iso",
]
