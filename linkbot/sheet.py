# linkbot/sheet.py — 추출 항목을 구글 시트 웹훅으로 upsert 전송
import json
import time
import urllib.error
import urllib.request
import urllib.parse
import config

# Apps Script는 LockService로 모든 요청을 직렬화한다. 봇이 몇 분마다 확인필요를 폴링하고
#   쓰기까지 겹치면 대기가 길어지다 GAS가 404/5xx를 뱉는다(실측: 8회 중 1회 실패,
#   실패 직전 응답이 14~32초로 늘어짐). GAS 쪽 배치 쓰기로 락 점유는 짧아졌지만
#   구글 프런트의 무작위 404는 여전히 있어(2026-09-13: 쓰기 없는 시간대에도 발생),
#   2회 시도·3초 대기로는 30초짜리 혼잡을 못 넘긴다 → 3회, 대기는 3초 → 10초로 늘려 잡는다.
_RETRY = 3                      # 총 시도 횟수
_RETRY_WAITS = (3.0, 10.0)      # n번째 실패 후 대기(초) — 뒤로 갈수록 길게(락이 풀릴 시간)
_TRANSIENT = (404, 429, 500, 502, 503, 504)


class _Locked(Exception):
    """doGet이 락 대기(20s)를 못 이기고 {"ok":false,"error":"locked"}를 돌려준 경우 — 일시적이라 재시도 대상."""


def _transient(e):
    """다시 시도할 가치가 있는 오류인가 (일시적 혼잡/타임아웃)."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in _TRANSIENT
    return isinstance(e, (urllib.error.URLError, TimeoutError, OSError, _Locked))


def _request(make_call):
    """make_call() 을 일시적 오류에 한해 재시도. 마지막 예외는 그대로 올린다."""
    last = None
    for attempt in range(_RETRY):
        try:
            return make_call()
        except Exception as e:
            last = e
            if attempt + 1 >= _RETRY or not _transient(e):
                break
            time.sleep(_RETRY_WAITS[min(attempt, len(_RETRY_WAITS) - 1)])
    raise last


def push(sheet, items, mode="upsert"):
    """items = [{'key': '<msg_id>#<i>', 'fields': {...}}]. mode: 'upsert'(전체덮기) | 'merge'(부분병합)."""
    if not items:
        return None
    body = json.dumps({"sheet": sheet, "mode": mode, "items": items}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        config.WEBHOOK_URL, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    def call():
        with urllib.request.urlopen(req, timeout=90) as r:      # GAS 콜드스타트/락 대기 대응 (구 30s는 부족)
            return r.read().decode("utf-8", "replace")
    try:
        return _request(call)
    except Exception as e:
        return f"err:{e}"


def fetch(sheet):
    """doGet으로 탭 읽기. 성공: rows 리스트, 실패: {'err':..}
    READ_TOKEN: Apps Script 쪽 스크립트 속성과 같은 값을 config에 두면 인증 읽기가 된다
    (미설정이면 기존처럼 토큰 없이 요청 — 점진 배포)."""
    url = config.WEBHOOK_URL + "?sheet=" + urllib.parse.quote(sheet)
    tok = getattr(config, "READ_TOKEN", "")
    if tok:
        url += "&token=" + urllib.parse.quote(tok)
    def call():
        with urllib.request.urlopen(url, timeout=120) as r:     # doGet은 콜드스타트+락으로 최대 ~74s 관측 → 120s
            data = json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("error") == "locked":
            raise _Locked("locked")                             # 락 경합 → _request가 다시 시도
        return data
    try:
        data = _request(call)
        if not isinstance(data, dict) or not data.get("ok"):
            return {"err": (data.get("error") if isinstance(data, dict) else "bad-json")}
        return data.get("rows", [])
    except _Locked:
        return {"err": "locked"}
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
    def call():
        with urllib.request.urlopen(req, timeout=90) as r:      # 삭제도 락 직렬화라 느릴 수 있음 → 90s
            return r.read().decode("utf-8", "replace")
    try:
        return _request(call)
    except Exception as e:
        return f"err:{e}"
