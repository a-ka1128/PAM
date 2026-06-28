# reminderbot/state_reminder.py — 리마인더봇 추적상태 영속(SQLite). 재시작해도 추적 유지.
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_reminder.db")


def _con():
    c = sqlite3.connect(DB, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS tracked ("
              "mid INTEGER PRIMARY KEY, channel_id INTEGER, interval INTEGER, "
              "last REAL, author_id INTEGER, emoji TEXT)")
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
