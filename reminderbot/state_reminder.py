# reminderbot/state_reminder.py — 리마인더봇 추적상태 영속(SQLite). 재시작해도 추적 유지.
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_reminder.db")


def _con():
    c = sqlite3.connect(DB, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS tracked ("
              "mid INTEGER PRIMARY KEY, channel_id INTEGER, interval INTEGER, "
              "last REAL, author_id INTEGER, emoji TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS done_snap (rowkey TEXT PRIMARY KEY)")
    return c


def load_tracked():
    c = _con()
    rows = c.execute("SELECT mid, channel_id, interval, last, author_id, emoji FROM tracked").fetchall()
    c.close()
    return {r[0]: {"channel_id": r[1], "interval": r[2], "last": r[3],
                   "author_id": r[4], "emoji": r[5]} for r in rows}


def save_tracked(mid, t):
    c = _con()
    c.execute("INSERT OR REPLACE INTO tracked VALUES (?,?,?,?,?,?)",
              (mid, t["channel_id"], t["interval"], t["last"], t["author_id"], t["emoji"]))
    c.commit(); c.close()


def del_tracked(mid):
    c = _con(); c.execute("DELETE FROM tracked WHERE mid=?", (mid,)); c.commit(); c.close()


# ── 다이제스트용 kv / 완료행 스냅샷 ─────────────────────────────
def get_kv(k, default=None):
    c = _con()
    row = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    c.close()
    return row[0] if row else default


def set_kv(k, v):
    c = _con()
    c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, str(v)))
    c.commit(); c.close()


def get_done_snapshot():
    """어제(직전 브리핑 시점) '완료' 상태였던 행 _key 집합."""
    c = _con()
    rows = c.execute("SELECT rowkey FROM done_snap").fetchall()
    c.close()
    return set(r[0] for r in rows)


def set_done_snapshot(keys):
    """완료행 _key 집합을 통째로 교체(다음 브리핑의 기준선)."""
    c = _con()
    c.execute("DELETE FROM done_snap")
    c.executemany("INSERT OR IGNORE INTO done_snap VALUES (?)", [(str(k),) for k in keys])
    c.commit(); c.close()
