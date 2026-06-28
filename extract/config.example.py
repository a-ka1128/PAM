# config.example.py → config.py 로 복사 (config.py 는 gitignore됨)
# 추출(A 엔진) 설정

MODEL = "qwen2.5:7b"        # 추출에 쓸 로컬 LLM
N_TARGET = 10              # 뽑을 총 항목 수 (마감+정산 합쳐서 대략)
SCAN_LIMIT = 80           # 채널그룹별 스캔할 최근 메시지 수
CONTEXT_WINDOW = 3        # 의뢰자("님") 찾을 때 같은 채널에서 앞뒤로 볼 메시지 수

DEADLINE_KEYWORD = "일정"  # 이 단어 들어간 채널 = 마감/일정 소스
SETTLE_KEYWORD = "정산"    # 이 단어 들어간 채널 = 정산 소스

# "OO님"에서 사람으로 안 칠 보통명사 (이 단어 + 님 은 무시)
NIM_BLOCKLIST = ["고객", "손님", "선생", "여러분", "회원", "운영자", "관리자", "모두", "대표", "사장", "팀장"]

# 구글 시트 동기화용 Apps Script 웹앱 URL (비우면 CSV만 생성)
WEBHOOK_URL = ""
