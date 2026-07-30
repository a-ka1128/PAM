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

# ── 아침 다이제스트(PM 브리핑 DM) ──────────────────────────────
#   추출봇 config.py 의 WEBHOOK_URL 과 동일한 Apps Script 웹앱 URL을 넣으면 활성화.
#   비워두면 다이제스트만 자동 OFF(재촉 기능은 정상). 시트 '일정' 탭(파생 데이터)만 읽음.
WEBHOOK_URL = "여기에_AppsScript_웹앱_URL_붙여넣기(추출봇과_동일)"
# 읽기 토큰 — Apps Script 프로젝트 설정 > 스크립트 속성의 READ_TOKEN 과 같은 값.
#   설정하면 URL만 아는 사람은 일정 탭을 못 읽는다. 비워두면 인증 없이 읽음(기존 동작).
READ_TOKEN = ""
DIGEST_HOUR = 9        # KST 몇 시 이후 첫 점검에 하루 1회 발송
