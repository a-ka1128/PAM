# linkbot/state.py — 추적 상태 영속(SQLite). 봇 재시작해도 추적/플래그/마지막확인 유지.
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.db")


def _con():
    c = sqlite3.connect(DB, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS tracked "
              "(mid INTEGER PRIMARY KEY, channel_id INTEGER, interval INTEGER, last REAL, author_id INTEGER, emoji TEXT, task TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS flagged (mid INTEGER PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    try:
        c.execute("ALTER TABLE tracked ADD COLUMN task TEXT")   # 기존 DB 보강 (완료 키워드 매칭용)
    except sqlite3.OperationalError:
        pass
    return c


def load_tracked():
    c = _con()
    rows = c.execute("SELECT mid, channel_id, interval, last, author_id, emoji FROM tracked").fetchall()
    c.close()
    return {r[0]: {"channel_id": r[1], "interval": r[2], "last": r[3], "author_id": r[4], "emoji": r[5]} for r in rows}


def save_tracked(mid, t):
    c = _con()
    c.execute("INSERT OR REPLACE INTO tracked VALUES (?,?,?,?,?,?)",
              (mid, t["channel_id"], t["interval"], t["last"], t["author_id"], t["emoji"]))
    c.commit(); c.close()


def del_tracked(mid):
    c = _con(); c.execute("DELETE FROM tracked WHERE mid=?", (mid,)); c.commit(); c.close()


def load_flagged():
    c = _con(); rows = c.execute("SELECT mid FROM flagged").fetchall(); c.close()
    return set(r[0] for r in rows)


def add_flagged(mid):
    c = _con(); c.execute("INSERT OR REPLACE INTO flagged VALUES (?)", (mid,)); c.commit(); c.close()


def del_flagged(mid):
    c = _con(); c.execute("DELETE FROM flagged WHERE mid=?", (mid,)); c.commit(); c.close()


def get_kv(k, default=None):
    c = _con(); r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone(); c.close()
    return r[0] if r else default


def set_kv(k, v):
    c = _con(); c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, str(v))); c.commit(); c.close()


def del_kv(k):
    c = _con(); c.execute("DELETE FROM kv WHERE k=?", (k,)); c.commit(); c.close()


# ── 메시지별 일정 건수(item_count) 영속 — 답장 N 판정·수정 reconcile용 ──
def set_msg_item_count(mid, n):
    set_kv(f"sched_n:{int(mid)}", int(n))


def get_msg_item_count(mid):
    v = get_kv(f"sched_n:{int(mid)}")
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def del_msg_item_count(mid):
    del_kv(f"sched_n:{int(mid)}")
