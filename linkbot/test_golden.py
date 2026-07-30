# linkbot/test_golden.py — 회귀 골든셋 러너 (LLM 경로, Ollama 필요)
#   실행: venv\python.exe linkbot\test_golden.py
#   ★ LLM 경로는 temperature 0라도 완전 결정적이 아니라 hard-assert 하지 않음.
#     통과/실패 카운트 + 실패 diff만 출력하고 '항상 exit 0'. 프롬프트/모델 변경 전후 회귀 감지용.
#   순수함수(norm_date/norm_amount/canon 등)의 정밀 회귀는 test_brain.py(hard assert)가 담당.
import os
import sys
import json
import re
from datetime import date

try:                                    # Windows 콘솔(cp949)에서 한글/em대시 출력 크래시 방지
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brain

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR = str(date.today().year)     # golden의 "{Y}" 치환값 — norm_date가 today.year 기준이라 연도부패 방지


def _load(name):
    """골든 파일 로드. local_ 변형(실서버 데이터, gitignore)도 있으면 합쳐 로드."""
    out = []
    for fn in (name, "local_" + name):
        path = os.path.join(GOLDEN, fn)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                out.append(json.loads(ln))
    return out


class Tally:
    def __init__(self):
        self.p = 0
        self.f = 0
        self.fails = []

    def ok(self):
        self.p += 1

    def bad(self, name, detail):
        self.f += 1
        self.fails.append((name, detail))


def _match_item(exp, items):
    """기대 item(task_has + date/date_ymd + status/client)이 실제 items 중 하나와 맞으면 True.
    client: 의뢰자 정확일치("" = 빈칸이어야 함). 다중 항목 메시지에서 앞사람 이름이
    뒤 항목으로 새던 버그를 골든으로 잡기 위한 필드."""
    for it in items:
        if exp.get("task_has", "") not in it.get("작업내용", ""):
            continue
        if "date" in exp:
            if it.get("날짜", "") != exp["date"].replace("{Y}", _YEAR):
                continue
        elif exp.get("date_ymd"):
            if not _YMD.match(it.get("날짜", "")):
                continue
        if "status" in exp and it.get("상태", "") != exp["status"]:
            continue
        if "client" in exp and (it.get("의뢰자") or "") != exp["client"]:
            continue
        return True
    return False


def run_deadline(t):
    for c in _load("deadline.jsonl"):
        name = "[deadline] " + c.get("name", "?")
        try:
            r = brain.analyze(c["msg"], c.get("channel", "일정"), c.get("author", "x"),
                              c.get("mentions", []), c.get("context", []))
            tab = r["tab"] if r else None
            if tab != c["expect_tab"]:
                t.bad(name, f"tab={tab} expect={c['expect_tab']}")
                continue
            if c["expect_tab"] == "일정":
                items = r.get("items", [])
                miss = [e for e in c.get("items", []) if not _match_item(e, items)]
                if miss:
                    got = [{"작업내용": i.get("작업내용"), "날짜": i.get("날짜")} for i in items]
                    t.bad(name, f"missing={miss} got={got}")
                    continue
            t.ok()
        except Exception as e:
            t.bad(name, f"EXC {type(e).__name__}: {e}")


def run_settle(t):
    for c in _load("settle.jsonl"):
        name = "[settle] " + c.get("name", "?")
        try:
            r = brain.analyze(c["msg"], "정산", "x", [], [])
            tab = r["tab"] if r else None
            if tab != c["expect_tab"]:
                t.bad(name, f"tab={tab} expect={c['expect_tab']}")
                continue
            ff = (r or {}).get("fields", {})
            exp = c.get("fields", {})
            bad = None
            if "항목_has" in exp and exp["항목_has"] not in ff.get("항목", ""):
                bad = f"항목={ff.get('항목')!r} !~ {exp['항목_has']!r}"
            elif "금액" in exp and ff.get("금액", "") != exp["금액"]:
                bad = f"금액={ff.get('금액')!r} expect={exp['금액']!r}"
            elif "상태" in exp and ff.get("상태", "") != exp["상태"]:
                bad = f"상태={ff.get('상태')!r} expect={exp['상태']!r}"
            if bad:
                t.bad(name, bad)
            else:
                t.ok()
        except Exception as e:
            t.bad(name, f"EXC {type(e).__name__}: {e}")


def run_same(t):
    for c in _load("same_task.jsonl"):
        name = "[same_task] " + c.get("name", "?")
        try:
            cands = [{"canon": brain.canon(x), "task": x} for x in c["candidates"]]
            r = brain.match_task(cands, c["query"])
            exp = c["expect"]
            got = "new" if r is None else r
            if got != exp:
                t.bad(name, f"got={got} expect={exp}")
            else:
                t.ok()
        except Exception as e:
            t.bad(name, f"EXC {type(e).__name__}: {e}")


def run_route(t):
    for c in _load("route.jsonl"):
        name = "[route] " + c.get("name", "?")
        try:
            cands = [{"task": x} for x in c["candidates"]]
            kind, idx = brain.route_reply(cands, c["reply"])
            if kind != c["expect_kind"]:
                t.bad(name, f"kind={kind} expect={c['expect_kind']}")
            elif kind == "one" and idx != c.get("expect_idx"):
                t.bad(name, f"idx={idx} expect={c.get('expect_idx')}")
            else:
                t.ok()
        except Exception as e:
            t.bad(name, f"EXC {type(e).__name__}: {e}")


def main():
    print("골든셋 회귀 러너 (LLM 경로, Ollama 필요) — report-only, 항상 exit 0")
    print("=" * 62)
    try:
        brain.ollama("핑")                                  # Ollama 사전 점검
    except Exception as e:
        print(f"⚠️ Ollama 연결 실패 — 러너 중단: {e}\n   (Ollama 켜고 다시 실행)")
        return 0
    t = Tally()
    run_deadline(t)
    run_settle(t)
    run_same(t)
    run_route(t)
    print()
    if t.fails:
        print(f"실패 {t.f}건:")
        for nm, d in t.fails:
            print(f"  FAIL {nm}\n        {d}")
        print()
    total = t.p + t.f
    tail = "  ✅ 전부 통과" if not t.f else f"  ({t.f} FAIL — 위 diff 확인)"
    print(f"RESULT: {t.p}/{total} PASS{tail}")
    return 0                                                 # LLM 비결정성 → 회귀는 눈으로 확인, 항상 0


if __name__ == "__main__":
    sys.exit(main())
