# 테스트용: 현재 일정/정산 채널의 모든 메시지를 _processed에 넣어 '드레인'
# → 이후 새로 올라온 메시지만 추출되게 (격리 테스트). 실데이터 본문은 안 읽음(ID만).
import os, sys, glob, sqlite3, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config

BASE = os.path.dirname(HERE)
DATA_DIR = os.path.join(BASE, "data")
PROC = os.path.join(HERE, "_processed.json")

proc = set()
if os.path.exists(PROC):
    proc = set(json.load(open(PROC, encoding="utf-8")))
before = len(proc)

for p in glob.glob(os.path.join(DATA_DIR, "*_*.db")):
    con = sqlite3.connect(p)
    chans = con.execute(
        "SELECT channel_id, channel_name FROM channel_snapshots GROUP BY channel_id").fetchall()
    keep = [cid for cid, name in chans
            if name and (config.DEADLINE_KEYWORD in name or config.SETTLE_KEYWORD in name)]
    for cid in keep:
        for (mid,) in con.execute(
                "SELECT message_id FROM messages WHERE channel_id=? AND is_bot=0", (cid,)):
            proc.add(str(mid))
    con.close()

json.dump(sorted(proc), open(PROC, "w", encoding="utf-8"))
print(f"drained: {before} -> {len(proc)} processed (일정/정산 채널 전체 마킹)")
