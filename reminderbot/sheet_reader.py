# reminderbot/sheet_reader.py — 시트 탭 읽기(다이제스트 전용, 읽기만).
#   ★ 디스코드 메시지 본문이 아니라 시트의 '파생 데이터'(추출봇이 이미 정제해 올린 일정)만 읽음.
#      리마인더봇 프라이버시 원칙(본문 미열람) 유지.
import json
import urllib.request
import urllib.parse
import config_reminder as config


def fetch(sheet):
    """doGet으로 탭 읽기. 성공: rows 리스트, 실패: {'err':..}. WEBHOOK_URL 미설정이면 즉시 err."""
    if not config.WEBHOOK_URL:
        return {"err": "no-webhook-url"}
    url = config.WEBHOOK_URL + "?sheet=" + urllib.parse.quote(sheet)
    tok = getattr(config, "READ_TOKEN", "")       # Apps Script 스크립트 속성 READ_TOKEN과 동일 값
    if tok:
        url += "&token=" + urllib.parse.quote(tok)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if not isinstance(data, dict) or not data.get("ok"):
            return {"err": (data.get("error") if isinstance(data, dict) else "bad-json")}
        return data.get("rows", [])
    except Exception as e:
        return {"err": str(e)}
