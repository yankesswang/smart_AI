"""SQLite 持久化層。

正式平台與 Demo 的關鍵差別之一：重啟之後資料還在。告警、工單、
使用者、稽核軌跡都寫進 SQLite，不再只活在記憶體裡。

設計選擇：
- 只用標準函式庫的 ``sqlite3``，不引入 ORM。這一層的查詢形態固定，
  ORM 帶來的抽象成本高於收益，而且能保持「零額外相依」。
- 連線用 thread-local。閉環跑在工作執行緒、API 在另一批執行緒，
  共用同一個 connection 會踩到 sqlite 的 thread affinity。
- 每張表的 schema 版本由 ``_MIGRATIONS`` 線性管理，啟動時自動補齊。
- 時間一律存 ISO-8601 UTC 字串，讓 SQLite 的字典序等同時間序。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2


def utcnow() -> str:
    """平台內所有時間戳的唯一產生點，確保格式一致。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------
# 每個項目是一個 migration step；索引即版本號。只能往後追加，不可修改既有項目。
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        # ---------------------------------------------------------------- 站點
        """
        CREATE TABLE IF NOT EXISTS sites (
            site_id       TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            region        TEXT NOT NULL DEFAULT '',
            timezone      TEXT NOT NULL DEFAULT 'Asia/Taipei',
            adapter_kind  TEXT NOT NULL DEFAULT 'simulated',
            adapter_config TEXT NOT NULL DEFAULT '{}',
            enabled       INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
        """,
        # ---------------------------------------------------------------- 告警
        # 正式平台的告警有生命週期：open → acknowledged → resolved / closed。
        # dedup_key 讓同一個持續異常不會每個 tick 都新增一筆。
        """
        CREATE TABLE IF NOT EXISTS alarms (
            alarm_id      TEXT PRIMARY KEY,
            site_id       TEXT NOT NULL,
            machine_id    TEXT NOT NULL DEFAULT '',
            dedup_key     TEXT NOT NULL,
            severity      TEXT NOT NULL,
            state         TEXT NOT NULL DEFAULT 'open',
            title         TEXT NOT NULL,
            detail        TEXT NOT NULL DEFAULT '',
            source        TEXT NOT NULL DEFAULT 'monitoring',
            evidence      TEXT NOT NULL DEFAULT '{}',
            raised_at     TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            acked_at      TEXT,
            acked_by      TEXT,
            resolved_at   TEXT,
            resolved_by   TEXT,
            resolution    TEXT NOT NULL DEFAULT '',
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (site_id) REFERENCES sites(site_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_alarms_site_state ON alarms(site_id, state)",
        "CREATE INDEX IF NOT EXISTS idx_alarms_raised ON alarms(raised_at DESC)",
        # 同一站點同一 dedup_key 只允許一筆「未關閉」告警。
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_alarms_active_dedup
            ON alarms(site_id, dedup_key)
            WHERE state IN ('open', 'acknowledged')
        """,
        # ---------------------------------------------------------------- 工單
        """
        CREATE TABLE IF NOT EXISTS work_orders (
            work_order_id TEXT PRIMARY KEY,
            site_id       TEXT NOT NULL,
            machine_id    TEXT NOT NULL DEFAULT '',
            alarm_id      TEXT,
            kind          TEXT NOT NULL DEFAULT 'corrective',
            priority      TEXT NOT NULL DEFAULT 'normal',
            state         TEXT NOT NULL DEFAULT 'open',
            title         TEXT NOT NULL,
            detail        TEXT NOT NULL DEFAULT '',
            assignee      TEXT NOT NULL DEFAULT '',
            estimated_min REAL NOT NULL DEFAULT 0,
            created_by    TEXT NOT NULL DEFAULT 'system',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            started_at    TEXT,
            completed_at  TEXT,
            completion_note TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (site_id) REFERENCES sites(site_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_wo_site_state ON work_orders(site_id, state)",
        "CREATE INDEX IF NOT EXISTS idx_wo_created ON work_orders(created_at DESC)",
        # ---------------------------------------------------------------- 使用者
        """
        CREATE TABLE IF NOT EXISTS users (
            username      TEXT PRIMARY KEY,
            display_name  TEXT NOT NULL,
            role          TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            enabled       INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL,
            last_login_at TEXT
        )
        """,
        # 登入 token。正式部署可換成 JWT，但 server-side session 讓「立即撤銷」成立。
        """
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token       TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            FOREIGN KEY (username) REFERENCES users(username)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_auth_expiry ON auth_sessions(expires_at)",
        # ---------------------------------------------------------------- 稽核
        # api.audit 的 JSONL 是「單次 run 的推理軌跡」；這張表是「平台上誰做了什麼」。
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            actor       TEXT NOT NULL,
            actor_role  TEXT NOT NULL DEFAULT '',
            action      TEXT NOT NULL,
            site_id     TEXT NOT NULL DEFAULT '',
            target      TEXT NOT NULL DEFAULT '',
            outcome     TEXT NOT NULL DEFAULT 'ok',
            detail      TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts DESC)",
        "CREATE INDEX IF NOT EXISTS idx_audit_site ON audit_events(site_id, ts DESC)",
        # ---------------------------------------------------------------- 班別交接
        """
        CREATE TABLE IF NOT EXISTS shift_handovers (
            handover_id TEXT PRIMARY KEY,
            site_id     TEXT NOT NULL,
            shift       TEXT NOT NULL,
            author      TEXT NOT NULL,
            summary     TEXT NOT NULL,
            open_items  TEXT NOT NULL DEFAULT '[]',
            created_at  TEXT NOT NULL,
            FOREIGN KEY (site_id) REFERENCES sites(site_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_handover_site ON shift_handovers(site_id, created_at DESC)",
        # ---------------------------------------------------------------- KPI 歷史
        # 讓「歷史查詢」與趨勢圖不依賴記憶體中的 session。
        """
        CREATE TABLE IF NOT EXISTS kpi_samples (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id     TEXT NOT NULL,
            ts          TEXT NOT NULL,
            tick        INTEGER NOT NULL DEFAULT 0,
            factory_health   REAL NOT NULL DEFAULT 0,
            production_pct   REAL NOT NULL DEFAULT 0,
            max_delay_min    REAL NOT NULL DEFAULT 0,
            hazard_exposure_min REAL NOT NULL DEFAULT 0,
            open_alarms      INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (site_id) REFERENCES sites(site_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_kpi_site_ts ON kpi_samples(site_id, ts DESC)",
    ),
    (
        # v2：閉環工單去重。
        # Agent 的 work_order_id（WO-{tick}-{seq}）在站點重建後會重複，
        # 需要一個含站點與問題內容的來源鍵，才能可靠判斷「這張工單已經建過了」。
        "ALTER TABLE work_orders ADD COLUMN source_key TEXT NOT NULL DEFAULT ''",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wo_source_key
            ON work_orders(source_key) WHERE source_key <> ''
        """,
    ),
)


class Store:
    """平台的 SQLite 資料庫控制點。

    所有讀寫都經過這裡，讓交易邊界、時間戳格式與 JSON 欄位序列化
    只有一個實作，不會散落在各個 service。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # 寫入序列化：SQLite 允許多讀單寫，用一把鎖把寫入排隊，
        # 比讓呼叫端各自處理 "database is locked" 重試簡單也可靠。
        self._write_lock = threading.RLock()
        self._migrate()

    # ------------------------------------------------------------------ 連線
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            # WAL：讓讀取不被寫入阻塞，Dashboard 輪詢時不會卡住閉環寫入。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """單一寫入交易。例外時 rollback，避免半套資料。"""
        with self._write_lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # ------------------------------------------------------------------ Migration
    def _migrate(self) -> None:
        conn = self.conn
        conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
        row = conn.execute("SELECT version FROM schema_meta").fetchone()
        current = int(row["version"]) if row else 0
        if current >= len(_MIGRATIONS):
            return
        with self.write() as w:
            for statements in _MIGRATIONS[current:]:
                for sql in statements:
                    w.execute(sql)
            w.execute("DELETE FROM schema_meta")
            w.execute("INSERT INTO schema_meta (version) VALUES (?)", (len(_MIGRATIONS),))

    # ------------------------------------------------------------------ 查詢輔助
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        if row is None:
            return default
        value = row[0]
        return default if value is None else value


# --------------------------------------------------------------------------------------
# JSON 欄位輔助
# --------------------------------------------------------------------------------------
def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def load_json(raw: Any, fallback: Any = None) -> Any:
    if raw in (None, ""):
        return {} if fallback is None else fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {} if fallback is None else fallback


__all__ = ["SCHEMA_VERSION", "Store", "dump_json", "load_json", "utcnow"]
