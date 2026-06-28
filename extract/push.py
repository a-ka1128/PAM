# extract/push.py — 이미 만든 CSV를 구글 시트 웹훅으로 동기화 (재추출 없음)
import os
import sys
import csv
import json
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config


def read_csv(path):
    if not os.path.exists(path):
        return [], []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return [], []
    return rows[0], rows[1:]


def push(kind, cols, rows):
    if not rows:
        print(kind, "→ 행 없음, 건너뜀")
        return
    body = json.dumps({"sheet": kind, "cols": cols, "rows": rows}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(config.WEBHOOK_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            print(f"{kind} → push OK (status {resp.status}, {len(rows)}행)")
    except Exception as e:
        print(f"{kind} → push 실패: {e}")


if not getattr(config, "WEBHOOK_URL", ""):
    print("WEBHOOK_URL 비어있음")
    sys.exit(1)

for kind, fname in [("마감(추출)", "마감.csv"), ("확인필요", "확인필요.csv"), ("정산(추출)", "정산.csv")]:
    cols, rows = read_csv(os.path.join(HERE, fname))
    push(kind, cols, rows)
print("DONE")
