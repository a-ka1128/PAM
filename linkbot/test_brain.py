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
check("range 끝 일만: 월 상속", brain.norm_date("6월 5일~10일") == "2026-06-05 ~ 2026-06-10", brain.norm_date("6월 5일~10일"))
check("range 끝 일만(일 생략)", brain.norm_date("7/8~10") == "2026-07-08 ~ 2026-07-10", brain.norm_date("7/8~10"))
check("range 끝 일만+일자 역전: 다음달", brain.norm_date("6월 28일~3일") == "2026-06-28 ~ 2026-07-03", brain.norm_date("6월 28일~3일"))
check("range 연말 역전: 다음해", brain.norm_date("12월 28일~3일") == "2026-12-28 ~ 2027-01-03", brain.norm_date("12월 28일~3일"))
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
# 완료/진행중 경계 (골든 FAIL 3건에서 도출한 하드 회귀 고정)
check("status 완료 단독", brain.is_done("완료") is True, brain.is_done("완료"))
check("status 끝내고->완료", brain.is_done("끝내고") is True, brain.is_done("끝내고"))
check("status 거의완성->미완료", brain.is_done("거의 완성") is False, brain.is_done("거의 완성"))
check("status 거의끝났->미완료", brain.is_done("거의 끝났습니다") is False, brain.is_done("거의 끝났습니다"))
check("status 마무리후현재정리중->미완료", brain.is_done("마무리 후, 현재 정리 중") is False, brain.is_done("마무리 후, 현재 정리 중"))
check("status 정리중->미완료", brain.is_done("정리 중") is False, brain.is_done("정리 중"))
check("status 계속작업->미완료", brain.is_done("마무리 후 계속 작업") is False, brain.is_done("마무리 후 계속 작업"))
check("status 빈값->미완료", brain.is_done("") is False, brain.is_done(""))
import json as _json
_dj = lambda items: _json.dumps({"items": items}, ensure_ascii=False)
_pl = brain.parse_deadline_json(_dj([{"작업내용": "모델링", "기한": "7/20", "진행": ""},
                                     {"작업내용": "의상", "기한": "7/25", "진행": ""}]))
check("parse 멀티 2건", len(_pl) == 2 and _pl[0]["작업내용"] == "모델링" and _pl[1]["기한"] == "7/25", _pl)
check("parse 빈배열->[]", brain.parse_deadline_json(_dj([])) == [], brain.parse_deadline_json(_dj([])))
check("parse 비JSON->[]", brain.parse_deadline_json("없음") == [], brain.parse_deadline_json("없음"))
_dd = brain.parse_deadline_json(_dj([{"작업내용": "모델링", "기한": "7/20", "진행": ""},
                                     {"작업내용": "모델링", "기한": "7/20", "진행": ""}]))
check("parse dedup", len(_dd) == 1, _dd)
_do = brain.parse_deadline_json(_dj([{"작업내용": "7/8", "기한": "7/8", "진행": ""}]))
check("parse dateonly 폐기", _do == [], _do)
_bl = brain.parse_deadline_json(_dj([{"작업내용": "- 구슬요 오리지널 의상", "기한": "7/20", "진행": ""}]))
check("parse 머리불릿 제거", len(_bl) == 1 and _bl[0]["작업내용"] == "구슬요 오리지널 의상", _bl)
_bd = brain.parse_deadline_json(_dj([{"작업내용": "2026-07-20 - 구슬요 오리지널 의상", "기한": "7/20", "진행": ""}]))
check("parse 머리날짜 제거", len(_bd) == 1 and _bd[0]["작업내용"] == "구슬요 오리지널 의상", _bd)
_gp = brain.parse_deadline_json(_dj([{"작업내용": "개인일정", "기한": "", "진행": ""},
                                     {"작업내용": "개인의뢰", "기한": "", "진행": ""}]))
check("parse 빈껍데기 폐기", _gp == [], _gp)
# 인물 오병합 방지 — 후보가 전부 '다른 사람 님'이면 LLM 없이 결정적으로 새 행 (실측 표우/카푸 오배정 회귀)
_pc = lambda ts, q: brain.match_task([{"canon": brain.canon(t), "task": t} for t in ts], q)
check("match 다른인물 오병합 기각", _pc(["요나일님 의상 제작"], "표우님 의상 제작") is None,
      _pc(["요나일님 의상 제작"], "표우님 의상 제작"))
check("match 다른인물 복수후보도 기각", _pc(["요나일님 의상 제작", "카푸님 의상 제작"], "표우님 의상 제작") is None,
      _pc(["요나일님 의상 제작", "카푸님 의상 제작"], "표우님 의상 제작"))
check("match 동일인물 완전일치는 병합", _pc(["요나일님 의상 제작"], "요나일님 의상 제작") == 0,
      _pc(["요나일님 의상 제작"], "요나일님 의상 제작"))
_gk = brain.parse_deadline_json(_dj([{"작업내용": "개인일정 (뮤 헤어 5종)", "기한": "7/8", "진행": ""}]))
check("parse 개인일정+내용 유지", len(_gk) == 1, _gk)
_ss = brain.parse_structured_snapshot(
    "전체일정\n[7/8 ~ 7/12] 희또 작업 | 의상 삼면도\n"
    "[7/13 ~ 7/31] 유화유화 작업 | 오리지널헤드\n"
    "[7/13 ~ 7/31] 유화유화 작업 | 오리지널헤어\n"
    "[7/20~8/16] 문모모 작업 | 헤어")
check("구조화 전체일정 파싱(헤어 보존·부위 구분)",
      _ss is not None and len(_ss) == 4 and _ss[0]["작업내용"] == "희또 의상 삼면도"
      and _ss[3]["작업내용"] == "문모모 헤어" and _ss[1]["작업내용"] != _ss[2]["작업내용"], _ss)
check("구조화 2줄미만 None(LLM폴백)", brain.parse_structured_snapshot("전체일정\n[7/8] 희또 작업 | 의상") is None, None)
_sp = brain.parse_structured_snapshot(
    "전체일정\n[7/19] 청백 윤이제 의상,헤어 완료\n"
    "[7/20 ~ 7/21] - (룩플)솜주먹 세트\n"
    "[7/22 ~ 7/25] -(개인) 위도2세트\n"
    "[7/26 ~ 7/30] - (룩플)청백가요대전 5세트")
check("구조화 파이프없는 형식(태그제거·꼬리완료)",
      _sp is not None and len(_sp) == 4
      and _sp[0]["작업내용"] == "청백 윤이제 의상,헤어" and _sp[0]["진행"] == "완료"
      and _sp[1]["작업내용"] == "솜주먹 세트" and _sp[2]["작업내용"] == "위도2세트", _sp)
check("구조화 날짜아닌 대괄호 무시",
      brain.parse_structured_snapshot("전체일정\n[진행보고] 내용정리\n[메모] 기타사항") is None, None)
_sl = brain.parse_structured_snapshot("전체일정\n[8/1~8/13] 문모모 작업ㅣ헤어\n[7/22~7/31] 카푸 작업ㅣ헤어")
check("구조화 한글모음ㅣ 구분자", _sl is not None and _sl[0]["작업내용"] == "문모모 헤어"
      and _sl[1]["작업내용"] == "카푸 헤어", _sl)

_cm0 = brain._CLIENT_MAP
brain._CLIENT_MAP = brain._parse_client_lines(
    ["미미짱짱세용", "카푸", "청백", "청백가요제=청백", "청백가요대전=청백"])
check("client 토큰 일치", brain.client_from_task("미미짱짱세용 오리지널 작업") == "미미짱짱세용", None)
check("client 별칭→정규명", brain.client_from_task("청백가요제 2벌") == "청백", None)
check("client 접두일치(3자+)", brain.client_from_task("청백가요대전2 백팀 4인") == "청백", None)
check("client 무매치 빈칸", brain.client_from_task("의상 모델링 작업") == "", None)
check("client 부분문자열 오탐 방지", brain.client_from_task("카푸치노 굿즈") == "", None)
brain._CLIENT_MAP = _cm0

print("[1.5] snapshot pure functions (no LLM)")
check("canon 님/공백/꼬리 제거", brain.canon("뿌요님 여름 의상 작업") == brain.canon("뿌요 여름의상"), brain.canon("뿌요님 여름 의상 작업"))
check("canon 괄호주석 무시", brain.canon("권민님 작캠용 바디 수정 도움 (개인일정)") == brain.canon("권민님 작캠용 바디 수정 도움"), brain.canon("권민님 작캠용 바디 수정 도움 (개인일정)"))
check("canon 계절 구분", brain.canon("뿌요님 여름 의상") != brain.canon("뿌요님 겨울 의상"), brain.canon("뿌요님 여름 의상"))
check("canon 진행꼬리 제거", brain.canon("카조에님 모델링 작업 진행중") == brain.canon("카조에님 모델링"), brain.canon("카조에님 모델링 작업 진행중"))
check("canon 식별괄호 보존(구분)", brain.canon("개인일정 (뮤 헤어 5종)") != brain.canon("개인일정 (하나미 세트)"), brain.canon("개인일정 (뮤 헤어 5종)"))
check("canon 식별괄호 vs 무괄호 구분", brain.canon("개인일정 (뮤 헤어 5종)") != brain.canon("개인일정"), None)
check("canon 부위 괄호 구분", brain.canon("유화유화 작업") != brain.canon("유화유화 작업 (오리지널헤드)"), None)
check("canon 날짜괄호 무시", brain.canon("의상 (6/28~7/15)") == brain.canon("의상"), brain.canon("의상 (6/28~7/15)"))
check("canon 룩플태그 무시", brain.canon("(룩플)솜주먹 세트") == brain.canon("솜주먹 세트"), brain.canon("(룩플)솜주먹 세트"))
check("canon 머리불릿 무관", brain.canon("- 구슬요 오리지널 의상") == brain.canon("구슬요 오리지널 의상"), None)
check("find_client 본문우선", brain.find_client("빈즈님 모델링", ["딴사람님 잡담"]) == "빈즈", None)
check("find_client context 무시", brain.find_client("모델링 작업", ["요나일님 딴작업"]) == "", brain.find_client("모델링 작업", ["요나일님 딴작업"]))
check("snap_hint 헤더줄 매치", bool(brain.SNAP_HINT_RE.search("06.28\n[진행보고]\n\n전체일정\n[6/28] 의상")), None)
check("snap_hint 문장속 미매치", not brain.SNAP_HINT_RE.search("전체일정에 추가해주세요"), None)
check("pick 번호", brain._pick_idx('{"번호": 2}', 5) == 2, brain._pick_idx('{"번호": 2}', 5))
check("pick 0=새작업 None", brain._pick_idx('{"번호": 0}', 3) is None, brain._pick_idx('{"번호": 0}', 3))
check("pick 범위밖 None", brain._pick_idx('{"번호": 7}', 3) is None, brain._pick_idx('{"번호": 7}', 3))
check("pick 비JSON None", brain._pick_idx("몰라요", 3) is None, brain._pick_idx("몰라요", 3))
check("progline 판정", brain._is_progline({"날짜": "", "진행내용": "진행중"}) and not brain._is_progline({"날짜": "2026-07-15", "진행내용": ""}), None)

_mi = brain.merge_snapshot_items([
    {"날짜": "", "작업내용": "카조에님 모델링 작업", "진행내용": "진행중", "상태": "진행중"},
    {"날짜": "2026-07-15", "작업내용": "카조에님 모델링", "진행내용": "", "상태": "진행중"},
    {"날짜": "2026-07-30", "작업내용": "다시바님 헤어", "진행내용": "", "상태": "진행중"},
])
check("merge 진행줄 흡수", len(_mi) == 2 and _mi[0]["날짜"] == "2026-07-15" and _mi[0]["진행내용"] == "진행중", _mi)
_mt = brain.merge_snapshot_items([
    {"날짜": "2026-07-05", "작업내용": "바디 수정(상의)", "진행내용": "", "상태": "진행중"},
    {"날짜": "2026-07-10", "작업내용": "바디 수정(하의)", "진행내용": "", "상태": "진행중"},
])
check("merge 날짜쌍둥이 보존", len(_mt) == 2, _mt)

_cands = [{"canon": brain.canon("카조에님 오리지널 의상"), "task": "카조에님 오리지널 의상"},
          {"canon": brain.canon("다시바님 헤드"), "task": "다시바님 헤드"}]
_ms = brain.match_snapshot(_cands, ["카조에 오리지널 의상", "다시바님 헤드 작업"])
check("match_snapshot 1단 선점", _ms == [0, 1], _ms)
check("match_snapshot 후보없음", brain.match_snapshot([], ["아무거나"]) == [None], brain.match_snapshot([], ["아무거나"]))

_orig_ollama = brain.ollama
brain.ollama = lambda p, schema=None: '{"번호": 0}'
check("match_task 2단 새작업", brain.match_task(_cands, "완전 무관한 작업명") is None, None)
brain.ollama = lambda p, schema=None: '{"번호": 1}'
check("match_task 2단 번호", brain.match_task(_cands, "카조에님 의상") == 0, None)
check("match_task 유사도가드 기각", brain.match_task(_cands, "카조에님 리깅") is None, None)
brain.ollama = _orig_ollama
check("route_reply 후보1개 생략", brain.route_reply([{"task": "의상"}], "절반 했어요") == ("one", 0), None)

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
