# linkbot/bench_models.py — 골든셋으로 모델별 정확도 비교 (Ollama 필요)
#   실행: venv\python.exe linkbot\bench_models.py exaone3.5:7.8b qwen3:8b ...
#   test_golden의 스위트를 재사용해 모델만 바꿔가며 통과율/소요시간을 표로 출력.
#   ⚠️ qwen3 등 thinking 모델은 think:false로 호출 (본 프롬프트는 즉답형 설계).
import os
import sys
import json
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import brain
import test_golden

# thinking 지원 모델 프리픽스 → think:false 강제 (미지원 모델에 넣으면 400)
THINKING_PREFIXES = ("qwen3", "deepseek-r1", "magistral")


def _make_ollama(model):
    think_off = any(model.startswith(p) for p in THINKING_PREFIXES)

    def _call(prompt, schema=None):
        payload = {"model": model, "prompt": prompt, "stream": False,
                   "keep_alive": "10m", "options": {"temperature": 0}}
        if schema is not None:
            payload["format"] = schema
        if think_off:
            payload["think"] = False
        body = json.dumps(payload).encode("utf-8")
        last = None
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    config.OLLAMA, data=body,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    return json.loads(r.read()).get("response", "").strip()
            except Exception as e:
                last = e
                if attempt == 0:
                    time.sleep(3)
        raise last

    return _call


SUITES = [("deadline", test_golden.run_deadline),
          ("settle", test_golden.run_settle),
          ("same_task", test_golden.run_same),
          ("route", test_golden.run_route)]


def bench(model, show_fails):
    brain.ollama = _make_ollama(model)
    try:
        brain.ollama("핑")                       # 로드 겸 연결 확인
    except Exception as e:
        print(f"  ⚠️ {model} 호출 실패 — 스킵: {e}")
        return None
    row = {"model": model, "suites": {}, "fails": []}
    for name, fn in SUITES:
        t = test_golden.Tally()
        t0 = time.time()
        fn(t)
        row["suites"][name] = (t.p, t.p + t.f, time.time() - t0)
        row["fails"] += t.fails
    if show_fails and row["fails"]:
        for nm, d in row["fails"]:
            print(f"    FAIL {nm}: {d}")
    return row


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    show_fails = "-v" in sys.argv
    models = args or [config.MODEL]
    print(f"모델 벤치마크 — 골든셋 기준, {len(models)}개 모델")
    print("=" * 72)
    rows = []
    for m in models:
        print(f"\n▶ {m}")
        r = bench(m, show_fails)
        if r:
            rows.append(r)
            for s, (p, tot, sec) in r["suites"].items():
                print(f"  {s:10s} {p}/{tot}  ({sec:.0f}s)")

    print("\n" + "=" * 72)
    hdr = f"{'model':34s}" + "".join(f"{s:>11s}" for s, _ in SUITES) + f"{'총계':>9s}"
    print(hdr)
    for r in rows:
        tp = sum(p for p, _, _ in r["suites"].values())
        tt = sum(t for _, t, _ in r["suites"].values())
        line = f"{r['model']:34s}"
        for s, _ in SUITES:
            p, tot, _ = r["suites"][s]
            line += f"{p}/{tot:>2}".rjust(11)
        line += f"{tp}/{tt}".rjust(9)
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
