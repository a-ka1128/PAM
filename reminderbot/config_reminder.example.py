# reminderbot/config_reminder.example.py — 템플릿 (이 파일을 config_reminder.py 로 복사 후 토큰 채우기)
#   cp config_reminder.example.py config_reminder.py   (그리고 TOKEN 입력)
# 추출봇과 완전 분리된 별도 봇/토큰. 본문 인텐트 OFF.

TOKEN = "여기에_리마인더봇_토큰B_붙여넣기"     # ← 리마인더봇 전용 봇 토큰(신규 발급, 추출봇과 다른 봇)

# 모니터링/에러/설정경고 DM 받을 디스코드 유저 ID
OWNER_ID = 343290913172226049

# ★ 추출봇(linkbot)의 '봇 User ID' (Application ID 아님!).
#   확인법: 디스코드 개발자모드 ON → 추출봇(PAM) 멤버 우클릭 → "ID 복사" → 이 값과 같은지 확인.
EXTRACTOR_BOT_USER_ID = 1519559729687429131

# 우선도 이모지 → 재촉 간격(시간). 추출봇 config.py 의 PRIORITY와 반드시 동일하게 유지.
PRIORITY = {"🔴": 12, "🟡": 24, "🔵": 72}
DONE_EMOJI = "✅"

# 재촉 루프 점검 주기(초)
TICK_SECONDS = 60
