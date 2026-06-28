# 로컬 LLM(Ollama) 클라이언트 — 분석 하네스용
#   전부 localhost:11434 로컬 호출. 외부 전송 없음.
import urllib.request
import json

OLLAMA_URL = "http://localhost:11434/api/generate"


def generate(model, prompt, timeout=240, options=None):
    body = {"model": model, "prompt": prompt, "stream": False}
    if options:
        body["options"] = options
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read())
    return res.get("response", "").strip()


def classify(model, text, categories):
    """메시지 1건을 카테고리 중 하나로 분류."""
    cats = ", ".join(categories)
    prompt = (f"다음 디스코드 메시지를 [{cats}] 중 정확히 하나로만 분류해. "
              f"카테고리 단어 하나만 출력하고 다른 설명은 절대 하지 마.\n\n"
              f"메시지: \"{str(text)[:500]}\"")
    out = generate(model, prompt, options={"temperature": 0})
    for c in categories:
        if c in out:
            return c
    return "기타"


def suggest_automation(model, stats_text):
    """집계 통계(원문 아님)를 보고 자동화 아이디어 제안."""
    prompt = (
        "너는 디스코드 서버 운영 분석가야. 아래는 여러 서버의 활동 통계 요약이야(원문 메시지 아님). "
        "이 통계를 근거로 '봇으로 자동화하면 유용할 것' 3~5가지를 제안해줘. "
        "각 항목은 한국어 한 줄로, 어떤 통계 근거 때문인지 함께 적어줘.\n\n"
        f"{stats_text}"
    )
    return generate(model, prompt, timeout=300)
