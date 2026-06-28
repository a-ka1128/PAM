# =========================================================
# db.py — 리마인더 추적 항목 저장 (로컬 SQLite, 재시작해도 유지)
# =========================================================
import sqlite3
import os
import threading
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "reminder.db")

_conn = None
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER,
    channel_id       INTEGER,
    message_id       INTEGER UNIQUE,
    jump_url         TEXT,
    preview          TEXT,
    flagged_by       INTEGER,
    target_ids       TEXT,   -- JSON list of user ids
    target_names     TEXT,   -- JSON list of display names
    priority         TEXT,   -- high/medium/low
    created_at       TEXT,
    last_reminded_at TEXT,
    remind_count     INTEGER DEFAULT 0,
    status           TEXT DEFAULT 'open',   -- open/responded/done/cancelled
    resolved_at      TEXT,
    resolved_by      INTEGER,
    resolve_type     TEXT
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def init():
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL;")
    _conn.executescript(SCHEMA)
    _conn.commit()


def add_item(rec):
    """신규면 INSERT(True), 이미 추적 중이면 우선순위만 갱신(False)."""
    with _lock:
        ex = _conn.execute("SELECT id FROM tracked WHERE message_id=?", (rec["message_id"],)).fetchone()
        if ex:
            _conn.execute(
                "UPDATE tracked SET priority=?, status='open', resolved_at=NULL, resolved_by=NULL, resolve_type=NULL "
                "WHERE message_id=?", (rec["priority"], rec["message_id"]))
            _conn.commit()
            return False
        _conn.execute("""
            INSERT INTO tracked
            (guild_id, channel_id, message_id, jump_url, preview, flagged_by,
             target_ids, target_names, priority, created_at)
            VALUES (:guild_id,:channel_id,:message_id,:jump_url,:preview,:flagged_by,
             :target_ids,:target_names,:priority,:created_at)
        """, rec)
        _conn.commit()
        return True


def is_tracked(message_id):
    return _conn.execute(
        "SELECT 1 FROM tracked WHERE message_id=? AND status='open'", (message_id,)).fetchone() is not None


def resolve(message_id, status, by, rtype):
    with _lock:
        _conn.execute(
            "UPDATE tracked SET status=?, resolved_at=?, resolved_by=?, resolve_type=? "
            "WHERE message_id=? AND status='open'", (status, _now(), by, rtype, message_id))
        _conn.commit()


def resolve_by_id(item_id, status, by, rtype):
    with _lock:
        cur = _conn.execute(
            "UPDATE tracked SET status=?, resolved_at=?, resolved_by=?, resolve_type=? "
            "WHERE id=? AND status='open'", (status, _now(), by, rtype, item_id))
        _conn.commit()
        return cur.rowcount > 0


def open_items():
    return _conn.execute(
        "SELECT * FROM tracked WHERE status='open' "
        "ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at"
    ).fetchall()


def mark_reminded(item_id):
    with _lock:
        _conn.execute(
            "UPDATE tracked SET last_reminded_at=?, remind_count=remind_count+1 WHERE id=?",
            (_now(), item_id))
        _conn.commit()
