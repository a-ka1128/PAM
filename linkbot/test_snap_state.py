# linkbot/test_snap_state.py — 스냅샷 레지스트리(state) 회귀 테스트 (임시 DB, LLM 불필요)
#   실행: venv\python.exe linkbot\test_snap_state.py
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state

state.DB = os.path.join(tempfile.gettempdir(), f"snaptest_{os.getpid()}.db")
if os.path.exists(state.DB):
    os.remove(state.DB)

passed = 0
failed = 0


def check(name, cond, got=""):
    global passed, failed
    if cond:
        passed += 1
        print("  PASS  " + name)
    else:
        failed += 1
        print("  FAIL  " + name + "   got=" + repr(got))


def by_key(items, k):
    return next((x for x in items if x["row_key"] == k), None)


CH = 1

# [1] 첫 스냅샷 — 신규 2건
r = state.reg_apply_snapshot(CH, 100, "2026-07-03", [], [
    {"row_key": "100#0", "canon": "a작업", "task_text": "A작업", "date_text": "", "status": "진행중"},
    {"row_key": "100#1", "canon": "b작업", "task_text": "B작업", "date_text": "", "status": "진행중"},
])
items = state.reg_channel_items(CH)
check("첫 스냅샷 new + 2행", r["mode"] == "new" and len(items) == 2, (r, len(items)))
check("meta 갱신", state.reg_meta(CH)[0] == 100, state.reg_meta(CH))
check("snap_msgs 등록", state.is_snap_msg(100) and state.snap_msg_channel(100) == CH, None)

# [2] 다음날 스냅샷 — A만 언급 → B missing 1
r = state.reg_apply_snapshot(CH, 200, "2026-07-04",
    [{"row_key": "100#0", "task_text": "A작업 v2", "date_text": "2026-07-20", "status": "진행중"}], [])
b = by_key(state.reg_channel_items(CH), "100#1")
a = by_key(state.reg_channel_items(CH), "100#0")
check("matched 갱신+missing0", a["task_text"] == "A작업 v2" and a["missing_count"] == 0 and a["managed"] == 1, a)
check("미언급 missing+1", b["missing_count"] == 1, b["missing_count"])

# [3] 같은 날 재게시 — 하루 1회 가드로 missing 불변
r = state.reg_apply_snapshot(CH, 201, "2026-07-04",
    [{"row_key": "100#0", "task_text": "A작업 v2", "date_text": "2026-07-20", "status": "진행중"}], [])
b = by_key(state.reg_channel_items(CH), "100#1")
check("같은날 missing 불변", b["missing_count"] == 1, b["missing_count"])

# [4] 다음날 또 미언급 → missing 2 → 자동완료 + outbox
r = state.reg_apply_snapshot(CH, 300, "2026-07-05",
    [{"row_key": "100#0", "task_text": "A작업 v2", "date_text": "2026-07-20", "status": "진행중"}], [])
b = by_key(state.reg_channel_items(CH), "100#1")
check("2일 연속 → 자동완료", "100#1" in r["auto_completed"] and b["status"] == "완료"
      and b["completed_by"] == "snap:300", (r["auto_completed"], b["status"], b["completed_by"]))
ob = state.outbox_pending()
check("outbox done 적재", any(o["kind"] == "done" and o["row_key"] == "100#1" for o in ob), ob)

# [5] replay 멱등 — 같은 스냅샷 재적용해도 상태 동일 + outbox 중복 없음
r = state.reg_apply_snapshot(CH, 300, "2026-07-05",
    [{"row_key": "100#0", "task_text": "A작업 v2", "date_text": "2026-07-20", "status": "진행중"}], [])
b = by_key(state.reg_channel_items(CH), "100#1")
check("replay 멱등", r["mode"] == "replay" and b["missing_count"] == 2 and b["status"] == "완료",
      (r["mode"], b["missing_count"], b["status"]))
check("outbox UNIQUE", sum(1 for o in state.outbox_pending() if o["row_key"] == "100#1") == 1,
      state.outbox_pending())

# [6] stale — 과거 스냅샷은 전체 무시
r = state.reg_apply_snapshot(CH, 250, "2026-07-04", [],
    [{"row_key": "250#0", "canon": "x", "task_text": "X", "date_text": "", "status": "진행중"}])
check("stale skip", r["mode"] == "stale" and by_key(state.reg_channel_items(CH), "250#0") is None, r)

# [7] matched∩new 키 충돌 → 예외
try:
    state.reg_apply_snapshot(CH, 400, "2026-07-06",
        [{"row_key": "Y#0", "task_text": "y", "date_text": "", "status": "진행중"}],
        [{"row_key": "Y#0", "canon": "y", "task_text": "y", "date_text": "", "status": "진행중"}])
    check("키 충돌 예외", False, "no raise")
except ValueError:
    check("키 충돌 예외", True)

# [8] snap: 완료 행 재등장 → 부활 (matched status=진행중 전달 시 completed_by 비워짐)
r = state.reg_apply_snapshot(CH, 500, "2026-07-06",
    [{"row_key": "100#1", "task_text": "B작업", "date_text": "", "status": "진행중"}], [])
b = by_key(state.reg_channel_items(CH), "100#1")
check("snap완료 부활", b["status"] == "진행중" and b["completed_by"] == "" and b["missing_count"] == 0, b)

# [9] reply 완료 기록 + revoke의 완료 보존
state.reg_set_status(CH, ["100#1"], "완료", "reply:999")
b = by_key(state.reg_channel_items(CH), "100#1")
check("reply 완료 기록", b["status"] == "완료" and b["completed_by"] == "reply:999", b)
r = state.reg_apply_snapshot(CH, 600, "2026-07-07", [], [
    {"row_key": "600#0", "canon": "c작업", "task_text": "C작업", "date_text": "", "status": "진행중"},
    {"row_key": "600#1", "canon": "d작업", "task_text": "D작업", "date_text": "", "status": "진행중"},
])
state.reg_set_status(CH, ["600#1"], "완료", "reply:1000")
keys = state.reg_revoke_snapshot(CH, 600)
check("revoke 완료 보존", keys == ["600#0"] and by_key(state.reg_channel_items(CH), "600#1") is not None,
      (keys, [x["row_key"] for x in state.reg_channel_items(CH)]))

# [10] allow_missing=False → missing 불변
a_before = by_key(state.reg_channel_items(CH), "100#0")["missing_count"]
state.reg_apply_snapshot(CH, 700, "2026-07-08", [], [], allow_missing=False)
a_after = by_key(state.reg_channel_items(CH), "100#0")["missing_count"]
check("allow_missing=False", a_before == a_after, (a_before, a_after))

# [11] reg_enroll 재등록 시 canon 갱신
state.reg_enroll(CH, "800#0", "옛캐논", "옛작업", managed=0)
state.reg_enroll(CH, "800#0", "새캐논", "새작업", managed=0)
e = by_key(state.reg_channel_items(CH), "800#0")
check("enroll canon 재계산", e["canon"] == "새캐논" and e["task_text"] == "새작업", e)

# [12] canon 전건 조회 (fetchall)
state.reg_enroll(CH, "801#0", "새캐논", "새작업2", managed=0)
hits = state.reg_find_by_canon_all(CH, "새캐논")
check("canon 쌍둥이 전건", len(hits) == 2, len(hits))

# [13] outbox 드레인/리셋
ids = [o["id"] for o in state.outbox_pending()]
state.outbox_del(ids)
check("outbox 비움", state.outbox_pending() == [], state.outbox_pending())
state.reg_reset_channel(CH)
check("채널 리셋", state.reg_channel_items(CH) == [] and state.reg_meta(CH)[0] == 0
      and not state.is_snap_msg(100), None)

print("")
print("RESULT: " + str(passed) + " PASS / " + str(failed) + " FAIL")
try:
    os.remove(state.DB)
except OSError:
    pass
sys.exit(1 if failed else 0)
