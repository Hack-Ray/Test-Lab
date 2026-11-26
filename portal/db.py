"""SQLite：只存執行期狀態（分配紀錄、延長紀錄、稽核、可在 UI 改的設定）。

銀行、使用者、VM 清單以設定檔為準。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS allocations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    vm          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    bank        TEXT NOT NULL,
    email       TEXT NOT NULL,
    status      TEXT NOT NULL,          -- starting | ready | failed | released
    created_at  TEXT NOT NULL,
    ready_at    TEXT,
    ended_at    TEXT,
    note        TEXT,
    rdp_password TEXT                   -- client 分配的一次性密碼
);
CREATE INDEX IF NOT EXISTS ix_alloc_status ON allocations(status);

CREATE TABLE IF NOT EXISTS extensions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,          -- YYYY-MM-DD（Portal 時區）
    until       TEXT NOT NULL,          -- HH:MM
    email       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    email       TEXT,
    action      TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS users (
    email       TEXT PRIMARY KEY,
    role        TEXT NOT NULL,
    banks       TEXT NOT NULL,          -- JSON 陣列，"*" = 全部
    enabled     INTEGER NOT NULL DEFAULT 1,
    note        TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);

CREATE TABLE IF NOT EXISTS access_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL,
    role        TEXT NOT NULL,
    banks       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL,          -- pending | approved | rejected
    created_at  TEXT NOT NULL,
    decided_by  TEXT,
    decided_at  TEXT,
    decision_note TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        """舊資料庫補欄位。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(allocations)")}
        if "rdp_password" not in cols:
            self.conn.execute("ALTER TABLE allocations ADD COLUMN rdp_password TEXT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def get_setting(self, key: str) -> Any:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
