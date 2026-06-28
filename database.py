# =========================================================
# database.py — 서버(길드)별 SQLite 파일 저장 계층 (로컬 전용)
#   data/<서버이름>_<서버ID>.db  형태로 서버마다 파일이 따로 생긴다.
#   - 같은 guild_id 면 이름이 바뀌어도 ID로 기존 파일을 재사용.
#   - 전역 상태(수집 on/off)는 data/state.json 에 별도 저장.
# =========================================================
import sqlite3
import os
import re
import glob
import json
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

_lock = threading.Lock()
_guild_dbs = {}     # guild_id -> GuildDB

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id         INTEGER PRIMARY KEY,
    guild_id           INTEGER,
    channel_id         INTEGER,
    thread_id          INTEGER,
    author_id          TEXT,
    author_name        TEXT,
    is_bot             INTEGER DEFAULT 0,
    content            TEXT,
    created_at         TEXT,
    edited_at          TEXT,
    length             INTEGER DEFAULT 0,
    is_reply           INTEGER DEFAULT 0,
    replied_to_id      INTEGER,
    has_attachment     INTEGER DEFAULT 0,
    attachment_count   INTEGER DEFAULT 0,
    has_link           INTEGER DEFAULT 0,
    link_domains       TEXT,
    mention_user_count INTEGER DEFAULT 0,
    mention_role_count INTEGER DEFAULT 0,
    deleted            INTEGER DEFAULT 0,
    deleted_at         TEXT,
    source             TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_channel ON messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_msg_author  ON messages(author_id);
CREATE INDEX IF NOT EXISTS idx_msg_created ON messages(created_at);

CREATE TABLE IF NOT EXISTS attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   INTEGER,
    filename     TEXT,
    ext          TEXT,
    content_type TEXT,
    size_bytes   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_att_msg ON attachments(message_id);

CREATE TABLE IF NOT EXISTS mentions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER,
    target_type TEXT,
    target_id   INTEGER,
    target_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_men_msg    ON mentions(message_id);
CREATE INDEX IF NOT EXISTS idx_men_target ON mentions(target_id);

CREATE TABLE IF NOT EXISTS reactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER,
    guild_id    INTEGER,
    channel_id  INTEGER,
    emoji       TEXT,
    user_id     TEXT,
    count       INTEGER DEFAULT 1,
    added_at    TEXT,
    source      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_react_live ON reactions(message_id, emoji, user_id) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_react_agg  ON reactions(message_id, emoji)          WHERE user_id IS NULL;

CREATE TABLE IF NOT EXISTS member_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER,
    user_id     TEXT,
    user_name   TEXT,
    event_type  TEXT,
    occurred_at TEXT
);

CREATE TABLE IF NOT EXISTS voice_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER,
    channel_id   INTEGER,
    channel_name TEXT,
    user_id      TEXT,
    user_name    TEXT,
    event_type   TEXT,
    occurred_at  TEXT
);

CREATE TABLE IF NOT EXISTS guild_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT,
    guild_id     INTEGER,
    guild_name   TEXT,
    member_count INTEGER
);

CREATE TABLE IF NOT EXISTS channel_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at   TEXT,
    guild_id      INTEGER,
    channel_id    INTEGER,
    channel_name  TEXT,
    channel_type  TEXT,
    category_name TEXT,
    topic         TEXT,
    position      INTEGER
);

CREATE TABLE IF NOT EXISTS role_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT,
    guild_id     INTEGER,
    role_id      INTEGER,
    role_name    TEXT,
    position     INTEGER,
    member_count INTEGER
);

CREATE TABLE IF NOT EXISTS channel_cursors (
    channel_id      INTEGER PRIMARY KEY,
    guild_id        INTEGER,
    last_message_id INTEGER,
    last_message_at TEXT
);
"""


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _sanitize(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "guild")
    name = name.strip().strip(".")          # 윈도우: 끝에 점/공백 금지
    return name[:60] or "guild"


def _file_for_guild(guild_id, guild_name):
    _ensure_data_dir()
    # 같은 guild_id 의 기존 파일이 있으면 재사용 (이름 바뀌어도 ID로 식별)
    existing = glob.glob(os.path.join(DATA_DIR, f"*_{guild_id}.db"))
    if existing:
        return existing[0]
    return os.path.join(DATA_DIR, f"{_sanitize(guild_name)}_{guild_id}.db")


class GuildDB:
    """서버 1개 = 파일 1개를 담당."""

    def __init__(self, guild_id, guild_name=None):
        self.guild_id = guild_id
        self.path = _file_for_guild(guild_id, guild_name)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ----- 메시지 -----
    def insert_message(self, rec):
        with _lock:
            cur = self.conn.execute("""
                INSERT OR IGNORE INTO messages
                (message_id, guild_id, channel_id, thread_id, author_id, author_name, is_bot,
                 content, created_at, edited_at, length, is_reply, replied_to_id,
                 has_attachment, attachment_count, has_link, link_domains,
                 mention_user_count, mention_role_count, source)
                VALUES
                (:message_id, :guild_id, :channel_id, :thread_id, :author_id, :author_name, :is_bot,
                 :content, :created_at, :edited_at, :length, :is_reply, :replied_to_id,
                 :has_attachment, :attachment_count, :has_link, :link_domains,
                 :mention_user_count, :mention_role_count, :source)
            """, rec)
            self.conn.commit()
            return cur.rowcount == 1

    def insert_attachments(self, rows):
        if not rows:
            return
        with _lock:
            self.conn.executemany(
                "INSERT INTO attachments (message_id, filename, ext, content_type, size_bytes) VALUES (?,?,?,?,?)",
                rows)
            self.conn.commit()

    def insert_mentions(self, rows):
        if not rows:
            return
        with _lock:
            self.conn.executemany(
                "INSERT INTO mentions (message_id, target_type, target_id, target_name) VALUES (?,?,?,?)",
                rows)
            self.conn.commit()

    def insert_reaction(self, rec):
        with _lock:
            self.conn.execute("""
                INSERT OR IGNORE INTO reactions
                (message_id, guild_id, channel_id, emoji, user_id, count, added_at, source)
                VALUES (:message_id, :guild_id, :channel_id, :emoji, :user_id, :count, :added_at, :source)
            """, rec)
            self.conn.commit()

    def mark_edited(self, message_id, content, edited_at):
        with _lock:
            self.conn.execute(
                "UPDATE messages SET content=?, edited_at=?, length=? WHERE message_id=?",
                (content, edited_at, len(content or ""), message_id))
            self.conn.commit()

    def mark_deleted(self, message_id, deleted_at):
        with _lock:
            self.conn.execute(
                "UPDATE messages SET deleted=1, deleted_at=? WHERE message_id=?",
                (deleted_at, message_id))
            self.conn.commit()

    # ----- 이벤트 -----
    def insert_member_event(self, row):
        with _lock:
            self.conn.execute(
                "INSERT INTO member_events (guild_id, user_id, user_name, event_type, occurred_at) VALUES (?,?,?,?,?)",
                row)
            self.conn.commit()

    def insert_voice_event(self, row):
        with _lock:
            self.conn.execute(
                "INSERT INTO voice_events (guild_id, channel_id, channel_name, user_id, user_name, event_type, occurred_at) VALUES (?,?,?,?,?,?,?)",
                row)
            self.conn.commit()

    # ----- 스냅샷 -----
    def insert_guild_snapshot(self, row):
        with _lock:
            self.conn.execute(
                "INSERT INTO guild_snapshots (captured_at, guild_id, guild_name, member_count) VALUES (?,?,?,?)",
                row)
            self.conn.commit()

    def insert_channel_snapshot(self, row):
        with _lock:
            self.conn.execute(
                "INSERT INTO channel_snapshots (captured_at, guild_id, channel_id, channel_name, channel_type, category_name, topic, position) VALUES (?,?,?,?,?,?,?,?)",
                row)
            self.conn.commit()

    def insert_role_snapshot(self, row):
        with _lock:
            self.conn.execute(
                "INSERT INTO role_snapshots (captured_at, guild_id, role_id, role_name, position, member_count) VALUES (?,?,?,?,?,?)",
                row)
            self.conn.commit()

    # ----- 커서 (gap-fill) -----
    def update_cursor(self, channel_id, guild_id, message_id, message_at):
        with _lock:
            row = self.conn.execute(
                "SELECT last_message_id FROM channel_cursors WHERE channel_id=?", (channel_id,)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO channel_cursors (channel_id, guild_id, last_message_id, last_message_at) VALUES (?,?,?,?)",
                    (channel_id, guild_id, message_id, message_at))
            elif message_id > (row["last_message_id"] or 0):
                self.conn.execute(
                    "UPDATE channel_cursors SET last_message_id=?, last_message_at=?, guild_id=? WHERE channel_id=?",
                    (message_id, message_at, guild_id, channel_id))
            self.conn.commit()

    def get_cursor(self, channel_id):
        return self.conn.execute(
            "SELECT * FROM channel_cursors WHERE channel_id=?", (channel_id,)).fetchone()

    # ----- 통계 (/상태) -----
    def stats(self):
        c = self.conn
        return {
            "messages":  c.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"],
            "reactions": c.execute("SELECT COUNT(*) n FROM reactions").fetchone()["n"],
            "members":   c.execute("SELECT COUNT(*) n FROM member_events").fetchone()["n"],
            "voice":     c.execute("SELECT COUNT(*) n FROM voice_events").fetchone()["n"],
            "deleted":   c.execute("SELECT COUNT(*) n FROM messages WHERE deleted=1").fetchone()["n"],
            "channels":  c.execute("SELECT COUNT(*) n FROM channel_cursors").fetchone()["n"],
            "last_at":   c.execute("SELECT MAX(created_at) m FROM messages").fetchone()["m"],
        }


def for_guild(guild_id, guild_name=None):
    """guild_id 에 해당하는 GuildDB 반환 (없으면 파일 생성)."""
    with _lock:
        gdb = _guild_dbs.get(guild_id)
    if gdb is None:
        gdb = GuildDB(guild_id, guild_name)
        with _lock:
            _guild_dbs[guild_id] = gdb
    return gdb


def all_guild_dbs():
    with _lock:
        return list(_guild_dbs.values())


def open_existing_guild_files():
    """data/ 의 기존 *_<id>.db 들을 미리 로드 (재시작 시 커서/통계 복구)."""
    _ensure_data_dir()
    for path in glob.glob(os.path.join(DATA_DIR, "*_*.db")):
        m = re.search(r"_(\d+)\.db$", os.path.basename(path))
        if m:
            for_guild(int(m.group(1)))


def init():
    _ensure_data_dir()


# ----- 전역 상태 (수집 on/off 등) : JSON -----
def get_state(key, default=None):
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get(key, default)
    except Exception:
        return default


def set_state(key, value):
    _ensure_data_dir()
    data = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data[key] = value
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
