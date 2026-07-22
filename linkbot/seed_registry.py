# linkbot/seed_registry.py — 시트 '일정' 탭의 레거시 행을 스냅샷 레지스트리에 편입 (1회 실행)
#
#   왜: 레지스트리(snap_reg) 도입(2026-07-16) 이전에 push된 시트 행은 레지스트리에 없어서
#       중복 가드(_canon_dup_foreign)·스냅샷 1단 매칭·확인필요 병합이 전부 그 행을 못 본다.
#       → 같은 작업이 다시 언급되면 새 행이 또 생긴다 (시트에 보이는 중복 쌍의 주원인).
#       이 스크립트가 시트의 현재 행을 전부 레지스트리에 편입해 그 구멍을 막는다.
#
#   사용 (봇 정지 후, linkbot/ 디렉토리에서):
#     python seed_registry.py           ← dry-run: 뭘 편입할지 출력만
#     python seed_registry.py --apply   ← 실제 편입
#
#   부수 효과: 'canon 중복(잔재)'로 분류된 행 목록 = 시트에 이미 쌓인 중복 행 후보.
#       봇은 시트 행을 임의 삭제하지 않으므로(자동 상태변경 금지 원칙) PM이 보고 수동 삭제.
import re
import sys

import brain
import sheet
import state

sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # cp949 콘솔에서 특수문자 크래시 방지

_LINK = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")


def main(apply=False):
    rows = sheet.fetch("일정")
    if isinstance(rows, dict):
        print("시트 읽기 실패:", rows.get("err"))
        return 1

    add, dups, skip_reg, skip_nolink = [], [], 0, 0
    seen_canon = set()                       # dry-run에서도 apply와 같은 판정이 나오게 로컬 추적
    for r in rows:
        key = str(r.get("key") or "")
        task = (r.get("작업내용") or "").strip()
        base = key.split("#")[0]
        if not task or not base.isdigit():   # 빈 행/수기 시드 행(seed-*, wh-*)은 대상 아님
            continue
        m = _LINK.search(r.get("원본링크") or "")
        if not m:
            skip_nolink += 1
            continue
        ch_id = int(m.group(2))
        if any(x["row_key"] == key for x in state.reg_items_by_src(int(base))):
            skip_reg += 1                    # 이미 레지스트리가 아는 행
            continue
        cn = brain.canon(task)
        status = (r.get("상태") or "").strip()
        st = "완료" if "완료" in status else "진행중"   # 가드가 보려면 활성(진행중)이어야 함
        if cn and ((ch_id, cn) in seen_canon
                   or state.reg_find_by_canon_all(ch_id, cn, active_only=False)):
            dups.append((ch_id, key, task, status))      # 같은 canon 이미 존재 = 중복 잔재 후보
            continue
        seen_canon.add((ch_id, cn))
        add.append((ch_id, key, cn, task, (r.get("날짜") or "").strip(), st))

    print(f"편입 대상 {len(add)}건 / 이미등록 {skip_reg} / 링크없음 {skip_nolink} / 중복잔재 후보 {len(dups)}건")
    for ch_id, key, cn, task, date, st in add:
        print(f"  [편입] {key}  {task!r}  ({st})")
        if apply:
            state.reg_enroll(ch_id, key, cn, task, date, st, managed=0)
    if dups:
        print("\n중복 잔재 후보 (같은 canon이 이미 등록됨 — PM이 시트에서 수동 삭제 검토):")
        for ch_id, key, task, status in dups:
            print(f"  [중복?] {key}  {task!r}  ({status or '상태없음'})")
    print("\n편입 완료." if apply else "\ndry-run — 실제 편입은 --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv[1:]))
