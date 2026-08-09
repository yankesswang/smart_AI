"""不可否認稽核軌跡：雜湊鏈 JSONL。

閉環的每一步（觀測、影響、規劃、推演、政策、核准、執行、驗證）都留下一筆紀錄，
每筆包含前一筆的雜湊值 —— 任何事後竄改都會讓鏈結斷裂，可被驗證。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev_hash = self._tail_hash()
        self._lock = threading.Lock()

    def _tail_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        # 稽核檔可能長期累積；初始化只需讀最後一筆，不必線性掃描整份檔案。
        with self.path.open("rb") as fh:
            position = fh.seek(0, 2)
            buffer = b""
            while position:
                size = min(8192, position)
                position -= size
                fh.seek(position)
                buffer = fh.read(size) + buffer
                stripped = buffer.rstrip(b"\r\n")
                if position == 0 or b"\n" in stripped:
                    if not stripped:
                        return GENESIS
                    line = stripped.rsplit(b"\n", 1)[-1].strip()
                    return json.loads(line.decode("utf-8")).get("hash", GENESIS)
        return GENESIS

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def record(self, stage: str, actor: str, detail: dict[str, Any]) -> dict[str, Any]:
        # 同一 AuditLog 被多執行緒使用時，也必須維持 prev_hash 與寫入的原子順序。
        with self._lock:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "stage": stage,
                "actor": actor,
                "detail": detail,
                "prev_hash": self._prev_hash,
            }
            entry["hash"] = self._digest(entry)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self._prev_hash = entry["hash"]
            return entry

    def iter_entries(self) -> Iterator[dict[str, Any]]:
        """逐筆讀取稽核資料，供驗證與大型檔案統計使用。"""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def entries(self) -> list[dict[str, Any]]:
        return list(self.iter_entries())

    def count(self) -> int:
        return sum(1 for _ in self.iter_entries())

    def verify(self) -> tuple[bool, str]:
        """重算整條雜湊鏈，確認軌跡未被竄改。"""
        prev = GENESIS
        for idx, entry in enumerate(self.iter_entries(), start=1):
            if entry.get("prev_hash") != prev:
                return False, f"第 {idx} 筆的 prev_hash 與前一筆不符"
            recomputed = self._digest({k: v for k, v in entry.items() if k != "hash"})
            if recomputed != entry.get("hash"):
                return False, f"第 {idx} 筆內容與雜湊值不符（已遭竄改）"
            prev = entry["hash"]
        return True, "稽核鏈完整"
