# -*- coding: utf-8 -*-
"""SQLite toi gian cho node: kv (api_key, boot_id, seq) + outbox (hang doi
gui khi mat mang toi edge). Cung mot mau voi edge_collector/store.py nhung
bo phan lich su (node khong can /api/stats)."""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seq_counter (name TEXT PRIMARY KEY, seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bid TEXT NOT NULL, seq INTEGER NOT NULL,
    payload TEXT NOT NULL, created_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._cx = sqlite3.connect(str(path), check_same_thread=False)
        self._cx.executescript(_SCHEMA)
        self._cx.commit()

    def kv_get(self, key: str, default=None):
        with self._lock:
            row = self._cx.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return row[0]

    def kv_set(self, key: str, value) -> None:
        raw = value if isinstance(value, str) else json.dumps(value)
        with self._lock:
            self._cx.execute(
                "INSERT INTO kv(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, raw))
            self._cx.commit()

    def next_seq(self) -> int:
        with self._lock:
            cur = self._cx.execute(
                "INSERT INTO seq_counter(name, seq) VALUES('main', 1) "
                "ON CONFLICT(name) DO UPDATE SET seq = seq_counter.seq + 1 RETURNING seq")
            seq = cur.fetchone()[0]
            self._cx.commit()
        return seq

    def outbox_push(self, bid: str, seq: int, payload: dict) -> None:
        with self._lock:
            self._cx.execute(
                "INSERT INTO outbox(bid, seq, payload, created_at) VALUES(?,?,?,?)",
                (bid, seq, json.dumps(payload), time.time()))
            self._cx.commit()

    def outbox_oldest(self) -> Optional[dict]:
        with self._lock:
            row = self._cx.execute(
                "SELECT id, bid, seq, payload FROM outbox ORDER BY id ASC LIMIT 1").fetchone()
        if not row:
            return None
        return {"id": row[0], "bid": row[1], "seq": row[2], "payload": json.loads(row[3])}

    def outbox_delete(self, row_id: int) -> None:
        with self._lock:
            self._cx.execute("DELETE FROM outbox WHERE id=?", (row_id,))
            self._cx.commit()

    def outbox_count(self) -> int:
        with self._lock:
            return self._cx.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
