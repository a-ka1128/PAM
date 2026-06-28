# linkbot/sheet.py — 추출 항목을 구글 시트 웹훅으로 upsert 전송
import json
import urllib.request
import urllib.parse
import config


def push(sheet, items, mode="upsert"):
    """items = [{'key': '<msg_id>#<i>', 'fields': {...}}]. mode: 'upsert'(전체덮기) | 'merge'(부분병합)."""
    if not items:
        return None
    body = json.dumps({"sheet": sheet, "mode": mode, "items": items}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        config.WEBHOOK_URL, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"err:{e}"


def fetch(sheet):
    """doGet으로 탭 읽기. 성공: rows 리스트, 실패: {'err':..}"""
    url = config.WEBHOOK_URL + "?sheet=" + urllib.parse.quote(sheet)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if not isinstance(data, dict) or not data.get("ok"):
            return {"err": (data.get("error") if isinstance(data, dict) else "bad-json")}
        return data.get("rows", [])
    except Exception as e:
        return {"err": str(e)}


def delete_rows(sheet, keys):
    """doPost(action=delete)로 _key 일치 행 삭제."""
    if not keys:
        return None
    body = json.dumps({"sheet": sheet, "action": "delete", "keys": keys},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(config.WEBHOOK_URL, data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"err:{e}"
