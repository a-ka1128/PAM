# linkbot/dedup_cleanup.py — 이미 시트에 쌓인 '같은 작업' 중복 행 정리 도구
#   실행(스캔만, 안전):  venv\python.exe linkbot\dedup_cleanup.py
#   실행(실제 삭제):     venv\python.exe linkbot\dedup_cleanup.py --apply
#
#   왜 필요한가: 라이브 경로의 중복 가드가 canon 완전일치만 보던 시절에 같은 작업이
#   2~3중으로 등록됐다(예: '요나일님 의상 제작' / '요나일님 의상 진행 중').
#   그 버그는 고쳤지만 이미 쌓인 행은 남아 있어, 이 도구로 사후 정리한다.
#
#   판정은 봇과 같은 엔진(brain.match_task: 인물 선필터 → 포함관계 → LLM → 유사도 백스톱).
#   담당자별로만 비교한다(남의 작업과 절대 안 섞임). 완료 행은 건드리지 않는다.
#
#   남길 행 선정: 정보가 가장 많은 행(빈칸이 적은 행) > 동률이면 먼저 등록된 행(key 순).
#   ⚠️ --apply는 시트 행을 실제로 지운다. 반드시 스캔 결과를 먼저 눈으로 확인할 것.
import os
import sys
import argparse
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brain
import sheet

FIELDS = ("날짜", "작업내용", "진행내용", "담당자", "의뢰자", "상태")
SKIP_STATUS = {"완료"}            # 완료 행은 이력이므로 정리 대상 제외


def _info_score(row):
    """정보량 = 비어있지 않은 주요 필드 수 (많을수록 남길 가치)."""
    return sum(1 for f in FIELDS if (row.get(f) or "").strip())


def _key_order(row):
    """등록 순서 — key '<msg_id>#<n>' 의 msg_id, n 기준."""
    k = str(row.get("key") or "")
    mid, _, idx = k.partition("#")
    try:
        return (int(mid), int(idx or 0))
    except ValueError:
        return (0, 0)


def find_groups(rows):
    """담당자별로 같은 작업 묶음을 찾는다. 반환 [(keep_row, [dup_row, ...]), ...]"""
    by_worker = defaultdict(list)
    for r in rows:
        if (r.get("상태") or "").strip() in SKIP_STATUS:
            continue
        if not (r.get("작업내용") or "").strip():
            continue
        by_worker[(r.get("담당자") or "").strip()].append(r)

    groups = []
    for worker, items in sorted(by_worker.items()):
        items = sorted(items, key=_key_order)          # 결정적 순서
        buckets = []                                    # [[row, ...], ...]
        for row in items:
            task = row["작업내용"]
            cands = [{"canon": brain.canon(b[0]["작업내용"]), "task": b[0]["작업내용"]}
                     for b in buckets]
            hit = None
            try:
                hit = brain.match_task(cands, task) if cands else None
            except Exception as e:
                print(f"  ⚠️ 매칭 실패(건너뜀): {task[:30]} — {e}")
            if hit is None:
                buckets.append([row])
            else:
                buckets[hit].append(row)
        for b in buckets:
            if len(b) < 2:
                continue
            # 남길 행: 정보량 최다 → 동률이면 먼저 등록된 것
            keep = sorted(b, key=lambda r: (-_info_score(r), _key_order(r)))[0]
            dups = [r for r in b if r is not keep]
            groups.append((worker, keep, dups))
    return groups


def _fmt(row, mark):
    return (f"   {mark} [{(row.get('날짜') or '').strip() or '날짜없음':23s}] "
            f"{(row.get('작업내용') or '').strip()[:34]:34s} "
            f"진행={(row.get('진행내용') or '').strip()[:12]:12s} "
            f"의뢰자={(row.get('의뢰자') or '').strip()[:8]:8s} key={row.get('key')}")


def _client_warn(keep, dups):
    """남길 행의 의뢰자가 작업명이 가리키는 인물과 어긋나면 경고 (틀린 의뢰자 보존 방지)."""
    named = brain.nim_in(keep.get("작업내용") or "") or ""
    cur = (keep.get("의뢰자") or "").strip()
    if named and cur and named != cur:
        return f"      ⚠️ 남길 행 의뢰자='{cur}' 인데 작업명은 '{named}님' — 시트에서 확인 필요"
    others = {(d.get("의뢰자") or "").strip() for d in dups} - {"", cur}
    if cur and others:
        return f"      ⚠️ 의뢰자 불일치: 남김='{cur}' vs 삭제될 행={sorted(others)} — 확인 후 적용"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제로 시트에서 중복 행 삭제")
    ap.add_argument("--groups", default="", help="적용할 묶음 번호만 (예: 1,3,4). 미지정=전체")
    args = ap.parse_args()
    pick = {int(x) for x in args.groups.replace(" ", "").split(",") if x.isdigit()}

    print("일정 탭 읽는 중…")
    rows = sheet.fetch("일정")
    if isinstance(rows, dict):
        print(f"❌ 시트 읽기 실패: {rows.get('err')}")
        return 1
    print(f"총 {len(rows)}행 (완료 제외하고 담당자별 비교)\n")

    groups = find_groups(rows)
    if not groups:
        print("✅ 중복으로 판정된 묶음이 없습니다.")
        return 0

    for n, (worker, keep, dups) in enumerate(groups, 1):
        sel = "" if not pick else ("  ← 적용대상" if n in pick else "  (제외)")
        print(f"[{n}] 담당 {worker or '(없음)'}{sel}")
        print(_fmt(keep, "유지"))
        for d in dups:
            print(_fmt(d, "삭제"))
        w = _client_warn(keep, dups)
        if w:
            print(w)
        print()

    targets = [g for n, g in enumerate(groups, 1) if not pick or n in pick]
    total_dup = sum(len(d) for _, _, d in targets)
    print(f"묶음 {len(groups)}개 발견 / 적용 대상 {len(targets)}묶음 · {total_dup}행 삭제")

    if not args.apply:
        print("\n(스캔만 했습니다 — 아무것도 지우지 않았습니다)")
        print("전체 적용:      dedup_cleanup.py --apply")
        print("일부만 적용:    dedup_cleanup.py --apply --groups 2,4,5")
        return 0

    keys = [d["key"] for _, _, dups in targets for d in dups]
    print(f"\n{len(keys)}행 삭제 중…")
    r = sheet.delete_rows("일정", keys)
    print("시트 응답:", r)
    if isinstance(r, str) and r.startswith("err"):
        print("❌ 삭제 실패 — 시트는 변경되지 않았을 수 있습니다.")
        return 1
    # 로컬 레지스트리도 같이 정리 (봇 내부 상태와 시트 불일치 방지)
    try:
        import state
        removed = 0
        for ch in {c["channel_id"] for c in _all_reg(state)}:
            ks = [k for k in keys if any(
                c["row_key"] == k for c in state.reg_channel_items(ch))]
            if ks:
                state.reg_delete(ch, ks)
                removed += len(ks)
        print(f"로컬 레지스트리 {removed}행 정리 완료")
    except Exception as e:
        print(f"⚠️ 레지스트리 정리 실패(시트는 정리됨): {e}")
    print("✅ 완료")
    return 0


def _all_reg(state):
    """모든 채널의 레지스트리 행 (channel_id 수집용)."""
    c = state._con()
    rows = c.execute("SELECT DISTINCT channel_id FROM snap_reg").fetchall()
    c.close()
    return [{"channel_id": r[0]} for r in rows]


if __name__ == "__main__":
    sys.exit(main())
