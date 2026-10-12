# linkbot/config.example.py → config.py 로 복사해서 채우기 (config.py 는 gitignore됨)
#   추출봇(PAM) 설정. 항목 설명은 운영가이드.md §7 참고.

# 디스코드 봇 토큰 (노출 시 개발자포털에서 Reset → 여기 교체)
TOKEN = "여기에_봇_토큰_붙여넣기"

# 알림 DM 받고 슬래시 명령(/상태, /과거등록)을 쓸 수 있는 디스코드 유저 ID
OWNER_ID = 0

# 구글 시트 Apps Script 웹앱 URL
WEBHOOK_URL = ""
# 웹앱 API 토큰 — Apps Script 프로젝트 설정 > 스크립트 속성의 API_TOKEN 과 같은 값 (필수)
API_TOKEN = ""

# 로컬 LLM (Ollama)
OLLAMA = "http://localhost:11434/api/generate"
MODEL = "exaone3.5:7.8b"
NUM_CTX = 8192            # 컨텍스트 길이 — 4096이면 긴 프롬프트 앞부분이 잘림
THINK_JUDGE = False       # '같은 작업인가' 판단 호출에 thinking 사용 (느려짐)

# 우선도 이모지 → 재촉 간격(시간). 비우면 이모지 부착 안 함(리마인더봇 운영 종료 상태).
#   되살리려면 {"🔴": 12, "🟡": 24, "🔵": 72} — 리마인더봇 config_reminder.py 의 PRIORITY와 동일하게.
PRIORITY = {}

# 사전필터 키워드 — 하나라도 들어간 메시지만 LLM 추출로 넘긴다
DATE_HINTS = ["일정", "마감", "까지", "기한", "납기", "제출", "오늘", "내일", "모레", "이번주", "다음주", "월", "일", "/"]
MONEY_HINTS = ["정산", "입금", "송금", "금액", "원", "만원"]

# "OO님"에서 사람으로 안 칠 보통명사
NIM_BLOCKLIST = ["고객", "손님", "선생", "여러분", "회원", "운영자", "관리자", "모두", "대표", "사장", "팀장"]

# 추출 대상 카테고리 (부분일치). 비우면 전체 채널.
ALLOWED_CATEGORIES = []
# 추출에서 제외할 채널 ID
EXCLUDED_CHANNEL_IDS = []

# 첫 기동(last_seen 없음) 때 따라잡을 일수
CATCHUP_COLD_START_DAYS = 3
