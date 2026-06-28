# linkbot/brain.py — 메시지 1건 분석 → {tab, fields, is_task} or None
#   추출 두뇌: 사전필터 → exaone+few-shot → 정규화 → 작업자/의뢰자 판별 → 탭 분류
import re
import json
import datetime
import urllib.request
import config

NIM_RE = re.compile(r"([가-힣A-Za-z0-9_]{1,12})님")
BLOCK = set(config.NIM_BLOCKLIST)

DEADLINE_PROMPT = (
    "다음 디스코드 메시지에서 '작업 일정'을 모두 뽑아.\n"
    "한 메시지에 일정이 여러 개일 수 있다. 서로 다른 작업이면 각각 한 줄로 분리해라.\n"
    "각 줄 형식(정확히): `작업내용 | 기한 | 진행`\n"
    "  - 작업내용: 무엇을 하는지. '@@님'(의뢰 대상) 이름이 붙어 있으면 떼지 말고 그대로 포함해라. (필수)\n"
    "  - 기한: 마감일/날짜. 범위면 '7/1~8/16'처럼 ~ 붙여 그대로 둬라(끝날짜만 X). 없으면 빈칸\n"
    "  - 진행: 진행상황·완료여부 문구가 있으면. 없으면 빈칸\n"
    "규칙:\n"
    "  - 같은 작업을 쪼개지 마라. '모델링'과 '의상'처럼 분명히 다른 작업만 분리.\n"
    "  - 작업내용 앞의 '@@님' 이름은 누가 의뢰한/대상인지라 중요하니 항상 살려라.\n"
    "  - 일정이 하나도 없으면 `없음` 한 단어만.\n"
    "  - 라벨·설명·번호·머리글 금지. 데이터 줄만 출력.\n\n"
    "예시1) 메시지: \"빈즈님 캐릭터 모델링 11/20까지요\"\n->\n빈즈님 캐릭터 모델링 | 11/20 | \n\n"
    "예시2) 메시지: \"뿌요님 여름 의상 작업 6/17~7/20\"\n->\n뿌요님 여름 의상 작업 | 6/17~7/20 | \n\n"
    "예시3) 메시지: \"마태자님 모델링 7/20까지, 의상 7/25까지\"\n->\n마태자님 모델링 | 7/20 | \n마태자님 의상 | 7/25 | \n\n"
    "예시4) 메시지: \"텍스처 작업 다음주 월요일까지 / 리깅은 이번주 금요일\"\n->\n텍스처 작업 | 다음주 월요일 | \n리깅 | 이번주 금요일 | \n\n"
    "예시5) 메시지: \"모델링 7/20까지였는데 절반 정도 끝냈어요\"\n->\n모델링 | 7/20 | 절반 정도 진행\n\n"
    "예시6) 메시지: \"캐릭터 모델링 완료했습니다\"\n->\n캐릭터 모델링 |  | 완료\n\n"
    "예시7) 메시지: \"다들 점심 드셨어요? 날씨 좋네요\"\n->\n없음\n\n"
    "메시지: \"{m}\"\n->\n")
SETTLE_PROMPT = (
    "다음 디스코드 메시지에서 정산(돈) 정보를 뽑아.\n"
    "규칙: 정보가 있으면 정확히 `항목 | 금액 | 상태` 한 줄로만. 없으면 `없음` 한 단어. 라벨·설명·다른 줄 금지.\n\n"
    "예시1) 메시지: \"키키님 모델링비 20만원 입금완료\" -> 모델링비 | 20만원 | 완료\n"
    "예시2) 메시지: \"디자인비 7만원 아직 정산 안됐어요\" -> 디자인비 | 7만원 | 대기\n"
    "예시3) 메시지: \"롤리 - PM업무 - 백만원\" -> PM업무 | 백만원 | 대기\n"
    "예시4) 메시지: \"넵 알겠습니다\" -> 없음\n\n"
    "메시지: \"{m}\"")


def ollama(prompt):
    body = json.dumps({"model": config.MODEL, "prompt": prompt, "stream": False,
                       "keep_alive": "30m",  # 모델을 30분 메모리에 유지 → 재로드 지연 방지
                       "options": {"temperature": 0}}).encode("utf-8")
    req = urllib.request.Request(config.OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read()).get("response", "").strip()


WEEKDAYS = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}


def _norm_one(t):
    """단일 날짜 토큰 → YYYY-MM-DD. 못 읽으면 원문 유지. (범위는 norm_date에서 분리)"""
    t = (t or "").strip()
    if not t:
        return t
    today = datetime.date.today()

    # 애매한 건 그대로 유지 (N월 중순/말/초)
    if re.search(r"\d{1,2}\s*월\s*(초|중순|중|말)", t):
        return t

    # YYYY-MM-DD
    m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # N월 M일
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", t)
    if m:
        return f"{today.year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    # M/D
    m = re.search(r"\b(\d{1,2})[./\-](\d{1,2})\b", t)
    if m:
        try:
            return datetime.date(today.year, int(m.group(1)), int(m.group(2))).strftime("%Y-%m-%d")
        except ValueError:
            return t

    # N/? (일을 모름) → YYYY-MM-??  (범위 끝에 자주 옴: "8/?")
    m = re.search(r"(\d{1,2})\s*[/.]\s*\?+", t)
    if m:
        return f"{today.year}-{int(m.group(1)):02d}-??"

    # 상대표현
    if "오늘" in t:
        return today.strftime("%Y-%m-%d")
    if "내일" in t:
        return (today + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if "모레" in t:
        return (today + datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    if "글피" in t:
        return (today + datetime.timedelta(days=3)).strftime("%Y-%m-%d")

    # 요일 (이번주/다음주 X요일)
    m = re.search(r"(다음\s*주|담\s*주|이번\s*주|금\s*주)?\s*([월화수목금토일])\s*요일", t)
    if m:
        wd = WEEKDAYS[m.group(2)]
        if m.group(1) and ("다음" in m.group(1) or "담" in m.group(1)):
            base = today + datetime.timedelta(days=(7 - today.weekday()))   # 다음 주 월요일
            target = base + datetime.timedelta(days=wd)
        else:
            target = today + datetime.timedelta(days=(wd - today.weekday()) % 7)  # 이번주/단독 = 다가오는 그 요일
        return target.strftime("%Y-%m-%d")

    # 일만 (27일) → 이번 달 그 날 (이미 지났으면 다음 달)
    m = re.search(r"(\d{1,2})\s*일", t)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            y, mo = today.year, today.month
            if day < today.day:
                mo += 1
                if mo > 12:
                    mo, y = 1, y + 1
            try:
                return datetime.date(y, mo, day).strftime("%Y-%m-%d")
            except ValueError:
                return t

    return t   # 그 외 불명확 → 원본 유지


def norm_date(s):
    t = (s or "").strip()
    if not t:
        return t
    # 범위 (A ~ B): 양쪽 각각 정규화. 정렬·D-day는 앞 날짜 기준(시트에서 처리).
    if "~" in t:
        left, right = t.split("~", 1)
        nl, nr = _norm_one(left), _norm_one(right)
        if nl == left.strip() and nr == right.strip():   # 양쪽 다 애매(말~초 등) → 원문 유지
            return t
        return f"{nl} ~ {nr}"
    return _norm_one(t)


HAN_DIGIT = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
HAN_UNIT = {"십": 10, "백": 100, "천": 1000}
HAN_BIG = {"만": 10000, "억": 100000000}


def hangul_num(t):
    total = section = cur = 0
    seen = False
    for ch in t:
        if ch in HAN_DIGIT:
            cur = HAN_DIGIT[ch]; seen = True
        elif ch in HAN_UNIT:
            section += (cur or 1) * HAN_UNIT[ch]; cur = 0; seen = True
        elif ch in HAN_BIG:
            section += cur; total += (section or 1) * HAN_BIG[ch]; section = cur = 0; seen = True
    return (total + section + cur) if seen else 0


def norm_amount(s):
    t = (s or "").replace(",", "").replace(" ", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(억|만|천)?", t)
    if m:
        val = float(m.group(1)) * {"억": 100000000, "만": 10000, "천": 1000}.get(m.group(2), 1)
        return str(int(val))
    han = hangul_num(t)  # 숫자 없이 한글만 ("백만원" 등)
    return str(han) if han else s


def norm_settle_status(s):
    t = (s or "").replace(" ", "")
    if any(k in t for k in ["완료", "입금완료", "지급완료", "정산완료", "입금했", "지급했"]):
        return "완료"
    return "미정산"


def nim_in(content):
    for m in NIM_RE.finditer(content or ""):
        if m.group(1) not in BLOCK:
            return m.group(1)
    return ""


def worker_from_channel(name):
    if name and "일정정리" in name:
        return (name.split("일정정리")[0].rstrip("-ㅣ| ").strip()) or None
    return None


def has(text, hints):
    return any(h in text for h in hints)


def find_client(content, context):
    p = nim_in(content)
    if p:
        return p
    for c in context:
        p = nim_in(c)
        if p:
            return p
    return ""


# LLM이 값 대신 틀의 라벨을 그대로 뱉으면 = 추출 실패
LABELS = {"항목", "금액", "상태", "작업내용", "기한", "담당자", "날짜", "없음", "내용"}

# 완료로 간주할 진행 문구
DONE_HINTS = ["완료", "끝냈", "끝났", "마무리", "마쳤", "done", "완성"]


def _canon_task(s):
    return re.sub(r"\s+", "", (s or "")).lower()


def is_done(text):
    """진짜 완료인지 — 완료 단어가 있고 '예정/예상'(아직 안 됨)이 아닐 때만."""
    t = (text or "").replace(" ", "")
    if "예정" in t or "예상" in t:        # "완료 예정" = 아직 안 됨
        return False
    return any(k in t for k in DONE_HINTS)


def detect_status(progress):
    """진행내용 문구 → 일정 상태. 완료면 '완료', 아니면 '진행중'."""
    return "완료" if is_done(progress) else "진행중"


# 작업명이 순수 날짜/기호로만 구성됐는지 (잡담 환각 방어)
_DATEONLY_RE = re.compile(r"^[\d\s/.\-월일주요()]+$")


def parse_deadline_lines(out):
    """LLM 멀티라인 출력 → [{'작업내용','기한','진행'}, ...].
    잘못된/라벨/빈/날짜만 줄은 폐기. (작업명,기한) 중복 제거."""
    if not out:
        return []
    if out.strip().startswith("없음"):
        return []
    rows, seen = [], set()
    for raw in out.splitlines():
        line = raw.strip()
        if not line or "|" not in line:
            continue
        p = [x.strip() for x in line.split("|")] + ["", "", ""]
        task, due, prog = p[0], p[1], p[2]
        if not task or task in LABELS:
            continue
        if _DATEONLY_RE.match(task):            # 작업명이 날짜/기호뿐 = 환각
            continue
        ck = (_canon_task(task), _canon_task(due))   # (작업명,기한) 튜플 dedup
        if ck in seen:
            continue
        seen.add(ck)
        rows.append({"작업내용": task, "기한": due, "진행": prog})
    return rows


# 답장 진행문구 전용 경량 추출 프롬프트
PROGRESS_PROMPT = (
    "다음은 어떤 작업에 대한 '진행 상황 답장'이다. 진행내용과 날짜를 뽑아.\n"
    "형식(정확히): `진행 | 날짜`  (날짜 없으면 빈칸). 진행 문구가 전혀 없으면 `없음`.\n\n"
    "예시1) \"모델링 절반 정도 했어요\" -> 모델링 절반 진행 | \n"
    "예시2) \"7/22까지 미루겠습니다\" -> 기한 연기 | 7/22\n"
    "예시3) \"다 끝냈습니다\" -> 완료 | \n"
    "예시4) \"ㅇㅋ 감사합니다\" -> 없음\n\n"
    "메시지: \"{m}\"\n->\n")


def extract_progress(text):
    """답장/진행 답장 → {'진행내용': str, '날짜': str|None} or None."""
    t = (text or "")[:300]
    if len(t) < 2:
        return None
    out = ollama(PROGRESS_PROMPT.format(m=t))
    if not out or out.strip().startswith("없음") or "|" not in out:
        return None
    p = [x.strip() for x in out.split("|")] + ["", ""]
    prog = p[0]
    if not prog or prog in LABELS:
        return None
    date = norm_date(p[1]) if p[1] else None
    return {"진행내용": prog, "날짜": date or None}


def review(content, who, client, reason):
    return {"tab": "확인필요", "fields": {
        "내용": content[:120], "추정 작업자": who or "", "추정 의뢰자": client or "", "사유": reason}}


def analyze(content, channel_name, author, mentions, context):
    text = (content or "")[:400]
    if len(text) < 3:
        return None

    # 정산 우선
    if has(text, config.MONEY_HINTS):
        out = ollama(SETTLE_PROMPT.format(m=text))
        if "|" in out and not out.startswith("없음"):
            p = [x.strip() for x in out.split("|")] + ["", "", ""]
            who = mentions[0] if mentions else author
            if not p[0] or p[0] in LABELS:                 # 라벨 echo = 추출 실패
                return review(content, who, nim_in(content), "정산 추출 실패(형식 불명확)")
            recv = nim_in(content) or who
            return {"tab": "정산", "fields": {
                "받는이": recv, "항목": p[0], "금액": norm_amount(p[1]),
                "상태": norm_settle_status(p[2])}}

    # 일정/작업 (멀티화: 0..N건)
    if has(text, config.DATE_HINTS):
        out = ollama(DEADLINE_PROMPT.format(m=text))
        rows = parse_deadline_lines(out)          # 0..N건
        if not rows:
            return None                           # 신호는 있었으나 0건 → None

        who = mentions[0] if mentions else author
        client = find_client(content, context)
        worker = worker_from_channel(channel_name) or (mentions[0] if mentions else None)

        # 작업자 불명확 = 메시지 단위 → 통째 확인필요 (행 안 쪼갬)
        if not worker:
            return review(content, who, client, "작업자 불명확")

        # 보수 모드: 10건 초과는 과분할/환각 의심 → 확인필요 회송
        if len(rows) > 10:
            return review(content, who, client, f"일정 과다추출({len(rows)}건) — 확인 요망")

        items = []
        for r in rows:
            items.append({
                "날짜": norm_date(r["기한"]),
                "작업내용": r["작업내용"],
                "진행내용": r["진행"],            # 빈 문자열 가능 (빈값이면 bot이 키 생략/빈칸)
                "담당자": worker,
                "상태": detect_status(r["진행"]),
            })
        return {"tab": "일정", "is_task": True, "items": items}

    return None
