# =========================================================
# extract.py (v5) — 일정/정산 채널에서 구조화 추출 → 마감/정산/확인필요 CSV
#   #1 중복방지 + #2 증분: _processed.json 으로 이미 본 메시지는 LLM 스킵
#   #3 정규화: 날짜→YYYY-MM-DD(가능시), 금액→숫자
#   #4 표준화: 정산 상태→[대기/완료]
#   #5(스크립트단): 의뢰자 미상 마감건 → 확인필요.csv 로 분리 (PM 검수 대상)
#   [마감] 작업자=채널, 의뢰자=본문/주변 'OO님'   [정산] 받는이=@멘션→님→작성자
# =========================================================
import os
import sys
import re
import glob
import sqlite3
import csv
import json
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config

BASE = os.path.dirname(HERE)
DATA_DIR = os.path.join(BASE, "data")
OLLAMA = "http://localhost:11434/api/generate"
PROCESSED_PATH = os.path.join(HERE, "_processed.json")

NIM_RE = re.compile(r"([가-힣A-Za-z0-9_]{1,12})님")
BLOCK = set(getattr(config, "NIM_BLOCKLIST", []))

DEADLINE_PROMPT = (
    "다음 디스코드 메시지에서 '작업 내용'과 '기한(마감일/날짜)'을 뽑아.\n"
    "규칙: 정보가 있으면 정확히 `작업내용 | 기한` 한 줄로만. 없으면 `없음` 한 단어. 라벨·설명·다른 줄 금지.\n\n"
    "예시1) 메시지: \"빈즈님 캐릭터 모델링 11/20까지요\" -> 캐릭터 모델링 | 11/20\n"
    "예시2) 메시지: \"다음주 월요일까지 텍스처 수정할게요\" -> 텍스처 수정 | 다음주 월요일\n"
    "예시3) 메시지: \"다들 점심 드셨어요?\" -> 없음\n\n"
    "메시지: \"{m}\"")
SETTLE_PROMPT = (
    "다음 디스코드 메시지에서 정산(돈) 정보를 뽑아.\n"
    "규칙: 정보가 있으면 정확히 `항목 | 금액 | 상태` 한 줄로만. 없으면 `없음` 한 단어. 라벨·설명·다른 줄 금지.\n\n"
    "예시1) 메시지: \"키키님 모델링비 20만원 입금완료\" -> 모델링비 | 20만원 | 완료\n"
    "예시2) 메시지: \"디자인비 7만원 아직 정산 안됐어요\" -> 디자인비 | 7만원 | 대기\n"
    "예시3) 메시지: \"넵 알겠습니다\" -> 없음\n\n"
    "메시지: \"{m}\"")


# ---------- 정규화 (#3, #4) ----------
def norm_date(s):
    t = (s or "").strip()
    m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{1,2})월\s*(\d{1,2})일", t)
    if m:
        return f"2026-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.search(r"\b(\d{1,2})[./\-](\d{1,2})\b", t)
    if m:
        return f"2026-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return t  # 상대표현(다음주 등)·불명확 → 원본 유지


def norm_amount(s):
    t = (s or "").replace(",", "").replace(" ", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(억|만|천)?", t)
    if not m:
        return s
    val = float(m.group(1)) * {"억": 100000000, "만": 10000, "천": 1000}.get(m.group(2), 1)
    return str(int(val))


def norm_status(s):
    t = (s or "").replace(" ", "")
    if any(k in t for k in ["미입금", "미지급", "대기", "예정", "미정", "안들어", "아직"]):
        return "대기"
    if any(k in t for k in ["완료", "입금완료", "지급완료", "정산완료", "입금했", "지급했"]):
        return "완료"
    return s


# ---------- LLM / DB ----------
def ollama(prompt):
    body = json.dumps({"model": config.MODEL, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0}}).encode("utf-8")
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read()).get("response", "").strip()


def channels_with(con, keyword):
    rows = con.execute(
        "SELECT channel_id, channel_name, MAX(captured_at) FROM channel_snapshots GROUP BY channel_id").fetchall()
    return {cid: name for cid, name, _ in rows if name and keyword in name}


def channel_msgs(con, channel_id, limit):
    rows = con.execute(
        "SELECT message_id, guild_id, author_name, content FROM messages "
        "WHERE channel_id=? AND is_bot=0 AND length(content)>=2 "
        "ORDER BY created_at DESC LIMIT ?", (channel_id, limit)).fetchall()
    return rows[::-1]


def recent_msgs(con, channel_ids, limit):
    if not channel_ids:
        return []
    qs = ",".join("?" * len(channel_ids))
    return con.execute(
        f"SELECT channel_id, message_id, guild_id, author_name, content FROM messages "
        f"WHERE is_bot=0 AND channel_id IN ({qs}) AND length(content)>=5 "
        f"ORDER BY created_at DESC LIMIT ?", list(channel_ids) + [limit]).fetchall()


def mentions_for(con, message_ids):
    if not message_ids:
        return {}
    qs = ",".join("?" * len(message_ids))
    d = {}
    for mid, name in con.execute(
            f"SELECT message_id, target_name FROM mentions "
            f"WHERE target_type='user' AND message_id IN ({qs})", message_ids).fetchall():
        d.setdefault(mid, []).append(name)
    return d


def worker_from_channel(name):
    if name and "일정정리" in name:
        return (name.split("일정정리")[0].rstrip("-ㅣ| ").strip()) or None
    return None


def nim_in(content):
    for m in NIM_RE.finditer(content or ""):
        if m.group(1) not in BLOCK:
            return m.group(1)
    return ""


def find_client(msgs, i, window):
    p = nim_in(msgs[i][3])
    if p:
        return p
    for d in range(1, window + 1):
        for j in (i - d, i + d):
            if 0 <= j < len(msgs):
                p = nim_in(msgs[j][3])
                if p:
                    return p
    return ""


def jump(gid, cid, mid):
    return f"https://discord.com/channels/{gid}/{cid}/{mid}"


def load_processed():
    if os.path.exists(PROCESSED_PATH):
        try:
            return set(json.load(open(PROCESSED_PATH, encoding="utf-8")))
        except Exception:
            return set()
    return set()


def extract_deadline(con, chan_map, want, window, processed):
    items, review = [], []
    for cid, cname in chan_map.items():
        if len(items) + len(review) >= want:
            break
        msgs = channel_msgs(con, cid, config.SCAN_LIMIT)
        worker = worker_from_channel(cname)
        for i, (mid, gid, author, content) in enumerate(msgs):
            if len(items) + len(review) >= want:
                break
            if str(mid) in processed:
                continue
            processed.add(str(mid))
            out = ollama(DEADLINE_PROMPT.format(m=str(content)[:400]))
            if out.startswith("없음") or "|" not in out:
                continue
            parts = [p.strip() for p in out.split("|")] + ["", ""]
            client = find_client(msgs, i, window)
            row = {"작업자": worker or author, "의뢰자": client,
                   "작업내용": parts[0], "기한": norm_date(parts[1]),
                   "채널": cname, "링크": jump(gid, cid, mid)}
            (items if client else review).append(row)
    return items, review


def extract_settle(con, chan_map, want, processed):
    items = []
    msgs = recent_msgs(con, list(chan_map.keys()), config.SCAN_LIMIT)
    mmap = mentions_for(con, [m[1] for m in msgs])
    for cid, mid, gid, author, content in msgs:
        if len(items) >= want:
            break
        if str(mid) in processed:
            continue
        processed.add(str(mid))
        out = ollama(SETTLE_PROMPT.format(m=str(content)[:400]))
        if out.startswith("없음") or "|" not in out:
            continue
        parts = [p.strip() for p in out.split("|")] + ["", "", ""]
        ment = mmap.get(mid, [])
        subj = ment[0] if ment else (nim_in(content) or author)
        items.append({"받는이": subj, "항목": parts[0], "금액": norm_amount(parts[1]),
                      "상태": norm_status(parts[2]), "채널": chan_map.get(cid, ""),
                      "링크": jump(gid, cid, mid)})
    return items


def write_csv(path, rows, cols):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def main():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*_*.db")))
    half = config.N_TARGET // 2
    window = getattr(config, "CONTEXT_WINDOW", 3)
    processed = load_processed()
    deadline, review, settle = [], [], []
    for p in files:
        con = sqlite3.connect(p)
        if len(deadline) + len(review) < half:
            d, r = extract_deadline(con, channels_with(con, config.DEADLINE_KEYWORD),
                                    half - (len(deadline) + len(review)), window, processed)
            deadline += d
            review += r
        if len(settle) < config.N_TARGET - half:
            settle += extract_settle(con, channels_with(con, config.SETTLE_KEYWORD),
                                     (config.N_TARGET - half) - len(settle), processed)
        con.close()

    json.dump(sorted(processed), open(PROCESSED_PATH, "w", encoding="utf-8"))

    dcols = ["작업자", "의뢰자", "작업내용", "기한", "채널", "링크"]
    scols = ["받는이", "항목", "금액", "상태", "채널", "링크"]
    write_csv(os.path.join(HERE, "마감.csv"), deadline, dcols)
    write_csv(os.path.join(HERE, "확인필요.csv"), review, dcols)
    write_csv(os.path.join(HERE, "정산.csv"), settle, scols)

    with open(os.path.join(HERE, "_summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"마감(의뢰자 있음): {len(deadline)}건\n")
        f.write(f"확인필요(의뢰자 미상→PM): {len(review)}건\n")
        f.write(f"정산: {len(settle)}건\n")
        f.write(f"처리한 메시지 누적: {len(processed)}개 (증분 — 다음 실행은 새 메시지만)\n")
    print("DONE 마감", len(deadline), "확인필요", len(review), "정산", len(settle))


if __name__ == "__main__":
    main()
