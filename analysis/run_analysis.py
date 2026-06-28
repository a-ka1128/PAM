# =========================================================
# 분석 하네스 — data/<서버>.db 들을 읽어 통계 + LLM 분류 + 자동화 제안
#   사용: python analysis/run_analysis.py
#   환경변수: SAMPLE(표본 크기, 기본 40), MODEL(기본 qwen2.5:7b)
#   전부 로컬. LLM 호출도 로컬 Ollama. 리포트는 analysis/report.md (집계만).
# =========================================================
import os
import sys
import glob
import sqlite3
import random
import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm_client as llm

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
KST = datetime.timezone(datetime.timedelta(hours=9))
DOW = ["월", "화", "수", "목", "금", "토", "일"]

MODEL = os.environ.get("MODEL", "qwen2.5:7b")
SAMPLE_SIZE = int(os.environ.get("SAMPLE", "40"))
CATEGORIES = ["질문", "잡담", "건의", "버그제보", "공지", "정보공유", "기타"]


def load_db(path):
    con = sqlite3.connect(path)
    msgs = pd.read_sql_query("SELECT * FROM messages", con)
    chans = pd.read_sql_query(
        "SELECT channel_id, channel_name, MAX(captured_at) ca FROM channel_snapshots GROUP BY channel_id", con)
    ments = pd.read_sql_query(
        "SELECT target_name, COUNT(*) c FROM mentions WHERE target_type='user' "
        "GROUP BY target_id ORDER BY c DESC LIMIT 5", con)
    react = con.execute("SELECT COUNT(*) FROM reactions").fetchone()[0]
    con.close()
    return msgs, chans, ments, react


def analyze_server(path, report):
    name = os.path.basename(path).replace(".db", "")
    msgs, chans, ments, react = load_db(path)
    report.append(f"\n## 📁 {name}")
    if msgs.empty:
        report.append("- (메시지 없음)")
        return None

    total = len(msgs)
    human = msgs[msgs["is_bot"] == 0]
    report.append(f"- 총 메시지 **{total}** (사람 {len(human)} / 봇 {total - len(human)}) · 반응 {react}")

    chanmap = dict(zip(chans["channel_id"], chans["channel_name"]))
    # 채널 활동: 이름 기준으로 합산(스레드/동명 채널 병합)
    named = {}
    for cid, n in msgs.groupby("channel_id").size().items():
        nm = chanmap.get(cid, str(cid))
        named[nm] = named.get(nm, 0) + int(n)
    top_named = sorted(named.items(), key=lambda x: -x[1])[:8]
    report.append("- **채널별 활동(상위)**: " + ", ".join(f"#{nm}({n})" for nm, n in top_named))

    dt = pd.to_datetime(msgs["created_at"], utc=True, errors="coerce").dt.tz_convert(KST)
    byhour = dt.dt.hour.value_counts().sort_index()
    peak = int(byhour.idxmax())
    byd = dt.dt.dayofweek.value_counts().sort_index()
    report.append(f"- **피크 시간대(KST)**: {peak}시 · **요일별**: " +
                  ", ".join(f"{DOW[int(d)]}{c}" for d, c in byd.items()))

    topa = human["author_name"].value_counts().head(5)
    report.append("- **활발한 사람(상위5)**: " + ", ".join(f"{a}({c})" for a, c in topa.items()))

    report.append(f"- 링크포함 {msgs['has_link'].mean() * 100:.0f}% · "
                  f"첨부 {msgs['has_attachment'].mean() * 100:.0f}% · "
                  f"답글 {msgs['is_reply'].mean() * 100:.0f}%")

    domc = {}
    for d in msgs["link_domains"].dropna():
        for x in str(d).split(","):
            if x:
                domc[x] = domc.get(x, 0) + 1
    if domc:
        top_doms = sorted(domc.items(), key=lambda x: -x[1])[:5]
        report.append("- **자주 공유된 도메인**: " + ", ".join(f"{d}({c})" for d, c in top_doms))

    if not ments.empty:
        report.append("- **자주 멘션되는 사람**: " +
                      ", ".join(f"{r.target_name}({r.c})" for r in ments.itertuples()))

    # LLM 분류 (표본)
    pool = human["content"].dropna()
    pool = pool[pool.str.len() >= 2].tolist()
    random.seed(42)
    sample = random.sample(pool, min(SAMPLE_SIZE, len(pool)))
    dist = {}
    for txt in sample:
        cat = llm.classify(MODEL, txt, CATEGORIES)
        dist[cat] = dist.get(cat, 0) + 1
    report.append(f"- **메시지 유형 분포** (표본 {len(sample)}건, {MODEL}): " +
                  ", ".join(f"{cat} {c}({c / len(sample) * 100:.0f}%)"
                            for cat, c in sorted(dist.items(), key=lambda x: -x[1])))

    return {"name": name, "total": total, "peak_hour": peak, "dist": dist,
            "top_channels": top_named[:5],
            "link_rate": round(float(msgs["has_link"].mean() * 100)),
            "att_rate": round(float(msgs["has_attachment"].mean() * 100))}


def main():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*_*.db")))
    report = ["# 📊 Collector 분석 리포트",
              f"모델: {MODEL} · 서버당 표본 {SAMPLE_SIZE}건 · (통계는 전수)"]
    if not files:
        report.append("\n(data/ 에 DB 파일이 없습니다)")
        print("NO_DATA")
    summaries = []
    for f in files:
        try:
            s = analyze_server(f, report)
            if s:
                summaries.append(s)
        except Exception as e:
            report.append(f"\n## {os.path.basename(f)}\n- 분석 오류: {e}")

    if summaries:
        agg = []
        for s in summaries:
            agg.append(f"[{s['name']}] 총 {s['total']}건, 피크 {s['peak_hour']}시(KST), "
                       f"메시지유형 {s['dist']}, 상위채널 {s['top_channels']}, "
                       f"링크율 {s['link_rate']}%, 첨부율 {s['att_rate']}%")
        report.append("\n## 🤖 자동화 제안 (로컬 LLM, 집계만 투입)")
        try:
            report.append(llm.suggest_automation(MODEL, "\n".join(agg)))
        except Exception as e:
            report.append(f"(제안 생성 실패: {e})")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    print("REPORT_WRITTEN", out)


if __name__ == "__main__":
    main()
