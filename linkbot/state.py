# linkbot/state.py — 추적 상태 영속(SQLite). 봇 재시작해도 추적/플래그/마지막확인 유지.
import os
import time
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
    # ── 스냅샷 레지스트리 (전체일정 diff 중복제거) ──
    c.execute("""CREATE TABLE IF NOT EXISTS snap_reg (
        channel_id        INTEGER NOT NULL,
        row_key           TEXT    NOT NULL,
        src_mid           INTEGER NOT NULL,
        canon             TEXT    NOT NULL,
        task_text         TEXT    NOT NULL,
        date_text         TEXT    NOT NULL DEFAULT '',
        status            TEXT    NOT NULL DEFAULT '진행중',
        managed           INTEGER NOT NULL DEFAULT 0,
        missing_count     INTEGER NOT NULL DEFAULT 0,
        last_missing_date TEXT    NOT NULL DEFAULT '',
        last_seen_snap    INTEGER NOT NULL DEFAULT 0,
        completed_by      TEXT    NOT NULL DEFAULT '',
        prev_missing      INTEGER NOT NULL DEFAULT 0,
        prev_status       TEXT    NOT NULL DEFAULT '',
        prev_missing_date TEXT    NOT NULL DEFAULT '',
        updated_at        REAL    NOT NULL,
        PRIMARY KEY (channel_id, row_key))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_snapreg_canon ON snap_reg(channel_id, canon)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_snapreg_src ON snap_reg(src_mid)")
    c.execute("""CREATE TABLE IF NOT EXISTS snap_meta (
        channel_id INTEGER PRIMARY KEY, last_snap_mid INTEGER NOT NULL DEFAULT 0,
        last_snap_ts REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS snap_msgs (
        mid INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, ts REAL NOT NULL,
        kind TEXT NOT NULL DEFAULT 'snap')""")   # kind: 'snap'|'alert'(자동완료 알림)
    c.execute("""CREATE TABLE IF NOT EXISTS sheet_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
        tab TEXT NOT NULL, row_key TEXT NOT NULL, created REAL NOT NULL,
        UNIQUE(kind, tab, row_key))""")          # kind: 'done'|'del'
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
#    스냅샷 메시지에서는 의미가 "신규 인덱스 high-water mark"로 확장됨 (연속 보장 X)
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


# ═══════════ 스냅샷 레지스트리 API (전체일정 diff 중복제거) ═══════════
_REG_COLS = ("channel_id,row_key,src_mid,canon,task_text,date_text,status,managed,"
             "missing_count,last_missing_date,last_seen_snap,completed_by,"
             "prev_missing,prev_status,prev_missing_date,updated_at")


def _row2d(r):
    return dict(zip(_REG_COLS.split(","), r))


def reg_channel_items(channel_id, active_only=False):
    """매칭 후보. 기본 = 완료 포함(재활성 정책이 완료 행을 봐야 함).
    정렬은 불변 키(src_mid,row_key) — replay 시 2단 프롬프트 후보 번호 재현성."""
    c = _con()
    q = f"SELECT {_REG_COLS} FROM snap_reg WHERE channel_id=?"
    if active_only:
        q += " AND status='진행중'"
    rows = c.execute(q + " ORDER BY src_mid, row_key", (channel_id,)).fetchall()
    c.close()
    return [_row2d(r) for r in rows]


def reg_find_by_canon_all(channel_id, canon, active_only=True):
    """canon 완전일치 전건 반환(fetchone 금지 — 쌍둥이 임의 병합 방지)."""
    c = _con()
    q = f"SELECT {_REG_COLS} FROM snap_reg WHERE channel_id=? AND canon=?"
    if active_only:
        q += " AND status='진행중'"
    rows = c.execute(q, (channel_id, canon)).fetchall()
    c.close()
    return [_row2d(r) for r in rows]


def reg_items_by_src(src_mid):
    c = _con()
    rows = c.execute(f"SELECT {_REG_COLS} FROM snap_reg WHERE src_mid=?", (int(src_mid),)).fetchall()
    c.close()
    return [_row2d(r) for r in rows]


def reg_meta(channel_id):
    c = _con()
    r = c.execute("SELECT last_snap_mid, last_snap_ts FROM snap_meta WHERE channel_id=?",
                  (channel_id,)).fetchone()
    c.close()
    return (r[0], r[1]) if r else (0, 0.0)


def is_snap_msg(mid):
    c = _con()
    r = c.execute("SELECT 1 FROM snap_msgs WHERE mid=? AND kind='snap'", (int(mid),)).fetchone()
    c.close()
    return r is not None


def snap_msg_channel(mid):
    """답장 라우팅용 — kind 불문(스냅샷+자동완료 알림 둘 다 레지스트리로 라우팅)."""
    c = _con()
    r = c.execute("SELECT channel_id FROM snap_msgs WHERE mid=?", (int(mid),)).fetchone()
    c.close()
    return r[0] if r else None


def snap_msg_add(mid, channel_id, kind="snap"):
    c = _con()
    c.execute("INSERT OR REPLACE INTO snap_msgs VALUES (?,?,?,?)",
              (int(mid), channel_id, time.time(), kind))
    c.commit(); c.close()


def reg_enroll(channel_id, row_key, canon, task_text, date_text="", status="진행중", managed=0):
    """비스냅샷 경로 편입. 재등록(edit)이면 canon도 재계산값으로 갱신 — missing류는 보존."""
    now = time.time()
    c = _con()
    c.execute("""INSERT INTO snap_reg (channel_id,row_key,src_mid,canon,task_text,date_text,status,
                   managed,missing_count,last_missing_date,last_seen_snap,completed_by,
                   prev_missing,prev_status,prev_missing_date,updated_at)
                 VALUES (?,?,?,?,?,?,?,?,0,'',0,'',0,'','',?)
                 ON CONFLICT(channel_id,row_key) DO UPDATE SET
                   canon=excluded.canon, task_text=excluded.task_text,
                   date_text=excluded.date_text, status=excluded.status,
                   updated_at=excluded.updated_at""",
              (channel_id, row_key, int(str(row_key).split("#")[0]), canon, task_text,
               date_text, status, int(managed), now))
    c.commit(); c.close()


def reg_delete(channel_id, row_keys):
    if not row_keys:
        return
    c = _con()
    c.executemany("DELETE FROM snap_reg WHERE channel_id=? AND row_key=?",
                  [(channel_id, k) for k in row_keys])
    c.commit(); c.close()


def reg_set_status(channel_id, row_keys, status, source):
    """답장 완료/부활 훅. status='완료'→completed_by=source, '진행중'→completed_by='' (부활)."""
    now = time.time()
    c = _con()
    c.executemany("""UPDATE snap_reg SET status=?, completed_by=?, updated_at=?
                     WHERE channel_id=? AND row_key=?""",
                  [(status, source if status == "완료" else "", now, channel_id, k)
                   for k in row_keys])
    c.commit(); c.close()


def reg_reset_channel(channel_id):
    """force 재구축용 초기화."""
    c = _con()
    c.execute("DELETE FROM snap_reg WHERE channel_id=?", (channel_id,))
    c.execute("DELETE FROM snap_meta WHERE channel_id=?", (channel_id,))
    c.execute("DELETE FROM snap_msgs WHERE channel_id=?", (channel_id,))
    c.commit(); c.close()


def reg_recanon(canon_fn, ver):
    """canon 규칙 버전업 시 전량 재계산 (on_ready에서 kv 'canon_ver' 불일치 시 1회)."""
    c = _con()
    rows = c.execute("SELECT channel_id,row_key,task_text FROM snap_reg").fetchall()
    for ch, rk, tt in rows:
        c.execute("UPDATE snap_reg SET canon=? WHERE channel_id=? AND row_key=?",
                  (canon_fn(tt), ch, rk))
    c.commit(); c.close()
    set_kv("canon_ver", ver)


def outbox_pending(limit=50):
    c = _con()
    rows = c.execute("SELECT id, kind, tab, row_key FROM sheet_outbox ORDER BY id LIMIT ?",
                     (limit,)).fetchall()
    c.close()
    return [{"id": r[0], "kind": r[1], "tab": r[2], "row_key": r[3]} for r in rows]


def outbox_del(ids):
    if not ids:
        return
    c = _con()
    c.executemany("DELETE FROM sheet_outbox WHERE id=?", [(i,) for i in ids])
    c.commit(); c.close()


def reg_apply_snapshot(channel_id, snap_mid, snap_kst_date, matched, new_items, allow_missing=True):
    """스냅샷 diff를 단일 트랜잭션으로 적용.
    matched=[{row_key,task_text,date_text,status}] (재활성 정책 적용 후 최종값 — bot이 계산)
    new_items=[{row_key,canon,task_text,date_text,status}] (row_key는 bot이 HW 인덱스로 부여)
    반환 {"mode":'new'|'replay'|'stale', "auto_completed":[...], "deleted_new":[...]}"""
    snap_mid = int(snap_mid)
    now = time.time()
    matched_keys = {m["row_key"] for m in matched}
    new_keys = {n["row_key"] for n in new_items}
    if matched_keys & new_keys:
        raise ValueError(f"snapshot key collision: {matched_keys & new_keys}")
    c = _con()
    c.isolation_level = None                     # 명시적 BEGIN/COMMIT 제어
    try:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT last_snap_mid FROM snap_meta WHERE channel_id=?",
                      (channel_id,)).fetchone()
        last = r[0] if r else 0
        if snap_mid < last:
            c.execute("ROLLBACK"); c.close()
            return {"mode": "stale", "auto_completed": [], "deleted_new": []}
        mode = "new" if snap_mid > last else "replay"
        deleted_new = []
        if mode == "new":
            c.execute("""UPDATE snap_reg SET prev_missing=missing_count, prev_status=status,
                         prev_missing_date=last_missing_date WHERE channel_id=?""", (channel_id,))
        else:   # replay: 되감기 (답장 완료 보호) 후 재계산
            c.execute("""UPDATE snap_reg SET missing_count=prev_missing,
                         status=CASE WHEN completed_by LIKE 'reply:%' THEN status ELSE prev_status END,
                         last_missing_date=prev_missing_date,
                         completed_by=CASE WHEN completed_by=? THEN '' ELSE completed_by END
                         WHERE channel_id=? AND prev_status != ''""",
                      (f"snap:{snap_mid}", channel_id))
            olds = c.execute("SELECT row_key, status FROM snap_reg WHERE channel_id=? AND src_mid=?",
                             (channel_id, snap_mid)).fetchall()
            deleted_new = [k for k, st in olds
                           if k not in new_keys and k not in matched_keys and st != "완료"]
            if deleted_new:
                c.executemany("DELETE FROM snap_reg WHERE channel_id=? AND row_key=?",
                              [(channel_id, k) for k in deleted_new])
        for m in matched:
            c.execute("""UPDATE snap_reg SET task_text=?, date_text=?, status=?, managed=1,
                         missing_count=0, last_seen_snap=?, updated_at=?,
                         completed_by=CASE WHEN ?='진행중' THEN '' ELSE completed_by END
                         WHERE channel_id=? AND row_key=?""",
                      (m["task_text"], m["date_text"], m["status"], snap_mid, now,
                       m["status"], channel_id, m["row_key"]))
        auto_completed = []
        if allow_missing:
            keep = matched_keys | new_keys
            ph = ",".join("?" * len(keep)) if keep else "''"
            c.execute(f"""UPDATE snap_reg SET missing_count=missing_count+1,
                          last_missing_date=?, updated_at=?
                          WHERE channel_id=? AND managed=1 AND status='진행중'
                            AND last_missing_date != ? AND row_key NOT IN ({ph})""",
                      (snap_kst_date, now, channel_id, snap_kst_date, *keep))
            rows = c.execute("""SELECT row_key FROM snap_reg WHERE channel_id=? AND managed=1
                                AND status='진행중' AND missing_count>=2""", (channel_id,)).fetchall()
            auto_completed = [x[0] for x in rows]
            if auto_completed:
                c.executemany("""UPDATE snap_reg SET status='완료', completed_by=?, updated_at=?
                                 WHERE channel_id=? AND row_key=?""",
                              [(f"snap:{snap_mid}", now, channel_id, k) for k in auto_completed])
        for n in new_items:
            c.execute("""INSERT INTO snap_reg (channel_id,row_key,src_mid,canon,task_text,date_text,
                          status,managed,missing_count,last_missing_date,last_seen_snap,completed_by,
                          prev_missing,prev_status,prev_missing_date,updated_at)
                         VALUES (?,?,?,?,?,?,?,1,0,'',?,'',0,?,'',?)""",
                      (channel_id, n["row_key"], snap_mid, n["canon"], n["task_text"],
                       n["date_text"], n["status"], snap_mid, n["status"], now))
        for k in auto_completed:
            c.execute("INSERT OR IGNORE INTO sheet_outbox (kind,tab,row_key,created) VALUES ('done','일정',?,?)", (k, now))
        for k in deleted_new:
            c.execute("INSERT OR IGNORE INTO sheet_outbox (kind,tab,row_key,created) VALUES ('del','일정',?,?)", (k, now))
        c.execute("""INSERT INTO snap_meta VALUES (?,?,?,?)
                     ON CONFLICT(channel_id) DO UPDATE SET last_snap_mid=excluded.last_snap_mid,
                       last_snap_ts=excluded.last_snap_ts, updated_at=excluded.updated_at""",
                  (channel_id, snap_mid, now, now))
        c.execute("INSERT OR REPLACE INTO snap_msgs VALUES (?,?,?,'snap')",
                  (snap_mid, channel_id, now))
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        finally:
            c.close()
        raise
    c.close()
    return {"mode": mode, "auto_completed": auto_completed, "deleted_new": deleted_new}


def reg_revoke_snapshot(channel_id, snap_mid):
    """최신 스냅샷 edit-철회: prev_* 복원 + 자기 신규행 제거(완료 행은 보존).
    snap_meta는 유지(더 오래된 스냅샷 역주입 차단). snap_msgs도 유지(잔여 답장 라우팅).
    반환: 시트에서 지울 row_key 목록(완료 제외)."""
    snap_mid = int(snap_mid)
    c = _con()
    c.isolation_level = None
    c.execute("BEGIN IMMEDIATE")
    c.execute("""UPDATE snap_reg SET missing_count=prev_missing,
                 status=CASE WHEN completed_by LIKE 'reply:%' THEN status ELSE prev_status END,
                 last_missing_date=prev_missing_date,
                 completed_by=CASE WHEN completed_by=? THEN '' ELSE completed_by END
                 WHERE channel_id=? AND prev_status != ''""", (f"snap:{snap_mid}", channel_id))
    olds = c.execute("SELECT row_key, status FROM snap_reg WHERE channel_id=? AND src_mid=?",
                     (channel_id, snap_mid)).fetchall()
    keys = [k for k, st in olds if st != "완료"]
    if keys:
        c.executemany("DELETE FROM snap_reg WHERE channel_id=? AND row_key=?",
                      [(channel_id, k) for k in keys])
    c.execute("COMMIT"); c.close()
    return keys
