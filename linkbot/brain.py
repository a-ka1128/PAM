# linkbot/brain.py — 메시지 1건 분석 → {tab, fields, is_task} or None
#   추출 두뇌: 사전필터 → exaone+few-shot → 정규화 → 작업자/의뢰자 판별 → 탭 분류
import os
import re
import json
import time
import datetime
import threading
import unicodedata
import urllib.request
import config

NIM_RE = re.compile(r"([가-힣A-Za-z0-9_]{1,12})님")
BLOCK = set(config.NIM_BLOCKLIST)

# 출력은 Ollama format(JSON 스키마)으로 강제 — 포맷 깨짐/라벨 echo 원천 차단.
#   프롬프트는 브레이스({}) 예시를 포함하므로 str.format 금지 — 호출부에서 메시지를 이어붙인다.
DEADLINE_PROMPT = (
    "다음 디스코드 메시지에서 '작업 일정'을 모두 뽑아라.\n"
    "출력(JSON만): {\"items\": [{\"작업내용\": \"...\", \"기한\": \"...\", \"진행\": \"...\"}]}\n"
    "규칙:\n"
    "  - 작업내용: 간결한 작업명(문장 전체 금지). '@@님'(의뢰 대상) 이름이 붙어 있으면 떼지 말고 그대로 포함.\n"
    "  - 기한: 마감·예정 날짜. 범위면 '7/1~8/16'처럼 ~ 유지(끝날짜만 X).\n"
    "  - 메시지 앞머리의 날짜 헤더('5월 13일', '## 0514', '-260516', '0517일', '0607(일)' 등, "
    "뒤에 '> ' 인용부호 불릿이 와도 무시하고 날짜로 인식)는 그 보고의 날짜다 — "
    "작업에 별도 날짜가 없으면 헤더 날짜를 'M/D'로 기한에 넣어라. 날짜가 전혀 없으면 빈칸.\n"
    "  - 진행: 진행상황·완료여부 문구('완료', '80% 진행', '내일 마무리 예정' 등). 없으면 빈칸.\n"
    "  - 일일 작업 보고('~진행중입니다', '~했습니다', '~예정입니다')도 일정이다. 놓치지 마라.\n"
    "  - '거의 완성'·'마무리 예정'·'컨펌 예정'은 완료가 아니다. '완료'는 실제로 끝났을 때만 써라.\n"
    "  - '~까지 컨펌받으려고 정리중이다/준비중이다'처럼 목표 시점 + 구체적 항목 나열이 함께 있으면 "
    "일정이다(잡담 아님). 나열된 항목은 각각 분리. 단, 그 반대로 상대에게 '언제 받을 수 있는지/맞는지' "
    "묻는 질문(물음표, '~일까요', '~궁금해요')이면 이 규칙을 적용하지 말고 items는 빈 배열.\n"
    "  - 같은 작업을 쪼개지 마라. '모델링'과 '의상'처럼 분명히 다른 작업만 분리.\n"
    "  - 일정 정보가 없는 잡담·질문이면 items는 빈 배열.\n\n"
    "예시1) \"빈즈님 캐릭터 모델링 11/20까지요\"\n"
    "-> {\"items\": [{\"작업내용\": \"빈즈님 캐릭터 모델링\", \"기한\": \"11/20\", \"진행\": \"\"}]}\n"
    "예시2) \"5월 13일\n앞부분 폴리곤 정리 + 어색한 부분 수정\"\n"
    "-> {\"items\": [{\"작업내용\": \"앞부분 폴리곤 정리 + 어색한 부분 수정\", \"기한\": \"5/13\", \"진행\": \"\"}]}\n"
    "예시3) \"마태자님 모델링 7/20까지, 의상 7/25까지\"\n"
    "-> {\"items\": [{\"작업내용\": \"마태자님 모델링\", \"기한\": \"7/20\", \"진행\": \"\"}, "
    "{\"작업내용\": \"마태자님 의상\", \"기한\": \"7/25\", \"진행\": \"\"}]}\n"
    "예시4) \"문모모님 헤어 90% 완성! 내일 마무리하고 컨펌받을 예정\"\n"
    "-> {\"items\": [{\"작업내용\": \"문모모님 헤어\", \"기한\": \"\", \"진행\": \"90% 진행, 내일 마무리 예정\"}]}\n"
    "예시5) \"네일 전부 완성해서 컨펌요청 드렸습니다\"\n"
    "-> {\"items\": [{\"작업내용\": \"네일\", \"기한\": \"\", \"진행\": \"완료\"}]}\n"
    "예시6) \"뿌요님 여름 의상 작업 6/17~7/20\"\n"
    "-> {\"items\": [{\"작업내용\": \"뿌요님 여름 의상 작업\", \"기한\": \"6/17~7/20\", \"진행\": \"\"}]}\n"
    "예시7) \"다들 점심 드셨어요? 날씨 좋네요\"\n-> {\"items\": []}\n\n"
    "예시8) \"0607(일)\n> 비전용뚜따 작업 의뢰 들어온거  작업\n> 오늘은 개인의뢰 위주로 할것같습니당\"\n"
    "-> {\"items\": [{\"작업내용\": \"비전용뚜따 작업\", \"기한\": \"6/7\", \"진행\": \"\"}]}\n"
    "예시9) \"이번주말중으로 헤드,헤어,의상 러프 컨펌 받으려고 정리중이여서 딜레이가 심하진 않을것 같습니다\"\n"
    "-> {\"items\": [{\"작업내용\": \"헤드\", \"기한\": \"이번주말\", \"진행\": \"러프 컨펌 준비중\"}, "
    "{\"작업내용\": \"헤어\", \"기한\": \"이번주말\", \"진행\": \"러프 컨펌 준비중\"}, "
    "{\"작업내용\": \"의상\", \"기한\": \"이번주말\", \"진행\": \"러프 컨펌 준비중\"}]}\n\n")
DEADLINE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"작업내용": {"type": "string"},
                       "기한": {"type": "string"},
                       "진행": {"type": "string"}},
        "required": ["작업내용", "기한", "진행"]}}},
    "required": ["items"]}

SETTLE_PROMPT = (
    "다음 디스코드 메시지에서 정산(돈) 정보를 뽑아라.\n"
    "출력(JSON만): {\"found\": true/false, \"항목\": \"...\", \"금액\": \"...\", \"상태\": \"...\"}\n"
    "규칙:\n"
    "  - 돈 얘기가 아니면 found는 false, 나머지는 빈칸.\n"
    "  - 상태: 입금/지급이 끝났으면 '완료', 아니면 '대기'. 견적·누락건 추가는 '대기'.\n"
    "  - 세부 항목별 금액과 총액이 같이 있으면 총액을 써라.\n"
    "  - 구체적 업무명/항목명이 없고 '1인당 n개씩 총 얼마'처럼 단순 수량 계산 설명이면 "
    "항목은 빈 문자열로 둬라(found는 true 유지).\n\n"
    "예시1) \"키키님 모델링비 20만원 입금완료\"\n"
    "-> {\"found\": true, \"항목\": \"모델링비\", \"금액\": \"20만원\", \"상태\": \"완료\"}\n"
    "예시2) \"디자인비 7만원 아직 정산 안됐어요\"\n"
    "-> {\"found\": true, \"항목\": \"디자인비\", \"금액\": \"7만원\", \"상태\": \"대기\"}\n"
    "예시3) \"롤리 - PM업무 - 백만원\"\n"
    "-> {\"found\": true, \"항목\": \"PM업무\", \"금액\": \"백만원\", \"상태\": \"대기\"}\n"
    "예시4) \"귀 수정 및 접합 / 8만원 (귀 5, 접합 3) 견적입니다\"\n"
    "-> {\"found\": true, \"항목\": \"귀 수정 및 접합\", \"금액\": \"8만원\", \"상태\": \"대기\"}\n"
    "예시5) \"작업 비용 정산드렸습니다!\"\n"
    "-> {\"found\": true, \"항목\": \"작업 비용\", \"금액\": \"\", \"상태\": \"완료\"}\n"
    "예시6) \"넵 알겠습니다\"\n"
    "-> {\"found\": false, \"항목\": \"\", \"금액\": \"\", \"상태\": \"\"}\n"
    "예시7) \"아아 1인당 4개씩 총 20만원입니다!\"\n"
    "-> {\"found\": true, \"항목\": \"\", \"금액\": \"20만원\", \"상태\": \"대기\"}\n\n")
SETTLE_SCHEMA = {
    "type": "object",
    "properties": {"found": {"type": "boolean"},
                   "항목": {"type": "string"},
                   "금액": {"type": "string"},
                   "상태": {"type": "string"}},
    "required": ["found", "항목", "금액", "상태"]}


# thinking 지원 모델 프리픽스 — 즉답형 프롬프트라 생각과정 비활성 (미지원 모델에 넣으면 400)
_THINK_OFF_PREFIXES = ("qwen3", "deepseek-r1", "magistral")


# 여러 메시지가 동시에 들어오면(다중 채널 활동, catchup 등) analyze()가 스레드풀에서 병렬로
#   ollama()를 호출한다 — GPU 1장·14B 모델은 동시요청을 못 받고 서버 큐에서 줄서다 각자의
#   120s 타임아웃을 넘겨버림(실측: 로그에 수 초 간격 TimeoutError 뭉치로 확인됨).
#   요청을 우리 쪽에서 한 번에 하나씩만 내보내면 대기는 이 락에서 하고, 타임아웃 시계는
#   실제로 실행될 때만 돌아 개별 요청은 정상적으로 120s 안에 끝난다.
_OLLAMA_LOCK = threading.Lock()


def ollama(prompt, schema=None, think=False):
    # num_ctx: Ollama 기본 4096은 위험 — DEADLINE_PROMPT만 1276토큰, 스냅샷 포함 시
    #   1888토큰이라 출력까지 하면 한계에 근접하고, 초과분은 프롬프트 앞(=규칙·예시)부터
    #   잘려나가 추출 품질이 조용히 무너진다. 모델 한도는 40960이므로 넉넉히 확보.
    # think: 추출은 즉답형이라 항상 끄고, '같은 작업인가' 같은 판단 호출만 켤 수 있게 한다
    #   (config.THINK_JUDGE). 사고 토큰만큼 느려지므로 이득이 측정될 때만 켠다.
    payload = {"model": config.MODEL, "prompt": prompt, "stream": False,
               "keep_alive": "30m",  # 모델을 30분 메모리에 유지 → 재로드 지연 방지
               "options": {"temperature": 0,
                           "num_ctx": getattr(config, "NUM_CTX", 8192)}}
    if schema is not None:
        payload["format"] = schema         # 문법 강제(constrained decoding) → 항상 유효 JSON
    if any(config.MODEL.startswith(p) for p in _THINK_OFF_PREFIXES):
        payload["think"] = bool(think)
    body = json.dumps(payload).encode("utf-8")
    last = None
    with _OLLAMA_LOCK:
        for attempt in range(2):          # 일시 멈칫 자동 복구 (최대 2회 시도)
            try:
                req = urllib.request.Request(config.OLLAMA, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read()).get("response", "").strip()
            except Exception as e:
                last = e
                if attempt == 0:
                    time.sleep(3)         # 잠깐 쉬고 한 번 더
    raise last


def _json_or(out, default):
    """LLM 출력 → JSON. 스키마 강제라 실패는 드물지만 방어."""
    try:
        return json.loads(out or "")
    except Exception:
        return default


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


_RANGE_DAY_RE = re.compile(r"^\s*(\d{1,2})\s*일?\s*$")   # 범위 끝의 '10일'/'10'
_YMD_FULL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _range_right(nl, right):
    """범위 끝쪽 정규화. '6월5일~10일'처럼 일(日)만 있으면 앞 날짜의 연·월을 상속.
    (_norm_one의 '지난 날짜면 다음 달' 규칙이 범위 끝에 적용돼 엉뚱한 달이 되던 버그 방지)"""
    m = _RANGE_DAY_RE.match(right or "")
    ym = _YMD_FULL_RE.match(nl or "")
    if m and ym:
        y, mo, left_day = int(ym.group(1)), int(ym.group(2)), int(ym.group(3))
        d = int(m.group(1))
        if d < left_day:                       # '6월28일~3일' → 다음 달 3일
            mo += 1
            if mo > 12:
                mo, y = 1, y + 1
        try:
            return datetime.date(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return _norm_one(right)


def norm_date(s):
    t = (s or "").strip()
    if not t:
        return t
    # 범위 (A ~ B): 양쪽 각각 정규화. 정렬·D-day는 앞 날짜 기준(시트에서 처리).
    if "~" in t:
        left, right = t.split("~", 1)
        # 한쪽만 있는 범위는 범위가 아니다 — "[~ 5/26]"은 '5/26까지'라는 단일 마감.
        #   그대로 두면 " ~ 2026-05-26"처럼 앞이 빈 기형 값이 시트에 들어가 정렬·D-day가 어긋난다.
        if not left.strip():
            return _norm_one(right)
        if not right.strip():
            return _norm_one(left)
        nl = _norm_one(left)
        nr = _range_right(nl, right)
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


def find_client(content, context=None):
    # 이웃 메시지(context)에서 'OO님'을 긁으면 엉뚱한 의뢰자가 새어들어와 오탐 →
    #   본문의 'OO님'만 사용. 본문에 없으면 빈칸(PM이 채움). context 스캔이 오탐의 주원인이었다.
    return nim_in(content)


# ═══════════ 의뢰자 사전 — PM 교정값의 결정적 재사용 (능동학습 ⑩의 수동승인 형태) ═══════════
#   golden/local_clients.txt (gitignored): 한 줄 = 의뢰자명, 또는 '별칭=의뢰자' (예: 청백가요제=청백).
#   PM이 시트에서 직접 교정/승인한 이름만 사람 손으로 추가한다 — 자동 축적 금지(결정성 원칙).
#   'OO님' 없이 이름만 쓰인 작업명("카푸 작업", "미미짱짱세용 오리지널")에서 의뢰자를 채우는 폴백.
_CLIENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "local_clients.txt")
_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-z가-힣]+")


def _parse_client_lines(lines):
    m = {}
    for ln in lines:
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        alias, _, name = ln.partition("=")
        key = unicodedata.normalize("NFKC", re.sub(r"\s+", "", alias)).lower()
        if key:
            m[key] = (name.strip() or alias.strip())
    return m


def reload_clients():
    global _CLIENT_MAP
    try:
        with open(_CLIENTS_PATH, encoding="utf-8") as f:
            _CLIENT_MAP = _parse_client_lines(f)
    except OSError:
        _CLIENT_MAP = {}


_CLIENT_MAP = {}
reload_clients()


def client_from_task(task):
    """작업내용 토큰이 사전의 의뢰자명과 일치하면 그 이름 반환 (없으면 "").
    완전일치(2자 이름) 또는 접두일치(3자+ 별칭)만 — 부분문자열 오탐 방지("카푸치노"≠"카푸")."""
    if not _CLIENT_MAP:
        return ""
    t = unicodedata.normalize("NFKC", (task or "")).lower()
    for tok in _TOKEN_SPLIT_RE.split(t):
        if not tok:
            continue
        if tok in _CLIENT_MAP:
            return _CLIENT_MAP[tok]
        for alias, name in _CLIENT_MAP.items():
            if len(alias) >= 3 and tok.startswith(alias):
                return name
    return ""


# LLM이 값 대신 틀의 라벨을 그대로 뱉으면 = 추출 실패
LABELS = {"항목", "금액", "상태", "작업내용", "기한", "담당자", "날짜", "없음", "내용"}

# 완료로 간주할 진행 문구
#   ⚠️ "끝내"는 넣지 말 것 — '끝내고'(완료)뿐 아니라 '끝내야/끝내려'(아직 안 함)까지 잡는다.
#      완료형만 좁게 나열한다.
DONE_HINTS = ["완료", "끝냈", "끝났", "끝내고", "마무리", "마쳤", "done", "완성"]
# 완료어가 함께 있어도 '아직 안 끝남'으로 봐야 하는 신호 (완료어보다 우선)
#   "거의 완성"=almost, "마무리 후 현재 정리 중"=계속 진행, "완료 예정"=아직 등.
NOT_DONE_HINTS = ["예정", "예상", "진행중", "거의", "계속", "현재", "하는중", "하고있",
                  "야겟", "야겠", "야하", "야 하", "려고"]   # '끝내야겟어요'·'끝내려고' = 아직 안 함


def _canon_task(s):
    return re.sub(r"\s+", "", (s or "")).lower()


def is_done(text):
    """진짜 완료인지 — 완료 단어가 있고 '아직 안 됨' 신호가 없을 때만.
    '아직 안 됨' 신호(NOT_DONE_HINTS, 문미 '중')는 완료어보다 우선한다:
    '거의 완성'·'마무리 후 현재 정리 중'은 완료가 아니라 진행중."""
    t = (text or "").replace(" ", "")
    if any(k in t for k in NOT_DONE_HINTS):        # "거의/현재/계속/예정…"=아직 안 됨
        return False
    if t.endswith("중"):                            # "정리중"·"작업중"·"검수중"=진행 중
        return False
    return any(k in t for k in DONE_HINTS)


def detect_status(progress):
    """진행내용 문구 → 일정 상태. 완료면 '완료', 아니면 '진행중'."""
    return "완료" if is_done(progress) else "진행중"


# 작업명이 순수 날짜/기호로만 구성됐는지 (잡담 환각 방어)
_DATEONLY_RE = re.compile(r"^[\d\s/.\-월일주요()]+$")

# 스냅샷 리스트 줄 포맷 잔재 정리용
_LEAD_JUNK_RE = re.compile(r"^\s*(?:[-–—•·▪‣*∙]+|\d{1,2}\s*[.)])\s*")
_LEAD_DATE_RE = re.compile(r"^\s*(?:20\d{2}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}\s*[/.]\s*\d{1,2})\s*[-~–]?\s*")
# 알맹이 없는 껍데기 작업명 (canon 기준) → 폐기
_GENERIC_ONLY = {"개인일정", "개인의뢰", "개인작업", "개인", "작업", "일정", "의뢰", "기타"}


def _clean_task(t):
    """작업내용 머리의 불릿/번호/날짜 토큰 제거 ('- 구슬요…', '7/20 - 구슬요…')."""
    t = (t or "").strip()
    for _ in range(4):
        m = _LEAD_JUNK_RE.match(t)
        if m and m.end() < len(t):
            t = t[m.end():].strip()
            continue
        m = _LEAD_DATE_RE.match(t)               # 날짜는 뒤에 실제 내용(한글/영문)이 있을 때만 제거
        if m and m.end() < len(t) and re.search(r"[가-힣A-Za-z]", t[m.end():]):
            t = t[m.end():].strip()
            continue
        break
    return t.strip()


def parse_deadline_json(out):
    """LLM JSON 출력 → [{'작업내용','기한','진행'}, ...].
    라벨/빈/날짜만 작업명은 폐기. (작업명,기한) 중복 제거."""
    rows, seen = [], set()
    for r in _json_or(out, {}).get("items", []):
        if not isinstance(r, dict):
            continue
        task = _clean_task(str(r.get("작업내용", "")))   # 머리 불릿/날짜 제거 ('- 구슬요…', '7/20 - …')
        due = str(r.get("기한", "")).strip()
        prog = str(r.get("진행", "")).strip()
        if not task or task in LABELS:
            continue
        if _DATEONLY_RE.match(task):            # 작업명이 날짜/기호뿐 = 환각
            continue
        if _canon_task(task) in _GENERIC_ONLY:   # 알맹이 없는 '개인일정/개인의뢰' 껍데기 → 폐기
            continue
        ck = (_canon_task(task), _canon_task(due))   # (작업명,기한) 튜플 dedup
        if ck in seen:
            continue
        seen.add(ck)
        rows.append({"작업내용": task, "기한": due, "진행": prog})
    return rows


def extract_deadline(text):
    """메시지 → 일정 rows. analyze()와 bot의 확인필요 재추출이 공용."""
    return parse_deadline_json(
        ollama(DEADLINE_PROMPT + f'메시지: "{text}"\n->\n', DEADLINE_SCHEMA))


def extract_settle(text):
    """메시지 → {'항목','금액','상태'}(정규화 전 항목, 정규화된 금액/상태) | None(돈 얘기 아님).
    항목이 라벨 echo면 {'항목': ''}로 반환해 호출부가 확인필요 회송 판단."""
    j = _json_or(ollama(SETTLE_PROMPT + f'메시지: "{text}"\n->\n', SETTLE_SCHEMA), {})
    if not j.get("found"):
        return None
    item = str(j.get("항목", "")).strip()
    if item in LABELS:
        item = ""
    return {"항목": item,
            "금액": norm_amount(str(j.get("금액", ""))),
            "상태": norm_settle_status(str(j.get("상태", "")))}


# '[날짜] 대상 작업 | 작업내용' 구조의 전체일정 줄 (구분자: | ｜ + 한글모음 ㅣ(U+3163) — 실사용 관측)
_SNAP_LINE_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*(.*?)\s*[|｜ㅣ]\s*(.+?)\s*$")
# 구분자 없는 '[날짜] 내용' 줄 ("[7/20 ~ 7/21] - (룩플)솜주먹 세트") — 날짜 검증은 _is_dateish로 별도
_SNAP_NOPIPE_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*[-–—~:]*\s*(.+?)\s*$")
_WORK_SUFFIX_RE = re.compile(r"\s*작업\s*$")
_TRAIL_DONE_RE = re.compile(r"[\s,]*완료\s*$")


def parse_structured_snapshot(text):
    """'[7/8 ~ 7/12] 희또 작업 | 의상 삼면도' 같은 구조화 전체일정을 결정적으로 파싱.
    사용자의 '|' 구분자가 DEADLINE_PROMPT의 '|' 출력 포맷과 충돌해 LLM이 오파싱(작업내용을
    껍데기 'OO 작업'으로 만들고 실제 내용을 진행칸에 넣거나 유실)하던 것을 우회한다.
    구분자 없는 '[날짜] - (태그)내용' 줄도 지원(과다추출 15건짜리 LLM 오파싱 형식) —
    이 경우 대괄호 안이 날짜꼴일 때만 인정하고, 꼬리 '완료'는 진행칸으로 옮긴다.
    2줄 이상 매치될 때만 rows 반환(그 포맷이 확실). 아니면 None → LLM 폴백."""
    rows, seen = [], set()
    for raw in (text or "").splitlines():
        m = _SNAP_LINE_RE.match(raw)
        if m:
            date, prefix, content = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            prefix = _WORK_SUFFIX_RE.sub("", prefix).strip()     # "희또 작업" → "희또"
            task_src = (prefix + " " + content).strip() if prefix else content
            prog = ""
        else:
            m = _SNAP_NOPIPE_RE.match(raw)
            if not m or not _is_dateish(m.group(1).strip()):
                continue                                     # 대괄호 안이 날짜가 아니면 헤더/메모 줄
            date = m.group(1).strip()
            body = _strip_noise_parens(m.group(2)).strip()   # '(룩플)/(개인)' 카테고리 태그 제거
            prog = ""
            dm = _TRAIL_DONE_RE.search(body)
            if dm:
                prog, body = "완료", body[:dm.start()].strip()
            task_src = body
        task = _clean_task(task_src)
        if not task or _canon_task(task) in _GENERIC_ONLY:
            continue
        ck = _canon_task(task)                                # 완전 동일 작업명만 중복 제거
        if ck in seen:
            continue
        seen.add(ck)
        rows.append({"작업내용": task, "기한": date, "진행": prog})
    return rows if len(rows) >= 2 else None


# 답장 진행문구 전용 경량 추출 프롬프트
PROGRESS_PROMPT = (
    "다음은 어떤 작업에 대한 '진행 상황 답장'이다. 진행내용과 날짜를 뽑아라.\n"
    "출력(JSON만): {\"found\": true/false, \"진행\": \"...\", \"날짜\": \"...\"}\n"
    "진행 문구가 전혀 없으면 found는 false. 날짜 없으면 빈칸.\n\n"
    "예시1) \"모델링 절반 정도 했어요\" -> {\"found\": true, \"진행\": \"모델링 절반 진행\", \"날짜\": \"\"}\n"
    "예시2) \"7/22까지 미루겠습니다\" -> {\"found\": true, \"진행\": \"기한 연기\", \"날짜\": \"7/22\"}\n"
    "예시3) \"다 끝냈습니다\" -> {\"found\": true, \"진행\": \"완료\", \"날짜\": \"\"}\n"
    "예시4) \"ㅇㅋ 감사합니다\" -> {\"found\": false, \"진행\": \"\", \"날짜\": \"\"}\n\n")
PROGRESS_SCHEMA = {
    "type": "object",
    "properties": {"found": {"type": "boolean"},
                   "진행": {"type": "string"},
                   "날짜": {"type": "string"}},
    "required": ["found", "진행", "날짜"]}


def extract_progress(text):
    """답장/진행 답장 → {'진행내용': str, '날짜': str|None} or None."""
    t = (text or "")[:300]
    if len(t) < 2:
        return None
    j = _json_or(ollama(PROGRESS_PROMPT + f'메시지: "{t}"\n->\n', PROGRESS_SCHEMA), {})
    if not j.get("found"):
        return None
    prog = str(j.get("진행", "")).strip()
    if not prog or prog in LABELS:
        return None
    d = str(j.get("날짜", "")).strip()
    date = norm_date(d) if d else None
    return {"진행내용": prog, "날짜": date or None}


# ═══════════ 스냅샷(전체일정) 매칭 엔진 ═══════════
CANON_VERSION = 3    # canon 규칙 변경 시 +1 → 기동 시 reg_recanon으로 전량 재계산 (v3: '(룩플)' 태그 노이즈 추가)

_PAREN_RE = re.compile(r"[(\[{（【〔「『［]\s*(.*?)\s*[)\]}）】〕」』］]")
_NIM_STRIP_RE = re.compile(r"([가-힣a-z0-9_]{1,12})님")
_TRAIL = ("까지", "부터", "작업", "예정", "진행", "중")
# 괄호주석 중 '노이즈'만 제거한다: 날짜/기간 + 아래 카테고리 태그.
#   ⚠️ "(뮤 헤어 5종)" "(위도 의상)" 같은 식별용 괄호는 반드시 보존 —
#   이걸 지우면 서로 다른 작업이 전부 canon "개인일정"으로 뭉쳐 중복/오병합이 난다.
_NOISE_PAREN = {"개인일정", "개인의뢰", "개인작업", "개인", "참고", "비고", "메모", "예시", "룩플"}


def _is_dateish(inner):
    """괄호 내용이 날짜/기간뿐인가 (예: '6/28~7/15', '7월 중순')."""
    x = re.sub(r"[\d\s.~/\-()]", "", inner)
    x = re.sub(r"(월|일|주|요일|초|중순|중|말|부터|까지)", "", x)
    return inner != "" and x == ""


def _strip_noise_parens(t):
    def repl(m):
        inner = m.group(1).strip()
        if not inner:
            return ""
        if re.sub(r"\s", "", inner) in _NOISE_PAREN:
            return ""                            # 카테고리 태그 → 제거
        if _is_dateish(inner):
            return ""                            # 날짜/기간 주석 → 제거
        return m.group(0)                        # 식별용 내용 → 보존
    return _PAREN_RE.sub(repl, t)


def canon(s):
    """스냅샷 레지스트리 매칭용 작업명 정규화. (_canon_task와 별개)"""
    t = unicodedata.normalize("NFKC", (s or "")).lower().strip()
    if not t:
        return ""
    t2 = _strip_noise_parens(t)                  # 날짜/카테고리 괄호만 제거(식별 괄호는 보존)
    if len(re.sub(r"\s", "", t2)) >= 2:          # 전부 지워졌으면 되돌림 (빈 canon 방지)
        t = t2
    t = _NIM_STRIP_RE.sub(r"\1", t)              # 카조에님 → 카조에 (이름은 보존)
    t = re.sub(r"[^0-9a-z가-힣.~/]", "", t)       # 공백·문장부호 제거
    t = re.sub(r"(?<![0-9])[.~/]|[.~/](?![0-9])", "", t)  # 숫자 사이 아닌 . ~ / 제거
    changed = True
    while changed:                               # 꼬리 필러: "모델링작업중"→"모델링"
        changed = False
        for suf in _TRAIL:
            if t.endswith(suf) and len(t) - len(suf) >= 2:
                t = t[:-len(suf)]
                changed = True
    return t


SNAP_HINT_RE = re.compile(r"^\s*[\[\(#*•\-~<【]*\s*전체\s*일정\s*[\]\)>:#*~】]*\s*$", re.M)
SNAP_BODY_MAX = 1200      # 스냅샷 본문 절단 한도 (bot이 절단 여부 판정에도 사용)

SAME_TASK_PROMPT = (
    "한 작업자의 '기존 작업 목록'과 '새로 보고된 작업' 하나가 있다.\n"
    "새 작업이 기존 목록 중 하나와 같은 작업인지 판정해라.\n"
    "같은 작업 = 표현·어순·꾸밈말만 다르고 실제로는 동일한 일.\n"
    "대상 인물이 다르거나, 부위·단계·차수·계절 등 실체가 다르면 다른 작업이다.\n"
    "출력(JSON만): {\"번호\": n} — 같은 작업의 번호. 없거나 확실하지 않으면 0.\n\n"
    "예시1)\n기존 작업 목록:\n1. 강님 명함 인쇄\n2. 하늘님 로고 시안\n"
    "새로 보고된 작업: 하늘님 로고시안 제작\n-> {\"번호\": 2}\n\n"
    "예시2)\n기존 작업 목록:\n1. 구름님 포스터 앞면\n"
    "새로 보고된 작업: 구름님 포스터 뒷면\n-> {\"번호\": 0}\n\n"
    "예시3)\n기존 작업 목록:\n1. 별님 채색\n2. 눈님 표지\n3. 강님 배너 수정 보조\n"
    "새로 보고된 작업: 강님 홈페이지용 배너 수정\n-> {\"번호\": 3}\n\n"
    "예시4)\n기존 작업 목록:\n1. 눈님 여름 카드\n"
    "새로 보고된 작업: 눈님 겨울 카드\n-> {\"번호\": 0}\n\n"
    "예시5)\n기존 작업 목록:\n1. 별님 1차 시안\n"
    "새로 보고된 작업: 별님 2차 시안\n-> {\"번호\": 0}\n\n")
_PICK_SCHEMA = {"type": "object",
                "properties": {"번호": {"type": "integer"}},
                "required": ["번호"]}


_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}")


def _last_json(out):
    """자유 텍스트 안의 마지막 JSON 객체를 꺼낸다.
    thinking 모드에선 format(문법 강제)을 못 쓴다 — 스키마를 걸면 모델이 사고 없이
    곧장 JSON만 뱉어 추론이 사라지기 때문(실측: thinking 길이 0). 그래서 판단 호출을
    thinking으로 돌릴 때는 스키마 없이 받고 여기서 결과만 회수한다."""
    if not out:
        return {}
    for m in reversed(_JSON_OBJ_RE.findall(out)):
        try:
            v = json.loads(m)
            if isinstance(v, dict):
                return v
        except Exception:
            continue
    return {}


def _judge(prompt):
    """판단 호출 1건 → dict. config.THINK_JUDGE면 thinking(스키마 없이), 아니면 스키마 강제."""
    if getattr(config, "THINK_JUDGE", False):
        return _last_json(ollama(prompt, None, think=True))
    return _json_or(ollama(prompt, _PICK_SCHEMA), {})


def _pick_idx(out, n):
    """{'번호': k} → 1..n | 그 외 None. (0 = 해당없음/새작업)"""
    k = (out if isinstance(out, dict) else _json_or(out, {})).get("번호")
    if isinstance(k, int) and 1 <= k <= n:
        return k
    return None


def _core_task(t):
    """인물(OO님) 제거 후 canon — 작업 실체 비교용."""
    return canon(NIM_RE.sub("", t or ""))


def _bigrams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def _bigram_ratio(a, b):
    ba, bb = _bigrams(a), _bigrams(b)
    return len(ba & bb) / max(1, min(len(ba), len(bb)))


_CHOSUNG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNGSUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONGSUNG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"


def _decompose_jamo(s):
    """완성형 한글 음절 → 초/중/종성 자모열 (그 외 문자는 그대로 유지).
    음절 단위 bigram은 짧은 작업명에서 원소 수가 2~3개뿐이라 임계값 근처에서
    글자 하나 차이로도 판정이 튄다 — 자모로 풀면 원소 수가 늘어 더 촘촘한 신호가 된다."""
    out = []
    for ch in s or "":
        code = ord(ch) - 0xAC00
        if 0 <= code <= 11171:
            cho, rem = divmod(code, 21 * 28)
            jung, jong = divmod(rem, 28)
            out.append(_CHOSUNG[cho])
            out.append(_JUNGSUNG[jung])
            if jong:
                out.append(_JONGSUNG[jong])
        else:
            out.append(ch)
    return "".join(out)


def _core_sim_ok(a, b):
    """LLM '같다' 판정의 결정적 백스톱 — 핵심 글자 겹침이 전혀 없으면 기각(리깅≠의상).
    음절 bigram AND 자모 bigram 둘 다 통과해야 병합 허용 — 짧은 작업명('채색1차' vs '채색2차')에서
    음절 bigram 단독은 임계값 근처 불안정, 자모 단위를 AND로 얹어 백스톱을 보강한다."""
    if not a or not b:
        return True                            # 판단 불가 → LLM 존중
    if _bigram_ratio(a, b) <= 0.34:
        return False
    return _bigram_ratio(_decompose_jamo(a), _decompose_jamo(b)) > 0.34


def match_task(candidates, new_task):
    """candidates: [{'canon','task'}] 순서 유지. 반환 인덱스|None(새 행).
    ollama 전송 실패는 전파 — 호출부가 스냅샷 diff 전체 중단."""
    if not candidates:
        return None
    nc = canon(new_task)
    if nc:
        hits = [i for i, c in enumerate(candidates)
                if c.get("canon") and c["canon"] == nc]
        if len(hits) == 1:
            return hits[0]                     # 1단: 유일 완전일치만
    # 인물 선필터(결정적): 새 작업에 'OO님'이 있으면 같은 인물 후보만 → 오답 공간 축소
    pool = list(range(len(candidates)))
    nm = nim_in(new_task or "")
    person_matched = False                     # 같은 인물 후보로 좁혀졌는가 (뒤의 길이비 게이트 면제 조건)
    if nm:
        same = [i for i in pool if nm in (candidates[i].get("task") or "")]
        if same:
            pool = same
            person_matched = True
        else:
            # 그 인물의 기존 작업이 하나도 없는 경우. 예전엔 필터가 통째로 무력화돼
            #   pool에 남의 작업이 그대로 남았고, 뒤의 포함 판정은 _core_task가 이름을 떼므로
            #   '표우님 의상 제작'과 '요나일님 의상 제작'이 둘 다 '의상제작'이 되어 오병합됐다.
            #   → 인물이 명시된 후보는 전부 다른 사람 일이므로 제외한다.
            #   (인물 표기가 없는 후보는 이름 없이 기록된 같은 작업일 수 있어 남긴다)
            pool = [i for i in pool if not nim_in(candidates[i].get("task") or "")]
            if not pool:
                return None
    # 포함 fast-path(결정적): 이름 뗀 핵심이 포함관계(≥4자)로 유일하면 LLM 생략 ("바디수정"⊂"작캠용 바디 수정 도움")
    #   ⚠️ 길이비 게이트 필수 — 짧은 일반명이 긴 특정 작업에 우연히 포함되는 경우가 많다
    #   ("오리지널의상" ⊂ "청백가요대전청팀콰르텟팀오리지널의상"). 이때 자동 병합하면
    #   서로 다른 작업이 조용히 합쳐지므로, 길이 차가 크면 fast-path를 포기하고 LLM에 맡긴다.
    core_new = _core_task(new_task)
    if len(core_new) >= 4:
        def _contained(cb):
            if not cb or not (core_new in cb or cb in core_new):
                return False
            lo, hi = min(len(core_new), len(cb)), max(len(core_new), len(cb))
            # 임계 0.4 — 정당한 포함("바디수정"⊂"작캠용바디수정도움" 0.44)은 살리고,
            #   일반명 우연포함("오리지널의상"⊂"청백가요대전청팀콰르텟팀오리지널의상" 0.33)은 걸러낸다.
            #   fast-path 포기 = LLM 판정으로 넘김(안전한 방향)이라 임계는 보수적으로 잡는다.
            return lo >= 4 and lo / hi >= 0.4
        sub = [i for i in pool if _contained(_core_task(candidates[i].get("task")))]
        if len(sub) == 1:
            return sub[0]
    cand_lines = "\n".join(
        f"{k + 1}. {(candidates[i].get('task') or '')[:60]}" for k, i in enumerate(pool))
    out = _judge(SAME_TASK_PROMPT
                 + f"기존 작업 목록:\n{cand_lines}\n새로 보고된 작업: {(new_task or '')[:60]}\n->\n")
    r = _pick_idx(out, len(pool))
    if r is not None:
        pick = pool[r - 1]
        cb = _core_task(candidates[pick].get("task"))
        if not _core_sim_ok(core_new, cb):
            return None                        # 글자 겹침 0 = 실체 다름 → 오병합 기각
        # 길이비 백스톱: 짧은 작업명이 긴 자유문장에 우연히 얽히는 오병합 차단
        #   ("쉐이프키 만들기" vs "트윈테일 잔머리 없애는 쉐이프키도 한번 만들어 봤습니다").
        #   ⚠️ 인물이 이미 일치한 경우는 면제 — 이름을 떼면 핵심이 짧아져서
        #   ("카조에님 의상" → "의상" vs "오리지널의상") 정당한 매칭까지 걸린다.
        if not person_matched and core_new and cb:
            lo, hi = min(len(core_new), len(cb)), max(len(core_new), len(cb))
            if lo / hi < 0.35:
                return None
        return pick
    return None


def match_snapshot(candidates, new_tasks):
    """1:1 선점 매칭. 반환: [cand_index|None] (new_tasks와 같은 길이). 예외는 전파."""
    result = [None] * len(new_tasks)
    claimed = set()
    canons = [canon(t) for t in new_tasks]
    by_canon = {}
    for i, c in enumerate(candidates):
        by_canon.setdefault(c.get("canon") or "", []).append(i)
    dup_new = {c for c in canons if c and canons.count(c) > 1}
    for j, nc in enumerate(canons):            # 1단: 양쪽 유일할 때만 선점
        if not nc or nc in dup_new:
            continue
        hits = [i for i in by_canon.get(nc, []) if i not in claimed]
        if len(hits) == 1:
            result[j] = hits[0]
            claimed.add(hits[0])
    for j in range(len(new_tasks)):            # 2단: 남은 항목 × 남은 후보 (메시지 순 = 결정적)
        if result[j] is not None:
            continue
        remain = [i for i in range(len(candidates)) if i not in claimed]
        if not remain:
            break
        r = match_task([candidates[i] for i in remain], new_tasks[j])
        if r is not None:
            result[j] = remain[r]
            claimed.add(remain[r])
    return result


def _is_progline(it):
    """날짜 없음 + (진행 문구 or 완료) = 진행 보고 줄."""
    return (not it.get("날짜")) and (bool(it.get("진행내용")) or it.get("상태") == "완료")


def merge_snapshot_items(items):
    """canon 동일 그룹에서 '진행줄'만 날짜 있는 항목에 흡수. 날짜 있는 canon 쌍둥이는 별개 유지.
    진행줄끼리(둘 다 날짜 없음)는 첫 항목에 합침."""
    out = []
    by_canon = {}
    for it in items:
        it = dict(it)
        k = canon(it.get("작업내용", ""))
        group = by_canon.get(k, []) if k else []
        target = None
        if group:
            if _is_progline(it):
                target = group[0]                       # 진행줄 → 같은 canon의 앞 항목에 흡수
            else:
                proglines = [g for g in group if _is_progline(g)]
                if proglines:
                    target = proglines[0]               # 앞서 나온 진행줄에 날짜 항목을 얹음
        if target is None:
            out.append(it)
            if k:
                by_canon.setdefault(k, []).append(it)
            continue
        if not target.get("날짜") and it.get("날짜"):
            target["날짜"] = it["날짜"]
        if it.get("진행내용") and not target.get("진행내용"):
            target["진행내용"] = it["진행내용"]
        if "완료" in (target.get("상태"), it.get("상태")):
            target["상태"] = "완료"
        elif it.get("날짜"):
            target["상태"] = it.get("상태") or target.get("상태")
    return out


REPLY_ROUTE_PROMPT = (
    "한 작업자의 '작업 목록'과, 그 작업자가 보낸 '답장'이 있다.\n"
    "답장이 어느 작업에 대한 것인지 판정해라.\n"
    "출력(JSON만): {\"번호\": n} — 작업 하나면 그 번호, "
    "목록 전체에 대한 것이면(다/전부/모두/싹 등) 0, 알 수 없으면 -1.\n\n"
    "예시1)\n작업 목록:\n1. 강님 명함 인쇄\n2. 하늘님 로고 시안\n"
    "답장: 로고 반쯤 그렸어요\n-> {\"번호\": 2}\n\n"
    "예시2)\n작업 목록:\n1. 강님 명함 인쇄\n2. 하늘님 로고 시안\n"
    "답장: 둘 다 끝냈습니다\n-> {\"번호\": 0}\n\n"
    "예시3)\n작업 목록:\n1. 강님 명함 인쇄\n2. 하늘님 로고 시안\n"
    "답장: 오늘 우체국 다녀올게요\n-> {\"번호\": -1}\n\n")


def route_reply(candidates, text):
    """스냅샷 답장 → ('one', idx) | ('all', None) | ('none', None). 전송 실패는 전파."""
    if not candidates:
        return ("none", None)
    if len(candidates) == 1:
        return ("one", 0)                      # 후보 1개면 LLM 생략
    cand_lines = "\n".join(
        f"{i + 1}. {(c.get('task') or '')[:60]}" for i, c in enumerate(candidates))
    out = _judge(REPLY_ROUTE_PROMPT
                 + f"작업 목록:\n{cand_lines}\n답장: {(text or '')[:200]}\n->\n")
    k = out.get("번호")
    if isinstance(k, int) and 1 <= k <= len(candidates):
        return ("one", k - 1)
    if k == 0:
        return ("all", None)
    return ("none", None)


def review(content, who, client, reason):
    return {"tab": "확인필요", "fields": {
        "내용": content[:120], "추정 작업자": who or "", "추정 의뢰자": client or "", "사유": reason}}


def analyze(content, channel_name, author, mentions, context):
    snap = bool(SNAP_HINT_RE.search(content or ""))                  # 스냅샷 힌트 선판정
    text = (content or "")[:SNAP_BODY_MAX if snap else 400]          # 스냅샷은 본문 컷 확장
    if len(text) < 3:
        return None

    # 정산 우선
    if has(text, config.MONEY_HINTS):
        st = extract_settle(text)
        if st is not None:
            who = mentions[0] if mentions else author
            if not st["항목"]:                             # 빈 항목/라벨 echo = 추출 실패
                return review(content, who, nim_in(content), "정산 추출 실패(형식 불명확)")
            recv = nim_in(content) or who
            return {"tab": "정산", "fields": {
                "받는이": recv, "항목": st["항목"], "금액": st["금액"],
                "상태": st["상태"]}}

    # 일정/작업 (멀티화: 0..N건) — 스냅샷 헤더도 일정 신호로 인정
    if has(text, config.DATE_HINTS) or snap:
        # 구조화 일정줄('[날짜] …' 2줄+)은 결정적 파싱 우선 — '전체일정' 헤더가 없어도.
        #   (헤더 없는 리스트를 LLM에 넘기면 과다추출(15건)로 확인필요에 회송되던 실사례)
        rows = parse_structured_snapshot(text)
        if rows is None:
            rows = extract_deadline(text)                  # 0..N건
        if not rows:
            return None                           # 신호는 있었으나 0건 → None

        who = mentions[0] if mentions else author
        client = find_client(content, context)
        # 작업자: 채널명 접두어 > 멘션 > 작성자(본인 보고).
        #   author 폴백이 없던 시절엔 채널명이 '*일정정리*'가 아니고 멘션도 없으면
        #   완벽히 추출된 일정도 통째로 확인필요로 폐기됐다(실측 회송의 약 30%).
        #   작업보고는 본인이 쓰는 게 기본이므로 작성자를 담당자로 인정한다.
        #   (PM이 남의 일정을 대신 적으면 담당자가 PM이 되므로 시트에서 수정)
        worker = worker_from_channel(channel_name) or (mentions[0] if mentions else None) or author

        # 작업자 불명확 = 메시지 단위 → 통째 확인필요 (행 안 쪼갬)
        if not worker:
            return review(content, who, client, "작업자 불명확")

        # 보수 모드: 상한 초과는 과분할/환각 의심 → 확인필요 회송 (스냅샷은 25)
        if len(rows) > (25 if snap else 10):
            return review(content, who, client, f"일정 과다추출({len(rows)}건) — 확인 요망")

        # 메시지 단위 의뢰자(client)는 본문의 '첫 OO님'이라, 한 메시지에 여러 사람 작업이
        #   섞이면 뒷 항목들이 앞사람 이름을 물려받아 '남의 고객'이 붙는다(실측: 표우·카푸 오배정).
        #   → 항목이 2건 이상이면 메시지 폴백을 쓰지 않는다. 틀린 이름보다 빈칸이 낫다(PM이 채움).
        allow_msg_client = (len(rows) == 1)
        items = []
        for r in rows:
            items.append({
                "날짜": norm_date(r["기한"]),
                "작업내용": r["작업내용"],
                "진행내용": r["진행"],            # 빈 문자열 가능 (빈값이면 bot이 키 생략/빈칸)
                "담당자": worker,
                # ⑫ 항목별 "@@님" 우선(다중 의뢰자 리스트 대응) → 의뢰자 사전(PM 교정 학습) → 메시지 의뢰자(단건일 때만)
                "의뢰자": (nim_in(r["작업내용"]) or client_from_task(r["작업내용"])
                          or (client if allow_msg_client else "")),
                "상태": detect_status(r["진행"]),
            })
        return {"tab": "일정", "is_task": True, "items": items}

    return None
