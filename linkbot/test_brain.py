# linkbot/test_brain.py — 추출 회귀 테스트 (end-to-end는 Ollama 필요)
#   실행: venv\python.exe linkbot\test_brain.py
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brain

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


print("[1] pure functions (no LLM)")
check("amount 백만원->1000000", brain.norm_amount("백만원") == "1000000", brain.norm_amount("백만원"))
check("amount 천만원->10000000", brain.norm_amount("천만원") == "10000000", brain.norm_amount("천만원"))
check("amount 15만원->150000", brain.norm_amount("15만원") == "150000", brain.norm_amount("15만원"))
check("date 7/15", brain.norm_date("7/15") == "2026-07-15", brain.norm_date("7/15"))
check("date 12월 30일", brain.norm_date("12월 30일") == "2026-12-30", brain.norm_date("12월 30일"))
import re as _re
_ymd = lambda s: bool(_re.match(r"^\d{4}-\d{2}-\d{2}$", s))
check("range 6/25~6/28 -> 범위유지", brain.norm_date("6/25~6/28") == "2026-06-25 ~ 2026-06-28", brain.norm_date("6/25~6/28"))
check("N/? -> YYYY-MM-??", brain.norm_date("8/?") == "2026-08-??", brain.norm_date("8/?"))
check("range 6/27~8/? -> 앞정규화+??", brain.norm_date("6/27~8/?") == "2026-06-27 ~ 2026-08-??", brain.norm_date("6/27~8/?"))
check("일만 27일 -> YMD", _ymd(brain.norm_date("27일")), brain.norm_date("27일"))
check("내일 -> YMD", _ymd(brain.norm_date("내일")), brain.norm_date("내일"))
check("다음주 월요일 -> YMD", _ymd(brain.norm_date("다음주 월요일")), brain.norm_date("다음주 월요일"))
check("중순 유지", brain.norm_date("7월 중순") == "7월 중순", brain.norm_date("7월 중순"))
check("말~초 유지", brain.norm_date("7월말~8월초") == "7월말~8월초", brain.norm_date("7월말~8월초"))
check("settle 미입금->미정산", brain.norm_settle_status("미입금") == "미정산", brain.norm_settle_status("미입금"))
check("settle 입금완료->완료", brain.norm_settle_status("입금완료") == "완료", brain.norm_settle_status("입금완료"))
check("nim 롤리님->롤리", brain.nim_in("롤리님 작업해요") == "롤리", brain.nim_in("롤리님 작업해요"))
check("nim 고객님 제외", brain.nim_in("고객님 문의") == "", brain.nim_in("고객님 문의"))
check("worker 채널접두어", brain.worker_from_channel("마태자-일정정리") == "마태자", brain.worker_from_channel("마태자-일정정리"))
check("status 완료감지", brain.detect_status("다 끝냈어요") == "완료", brain.detect_status("다 끝냈어요"))
check("status 진행중", brain.detect_status("절반 정도 진행") == "진행중", brain.detect_status("절반 정도 진행"))
check("status 완료예정->진행중", brain.detect_status("완료 예정") == "진행중", brain.detect_status("완료 예정"))
_pl = brain.parse_deadline_lines("모델링 | 7/20 | \n의상 | 7/25 | ")
check("parse 멀티 2건", len(_pl) == 2 and _pl[0]["작업내용"] == "모델링" and _pl[1]["기한"] == "7/25", _pl)
check("parse 없음->[]", brain.parse_deadline_lines("없음") == [], brain.parse_deadline_lines("없음"))
_dd = brain.parse_deadline_lines("모델링 | 7/20 | \n모델링 | 7/20 | ")
check("parse dedup", len(_dd) == 1, _dd)
_do = brain.parse_deadline_lines("7/8 | 7/8 | ")
check("parse dateonly 폐기", _do == [], _do)

print("[2] extraction end-to-end (Ollama exaone)")


def az(msg, ch="정산", author="x", men=None):
    return brain.analyze(msg, ch, author, men or [], [])


r = az("롤리 - PM업무 - 백만원")
check("settle 하이픈형식", bool(r) and r["tab"] == "정산" and r["fields"]["항목"] == "PM업무" and r["fields"]["금액"] == "1000000", r)

r = az("키키님 리깅비 5만원 미정산")
check("settle 자연어", bool(r) and r["tab"] == "정산" and r["fields"]["금액"] == "50000" and r["fields"]["상태"] == "미정산", r)

r = az("마태자님 캐릭터 모델링 7/15까지", ch="마태자-일정정리", author="마태자")
check("일정 단일", bool(r) and r["tab"] == "일정" and len(r["items"]) == 1 and "모델링" in r["items"][0]["작업내용"] and r["items"][0]["날짜"] == "2026-07-15", r)

r = az("마태자님 모델링 7/20까지, 의상 7/25까지", ch="마태자-일정정리", author="마태자")
check("일정 다중 2건", bool(r) and r["tab"] == "일정" and len(r["items"]) == 2, r)

r = brain.extract_progress("모델링 절반 정도 했어요")
check("progress 진행추출", bool(r) and bool(r.get("진행내용")), r)
r = brain.extract_progress("ㅇㅋ 감사합니다")
check("progress 무신호 None", r is None, r)

r = az("오늘 점심 뭐 먹지 ㅎㅎ", ch="잡담", author="철수")
check("잡담 무시", r is None, r)

print("")
print("RESULT: " + str(passed) + " PASS / " + str(failed) + " FAIL")
sys.exit(1 if failed else 0)
