# linkbot/bot.py — 룩플 통합 봇 (Tier1 신뢰성 포함)
#   실시간: 모든 채널 → 추출(brain) → 시트(sheet) upsert + 작업/마감엔 🔴🟡🔵 부착
#   재촉: 이모지 클릭 → 추적 → ✅/답장까지 알림
#   [Tier1] 추적상태 SQLite 영속 / 재시작 시 놓친 메시지 따라잡기 / 크래시·Ollama·푸시 실패 시 DM 알림
import os
import re
import sys
import time
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from collections import deque, defaultdict
import discord
from discord import app_commands
from discord.ext import tasks

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config
import brain
import sheet
import state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
    handlers=[logging.FileHandler(os.path.join(HERE, "linkbot.log"), encoding="utf-8", delay=True),
              logging.StreamHandler()])
log = logging.getLogger("linkbot")

# 단일 인스턴스 락 (중복 실행 방지) — 포트 점유. 두 번째 인스턴스는 바로 종료.
import socket
try:
    _LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _LOCK.bind(("127.0.0.1", 47291))
    _LOCK.listen(1)
except OSError:
    log.warning("이미 다른 인스턴스가 실행 중 → 이 인스턴스 종료")
    sys.exit(0)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

ctx_buf = defaultdict(lambda: deque(maxlen=8))
flagged = set()
_last_alert = {}
_fail_count = {}    # 키별 연속 실패 횟수 (성공 시 clear_err로 리셋)


def jump(g, c, m):
    return f"https://discord.com/channels/{g}/{c}/{m}"


async def alert(text):
    try:
        u = client.get_user(config.OWNER_ID) or await client.fetch_user(config.OWNER_ID)
        if u:
            await u.send(text)
    except Exception:
        log.exception("alert(DM) 실패")


async def alert_err(key, text, threshold=1):
    _fail_count[key] = _fail_count.get(key, 0) + 1
    # DM만 보내면 나중에 원인 추적이 불가능하다(실측: 시트 404 DM이 왔는데 로그에 흔적 0).
    #   레이트리밋에 걸려 DM을 안 보내는 경우에도 로그에는 반드시 남긴다.
    log.warning(f"[{key}] {text} (연속 {_fail_count[key]}회)")
    if _fail_count[key] < threshold:          # 연속 threshold회 미만 = 일시 블립 → 조용히 넘김
        return
    now = time.time()
    if now - _last_alert.get(key, 0) < 600:   # 같은 종류 알림은 10분에 1번
        return
    _last_alert[key] = now
    await alert(text)


def clear_err(key):
    _fail_count[key] = 0


# ── 스냅샷(전체일정) 공용 헬퍼 ──
_chan_locks = defaultdict(asyncio.Lock)     # 채널별 직렬화: diff·답장 라우팅·편입 전 구간

_LINK_RE = re.compile(r"discord\.com/channels/(\d+)/(\d+)/(\d+)")


def _cat_name(ch):
    """채널이 속한 카테고리 이름. 스레드면 부모 채널 기준. 없으면 ''.
    음성채널의 텍스트챗(VoiceChannel)도 category를 가지므로 동일하게 처리된다."""
    cat = getattr(ch, "category", None)
    if cat is None:
        parent = getattr(ch, "parent", None)          # 스레드 → 부모 텍스트채널
        cat = getattr(parent, "category", None) if parent is not None else None
    return getattr(cat, "name", "") or ""


def _norm_cat(s):
    """카테고리 이름 비교용 정규화 — 이모지·구분자(ㅣ)·공백을 떼고 비교.
    실제 이름이 '🗓️ㅣ일정 관리'처럼 장식이 붙어 있어 정확일치로는 못 맞춘다."""
    t = re.sub(r"[\s|ㅣ｜/·・\-_]", "", (s or "").lower())
    return re.sub(r"[^0-9a-z가-힣]", "", t)


def _channel_allowed(ch):
    """이 채널을 추출 대상으로 볼 것인가.
    config.ALLOWED_CATEGORIES가 비어 있으면 전체 허용(기존 동작), 채워져 있으면
    그 카테고리 하위만 처리한다 — 잡담·음성채널 채팅에까지 이모지가 붙던 문제 차단."""
    allow = getattr(config, "ALLOWED_CATEGORIES", None) or []
    if not allow:
        return True
    cat = _norm_cat(_cat_name(ch))
    if not cat:
        return False                                   # 카테고리 없는 채널 = 대상 아님
    return any(_norm_cat(a) in cat for a in allow)


def _channel_from_link(link):
    m = _LINK_RE.search(link or "")
    return int(m.group(2)) if m else None


def _kst_date(msg):
    return (msg.created_at + timedelta(hours=9)).strftime("%Y-%m-%d")


def _is_snapshot(msg_id, content, items):
    if state.is_snap_msg(msg_id):
        return True                          # sticky (재추출 흔들려도 스냅샷 유지)
    return bool(brain.SNAP_HINT_RE.search(content or "")) and len(items) >= 2


def _snap_hw(mid):
    """스냅샷 신규 키 시작 인덱스 (재사용 금지 단조 할당)."""
    idx = [int(r["row_key"].rsplit("#", 1)[1]) + 1 for r in state.reg_items_by_src(mid)]
    return max([state.get_msg_item_count(mid), 0] + idx)


DUP_MATCH_CAND_MAX = 30       # 2단 매칭에 넘길 최대 후보 수 (프롬프트 비대·지연 방지)


def _canon_dup_foreign(ch_id, mid, it):
    """다른 메시지 소유의 '같은 작업' 활성 행이 이미 있는가 (중복 등록 방지).

    1단 canon 완전일치 → 2단 스마트 매칭(match_task: 인물 선필터 → 포함관계 →
    LLM 판정 → 유사도 백스톱). 1단만 있던 시절엔 글자가 조금만 달라도
    ('요나일님 의상 제작' vs '요나일님 의상 진행 중') 전부 새 행이 되어
    같은 작업이 2~3중으로 쌓였다. 2단은 스냅샷 경로에서 검증된 같은 엔진이다.
    """
    task = it.get("작업내용", "")
    cn = brain.canon(task)
    if not cn:
        return False
    hits = state.reg_find_by_canon_all(ch_id, cn)
    if any(not h["row_key"].startswith(f"{mid}#") for h in hits):
        return True                                   # 1단: 완전일치
    # 2단: 다른 메시지 소유의 활성 후보와 스마트 매칭
    cands = [c for c in state.reg_channel_items(ch_id, active_only=True)
             if not c["row_key"].startswith(f"{mid}#")]
    if not cands:
        return False
    cands = cands[-DUP_MATCH_CAND_MAX:]               # 정렬은 불변키 → 재현성 유지
    try:
        return brain.match_task(
            [{"canon": c["canon"], "task": c["task_text"]} for c in cands], task) is not None
    except Exception:
        log.exception("중복 2단 매칭 실패 — 새 행으로 처리")
        return False                                  # LLM 실패 시 보수적으로 등록(유실 방지)


async def _drain_outbox():
    """자동완료 merge / 정리 delete 재시도 대기열 드레인 (ok:0 = 행 없음 = 목적 달성)."""
    ops = await asyncio.to_thread(state.outbox_pending)
    done_ids = []
    for op in ops:
        if op["kind"] == "done":
            r = await asyncio.to_thread(sheet.push, op["tab"],
                    [{"key": op["row_key"], "fields": {"상태": "완료"}}], "merge")
        else:   # 'del'
            r = await asyncio.to_thread(sheet.delete_rows, op["tab"], [op["row_key"]])
        if not (isinstance(r, str) and r.startswith("err")):
            done_ids.append(op["id"])
    await asyncio.to_thread(state.outbox_del, done_ids)


@client.event
async def on_ready():
    global flagged
    try:
        await tree.sync()
    except Exception:
        log.exception("명령어 sync 실패")
    flagged = state.load_flagged()
    log.info(f"login: {client.user} | servers {len(client.guilds)} | 플래그 {len(flagged)}")
    if str(state.get_kv("canon_ver", "")) != str(brain.CANON_VERSION):
        await asyncio.to_thread(state.reg_recanon, brain.canon, brain.CANON_VERSION)
        log.info(f"canon 재계산 완료 (v{brain.CANON_VERSION})")
    if not resolve_loop.is_running():
        resolve_loop.start()
    n = await catchup()
    # 시작 알림 (크래시 루프 스팸 방지: 영속 레이트리밋 5분)
    last_a = float(state.get_kv("last_startup_alert", 0) or 0)
    if time.time() - last_a > 300:
        state.set_kv("last_startup_alert", time.time())
        await alert(f"✅ 룩플봇(추출) 시작 (서버 {len(client.guilds)}) — 놓친 메시지 {n}건 따라잡음")


async def catchup():
    """봇이 꺼진 동안 올라온 메시지를 따라잡아 추출(이모지는 안 붙임)."""
    last = state.get_kv("last_seen")
    if not last:
        # 최초 실행(새 PC/DB): 그냥 스킵하면 이사 동안 쌓인 backlog가 통째로 유실됨.
        # 설정된 일수만큼 거슬러 올라가 따라잡는다 (0이면 기존처럼 스킵).
        cold_days = getattr(config, "CATCHUP_COLD_START_DAYS", 3)
        if cold_days <= 0:
            state.set_kv("last_seen", time.time())
            return 0
        after = datetime.now(timezone.utc) - timedelta(days=cold_days)
        log.info(f"catchup: 최초 실행 → 최근 {cold_days}일 따라잡기")
    else:
        after = datetime.fromtimestamp(float(last), tz=timezone.utc)
    count = 0
    for guild in client.guilds:
        for ch in getattr(guild, "text_channels", []):
            if not _channel_allowed(ch):        # 대상 밖 채널은 히스토리도 읽지 않는다(불필요한 API/LLM 비용)
                continue
            try:
                async for msg in ch.history(after=after, limit=200, oldest_first=True):
                    if msg.author.bot:
                        continue
                    ref = msg.reference.message_id if msg.reference else None
                    consumed = False
                    if ref:
                        consumed, _ = await _apply_reply_update(msg, ref)
                    if not consumed and await process(msg, live=False):
                        count += 1
            except Exception:
                pass
    state.set_kv("last_seen", time.time())
    if count:
        log.info(f"catchup: {count}건 따라잡음")
    return count


async def process(msg, live=True, is_edit=False):
    if msg.guild is None or msg.author.bot:
        return False
    if msg.channel.id in getattr(config, "EXCLUDED_CHANNEL_IDS", []):
        return False
    if not _channel_allowed(msg.channel):          # 카테고리 화이트리스트 (catchup/backfill/edit 공통 관문)
        return False

    ctx = list(ctx_buf[msg.channel.id])
    if live:                                   # edit/catchup은 컨텍스트 오염 방지
        ctx_buf[msg.channel.id].append(msg.content or "")
    mentions = [m.display_name for m in msg.mentions if not m.bot]

    try:
        res = await asyncio.to_thread(
            brain.analyze, msg.content, msg.channel.name, msg.author.display_name, mentions, ctx)
    except Exception:
        log.exception("analyze 실패")
        await alert_err("ollama", "⚠️ 추출 실패 — Ollama(로컬 AI)가 응답하지 않는 것 같아요. 켜져 있는지 확인해 주세요.", threshold=2)
        return False
    clear_err("ollama")    # 추출 성공 → 연속실패 카운터 리셋

    link = jump(msg.guild.id, msg.channel.id, msg.id)

    # 결과 없음
    if not res:
        if is_edit:
            await _clear_schedules(msg)        # edit로 일정 사라짐 → 행+추적 정리
        return False

    tab = res["tab"]

    # 탭 전이: edit로 일정→비일정 되면 이전 일정 행 정리
    if is_edit and tab != "일정":
        await _clear_schedules(msg, untrack=True)

    # 일정 (다중 0..N)
    if tab == "일정":
        items = res.get("items", [])
        if not items:
            if is_edit:
                await _clear_schedules(msg)
            return False
        if _is_snapshot(msg.id, msg.content, items):
            await _apply_snapshot(msg, items, link)
            return True                      # 이모지 미부착 (매일 리스트 스팸 방지 — 의도된 변화)
        async with _chan_locks[msg.channel.id]:
            fut = items
            if not is_edit:                      # 신규 메시지에만 중복 가드(수정은 자기 행 reconcile 유지)
                # _canon_dup_foreign는 2단 매칭에서 LLM을 부를 수 있다(수 초) →
                #   반드시 스레드로. 이벤트 루프에서 동기 호출하면 그동안 봇 전체가
                #   멈춰 게이트웨이 하트비트까지 밀린다.
                fut = await asyncio.to_thread(
                    lambda: [it for it in items
                             if not _canon_dup_foreign(msg.channel.id, msg.id, it)])
                if len(fut) < len(items):
                    log.info(f"일정 canon중복 {len(items) - len(fut)}건 skip(다른 메시지에 이미 있음)")
            if fut:
                await _reconcile_schedules(msg, fut, link)
                _reg_add_rows(msg, fut)      # 비스냅샷 편입 → 이후 스냅샷이 언급하면 matched로 입양
            items = fut
        if not items:
            return True                          # 전건 중복 → 시트 무변경(정상 종료)
        # 이모지: live + is_task + 미추적/미플래그일 때만 1세트
        #   config.PRIORITY가 비면 아예 건너뛴다 — 이모지를 소비하는 건 리마인더봇인데
        #   2026-07-31에 그 봇을 중단했다. 빈 dict로 두면 for문만 비는 게 아니라
        #   flagged 기록까지 막아야 쓸모없는 상태가 안 쌓인다.
        if live and config.PRIORITY and res.get("is_task") and msg.id not in flagged:
            try:
                for e in config.PRIORITY:
                    await msg.add_reaction(e)
                flagged.add(msg.id)
                state.add_flagged(msg.id)
            except Exception:
                log.exception("이모지 부착 실패")
        log.info(f"-> 일정 {len(items)}건: {items[0].get('작업내용','')[:24]}")
        return True

    # 정산/확인필요 (단건) — key #0 통일
    fields = dict(res["fields"])
    fields["링크"] = link
    r = await asyncio.to_thread(sheet.push, tab, [{"key": f"{msg.id}#0", "fields": fields}])
    if isinstance(r, str) and r.startswith("err"):
        await alert_err("push", f"⚠️ 시트 전송 실패: {r[:100]}")
    log.info(f"-> {tab} ({r}): {fields.get('항목') or (fields.get('내용','') or '')[:24]}")
    return True


async def _reconcile_schedules(msg, items, link):
    """일정 #0..#(n-1) 업서트(전체덮기) + 직전 초과분 삭제. state에 건수 저장."""
    n = len(items)
    push_items = []
    for i, f in enumerate(items):
        f = dict(f)
        f["링크"] = link
        push_items.append({"key": f"{msg.id}#{i}", "fields": f})

    r = await asyncio.to_thread(sheet.push, "일정", push_items, "upsert")
    if isinstance(r, str) and r.startswith("err"):
        await alert_err("push", f"⚠️ 시트 전송 실패: {r[:100]}")
        return

    prev = state.get_msg_item_count(msg.id)
    if prev > n:                                   # 줄었으면 초과행 삭제
        over = [f"{msg.id}#{i}" for i in range(n, prev)]
        await asyncio.to_thread(sheet.delete_rows, "일정", over)
        state.reg_delete(msg.channel.id, over)     # 레지스트리 동기
    state.set_msg_item_count(msg.id, n)


async def _clear_schedules(msg, untrack=False):
    """이 메시지의 모든 일정 행 삭제 + 건수 0 + (옵션) 추적 해제·이모지 정리."""
    if state.is_snap_msg(msg.id):                  # 스냅샷 철회 (edit 0건/비일정 전이)
        async with _chan_locks[msg.channel.id]:
            keys = await asyncio.to_thread(state.reg_revoke_snapshot, msg.channel.id, msg.id)
            if keys:
                await asyncio.to_thread(sheet.delete_rows, "일정", keys)
            state.set_msg_item_count(msg.id, 0)
        return
    prev = state.get_msg_item_count(msg.id)
    if prev > 0:
        keys = [f"{msg.id}#{i}" for i in range(prev)]
        await asyncio.to_thread(sheet.delete_rows, "일정", keys)
        state.reg_delete(msg.channel.id, keys)     # 레지스트리 동기
    state.set_msg_item_count(msg.id, 0)
    if untrack or msg.id in flagged:
        flagged.discard(msg.id)
        state.del_flagged(msg.id)
        try:
            for e in config.PRIORITY:
                await msg.remove_reaction(e, client.user)   # 자기 것만 제거(Manage 권한 불필요·사람 reaction 보존)
        except Exception:
            pass


def _reg_add_rows(msg, items):
    """일반 메시지가 만든 행을 레지스트리에 편입(managed=0). 이후 스냅샷이 언급하면 matched로 입양.
    동일 canon 활성 행(다른 메시지 소유)이 이미 있으면 편입 생략 — 쌍둥이로 1단 유일성 파괴 방지."""
    for i, it in enumerate(items):
        cn = brain.canon(it.get("작업내용", ""))
        if cn:
            hits = state.reg_find_by_canon_all(msg.channel.id, cn)
            if any(not h["row_key"].startswith(f"{msg.id}#") for h in hits):
                log.info(f"reg 편입 생략(canon 중복): {it.get('작업내용','')[:30]}")
                continue
        state.reg_enroll(msg.channel.id, f"{msg.id}#{i}", cn, it.get("작업내용", ""),
                         it.get("날짜", "") or "", it.get("상태", "진행중"), managed=0)


async def _apply_snapshot(msg, items, link):
    """스냅샷(전체일정) diff 본체 — 채널 락 안에서 매칭→재활성→HW키→트랜잭션→push."""
    ch_id = msg.channel.id
    async with _chan_locks[ch_id]:
        await asyncio.to_thread(state.snap_msg_add, msg.id, ch_id, "snap")   # 선등록 (답장 누수 차단)
        items = brain.merge_snapshot_items(items)
        cands = await asyncio.to_thread(state.reg_channel_items, ch_id)      # 완료 포함 (부활 지원)
        try:
            idx = await asyncio.to_thread(
                brain.match_snapshot,
                [{"canon": c["canon"], "task": c["task_text"]} for c in cands],
                [it["작업내용"] for it in items])
        except Exception:
            log.exception("스냅샷 매칭 실패")
            await alert_err("ollama", "⚠️ 스냅샷 매칭 실패 — Ollama 확인 필요", threshold=2)
            return          # diff 전체 중단 (부분 반영 금지)
        clear_err("ollama")

        matched, new_raw = [], []
        for j, it in enumerate(items):
            i = idx[j]
            if i is None:
                if brain._is_progline(it):
                    log.info(f"스냅샷 진행줄 미매칭 → 행 미생성: {it['작업내용'][:30]}")
                    continue                                   # F: 새 행 금지
                new_raw.append(it)
                continue
            c = cands[i]
            st = it.get("상태") or "진행중"
            if c["status"] == "완료" and st != "완료":         # 재활성(부활) 정책
                revive = (c["completed_by"].startswith("snap:")
                          or bool(it.get("진행내용"))
                          or bool(it.get("날짜") and it["날짜"] != c["date_text"]))
                st = "진행중" if revive else "완료"
            matched.append({
                "row_key": c["row_key"], "task_text": it["작업내용"],
                "date_text": it.get("날짜") or c["date_text"], "status": st,
                "_prev_status": c["status"], "_prog": it.get("진행내용", ""),
                "_date_new": it.get("날짜") or "", "_owner": it.get("담당자", "")})

        hw = _snap_hw(msg.id)
        new_items = []
        for k, it in enumerate(new_raw):
            new_items.append({
                "row_key": f"{msg.id}#{hw + k}", "canon": brain.canon(it["작업내용"]),
                "task_text": it["작업내용"], "date_text": it.get("날짜") or "",
                "status": it.get("상태") or "진행중", "_item": it})

        # 빠짐(missing) 자동완료 기능은 사용 안 함 — 리스트에서 빠져도 행 유지 (사용자 결정 2026-07-03)
        #   (state 쪽 카운트 기계는 남아있음 — 다시 켜려면 이 값만 가드 계산으로 되돌리면 됨)
        allow_missing = False

        try:
            res = await asyncio.to_thread(
                state.reg_apply_snapshot, ch_id, msg.id, _kst_date(msg),
                [{k2: m[k2] for k2 in ("row_key", "task_text", "date_text", "status")} for m in matched],
                [{k2: n[k2] for k2 in ("row_key", "canon", "task_text", "date_text", "status")} for n in new_items],
                allow_missing)
        except Exception:
            log.exception("reg_apply_snapshot 실패")
            return
        if res["mode"] == "stale":
            log.info(f"스냅샷 stale skip: {msg.id}")
            return

        push = []
        for m in matched:                    # 필드 제한: 상태는 전이 시에만 (PM 수동 상태 보존)
            f = {"작업내용": m["task_text"], "담당자": m["_owner"], "링크": link}
            if m["_date_new"]:
                f["날짜"] = m["_date_new"]
            if m["_prog"]:
                f["진행내용"] = m["_prog"]
            if m["status"] != m["_prev_status"]:
                f["상태"] = m["status"]
            push.append({"key": m["row_key"], "fields": f})
        for n in new_items:
            f = dict(n["_item"]); f["링크"] = link
            push.append({"key": n["row_key"], "fields": f})
        if push:
            r = await asyncio.to_thread(sheet.push, "일정", push, "upsert")
            if isinstance(r, str) and r.startswith("err"):
                await alert_err("push", f"⚠️ 시트 전송 실패: {r[:100]}")
                # 레지스트리는 커밋됨 → 다음 스냅샷 upsert가 자가치유
        state.set_msg_item_count(msg.id, hw + len(new_items))   # = 새 high-water mark

        await _drain_outbox()                # deleted_new delete 재시도
        log.info(f"-> 스냅샷 {res['mode']}: 매칭 {len(matched)} / 신규 {len(new_items)} / "
                 f"정리 {len(res['deleted_new'])}")


@client.event
async def on_message(msg):
    if msg.author.bot or msg.guild is None:
        return
    if not _channel_allowed(msg.channel):     # 답장 갱신 경로까지 함께 차단 (process 진입 전)
        return

    ref = msg.reference.message_id if msg.reference else None

    # 답장으로 일정 진행 갱신 (완료 처리/추적 해소는 리마인더봇 담당이라 done 미사용)
    consumed = False
    if ref:
        consumed, _ = await _apply_reply_update(msg, ref)

    if not consumed:
        await process(msg, live=True)

    state.set_kv("last_seen", time.time())


async def _apply_reply_update(reply_msg, ref_id):
    """답장→원본 일정 진행 갱신. 반환 (consumed, done)."""
    # 스냅샷(또는 자동완료 알림) 메시지에 대한 답장 → 레지스트리 라우팅 선점
    snap_ch = state.snap_msg_channel(ref_id)
    if snap_ch is not None:
        if brain.SNAP_HINT_RE.search(reply_msg.content or ""):
            return (False, False)            # 답장 본문이 새 스냅샷 → process가 diff
        return await _route_snapshot_reply(reply_msg, ref_id, snap_ch)

    # 원본 일정 건수 N은 재추출이 아니라 state 기준 (비결정성 회피)
    n = state.get_msg_item_count(ref_id)
    if n == 0:
        return (False, False)   # 원본이 일정 아님 → 일반 처리에 맡김 (플러딩·불필요 AI 호출 방지)

    # 원본이 일정일 때만 진행 문구 경량 추출
    try:
        upd = await asyncio.to_thread(brain.extract_progress, reply_msg.content)
    except Exception:
        await alert_err("ollama", "⚠️ 진행 추출 실패 — Ollama 확인 필요", threshold=2)
        return (False, False)
    clear_err("ollama")
    if not upd or not upd.get("진행내용"):
        return (False, False)   # 단순 답장(ㅇㅋ 등) → 미소비

    prog = upd["진행내용"]
    done = brain.detect_status(prog) == "완료"
    link = jump(reply_msg.guild.id, reply_msg.channel.id, reply_msg.id)

    if n == 1:
        fields = {"진행내용": prog, "상태": ("완료" if done else "진행중")}
        if upd.get("날짜"):
            fields["날짜"] = upd["날짜"]
        r = await asyncio.to_thread(sheet.push, "일정",
                                    [{"key": f"{ref_id}#0", "fields": fields}], "merge")
        if isinstance(r, str) and r.endswith(":0"):     # 원본행 부재(state-시트 불일치) → 회송 (버그B)
            await _review_reply(reply_msg, ref_id, "[수동] 답장 대상 일정 행 없음 — 시트 확인", link)
            return (True, done)
        state.reg_set_status(reply_msg.channel.id, [f"{ref_id}#0"],
                             fields["상태"], f"reply:{reply_msg.id}")   # 레지스트리 완료/부활 기록
        return (True, done)

    # n >= 2 (모호)
    if done:
        # 완료는 메시지 단위 → 전 인덱스 상태=완료 일괄
        items = [{"key": f"{ref_id}#{i}", "fields": {"상태": "완료"}} for i in range(n)]
        r = await asyncio.to_thread(sheet.push, "일정", items, "merge")
        if isinstance(r, str) and r.endswith(":0"):
            await alert_err("reply_merge", f"⚠️ 완료 답장 반영 실패(행 부재): {ref_id}")
        state.reg_set_status(reply_msg.channel.id, [f"{ref_id}#{i}" for i in range(n)],
                             "완료", f"reply:{reply_msg.id}")           # 레지스트리 완료 기록
        return (True, True)
    # 부분 진행 모호 → 확인필요(수동) 회송. 자동등록 금지 — PM이 시트 직접 갱신 (버그D)
    await _review_reply(reply_msg, ref_id, f"[수동] 답장 진행 모호(원본 {n}건) — 시트에서 직접 갱신", link)
    return (True, False)


async def _review_reply(reply_msg, ref_id, reason, link):
    fields = {
        "내용": (reply_msg.content or "")[:120],
        "추정 작업자": reply_msg.author.display_name,
        "추정 의뢰자": "",
        "사유": reason,
        "링크": link,
    }
    await asyncio.to_thread(sheet.push, "확인필요",
                            [{"key": f"{reply_msg.id}#0", "fields": fields}], "upsert")


async def _route_snapshot_reply(msg, ref_id, ch_id):
    """스냅샷/알림 메시지에 대한 답장 → 레지스트리 행으로 진행/완료 라우팅. 반환 (consumed, done)."""
    try:
        upd = await asyncio.to_thread(brain.extract_progress, msg.content)
    except Exception:
        await alert_err("ollama", "⚠️ 진행 추출 실패 — Ollama 확인 필요", threshold=2)
        return (False, False)
    clear_err("ollama")
    if not upd or not upd.get("진행내용"):
        return (False, False)                # ㅇㅋ류 → 미소비 (기존 관례)
    prog = upd["진행내용"]
    done = brain.detect_status(prog) == "완료"
    link = jump(msg.guild.id, msg.channel.id, msg.id)
    async with _chan_locks[ch_id]:
        cands = await asyncio.to_thread(state.reg_channel_items, ch_id)      # 완료 포함(부활 지원)
        if not cands:
            return (False, False)
        try:
            scope, i = await asyncio.to_thread(
                brain.route_reply, [{"task": c["task_text"]} for c in cands], msg.content)
        except Exception:
            await alert_err("ollama", "⚠️ 답장 라우팅 실패 — Ollama 확인 필요", threshold=2)
            return (False, False)
        if scope == "one":
            c = cands[i]
            st = "완료" if done else "진행중"
            fields = {"진행내용": prog, "상태": st}
            if upd.get("날짜"):
                fields["날짜"] = upd["날짜"]
            r = await asyncio.to_thread(sheet.push, "일정",
                                        [{"key": c["row_key"], "fields": fields}], "merge")
            if isinstance(r, str) and r.endswith(":0"):
                await _review_reply(msg, ref_id, "[수동] 답장 대상 일정 행 없음 — 시트 확인", link)
                return (True, done)
            await asyncio.to_thread(state.reg_set_status, ch_id, [c["row_key"]], st,
                                    f"reply:{msg.id}")     # 완료 기록/부활 (replay 보호 근거)
            return (True, done)
        if scope == "all" and done:
            tgt = [c for c in cands if c["status"] == "진행중"
                   and (c["last_seen_snap"] == ref_id or c["src_mid"] == ref_id)]
            if not tgt:
                tgt = [c for c in cands if c["status"] == "진행중" and c["managed"]]
            keys = [c["row_key"] for c in tgt]
            if keys:
                await asyncio.to_thread(sheet.push, "일정",
                        [{"key": k, "fields": {"상태": "완료"}} for k in keys], "merge")
                await asyncio.to_thread(state.reg_set_status, ch_id, keys, "완료",
                                        f"reply:{msg.id}")
            return (True, True)
        await _review_reply(msg, ref_id, "[수동] 스냅샷 답장 모호 — 시트에서 직접 갱신", link)
        return (True, False)


@client.event
async def on_raw_message_edit(payload):
    # 링크 임베드 자동 펼침 등은 edited_timestamp 없음 → 실제 사용자 수정만 처리
    if not (payload.data and payload.data.get("edited_timestamp")):
        return
    ch = client.get_channel(payload.channel_id)
    if ch is None or getattr(ch, "guild", None) is None:
        return
    try:
        msg = await ch.fetch_message(payload.message_id)
    except Exception:
        return
    if msg.author.bot:
        return
    if msg.channel.id in getattr(config, "EXCLUDED_CHANNEL_IDS", []):
        return

    # on_message과 동일 순서: 답장 먼저 → 소비되면 신규/재추출 skip (버그C: 답장수정 시 유령 일정 방지)
    ref = msg.reference.message_id if msg.reference else None
    consumed = False
    if ref:
        consumed, _ = await _apply_reply_update(msg, ref)
    if not consumed:
        await process(msg, live=False, is_edit=True)    # 이모지 재부착 안 함·컨텍스트 오염 없음
    state.set_kv("last_seen", time.time())


# ── 확인필요 처리 (시트 read-back): PM이 처리=등록/무시 → 봇이 반영 ──
_resolve_busy = False
RESOLVE_BATCH = 10   # 사이클당 등록 처리 상한 (ollama 호출이라 느림)


def _reextract_task(content):
    rows = brain.extract_deadline((content or "")[:400])
    if not rows:
        return None
    items = [{"날짜": brain.norm_date(r["기한"]), "작업내용": r["작업내용"],
              "진행내용": r["진행"],   # 항목별 "@@님" → 의뢰자 사전 폴백 (analyze와 대칭)
              "의뢰자": brain.nim_in(r["작업내용"]) or brain.client_from_task(r["작업내용"]),
              "상태": brain.detect_status(r["진행"])} for r in rows]
    return ("일정", items)     # items 리스트 반환


def _reextract_settle(content):
    st = brain.extract_settle((content or "")[:400])
    if st is None or not st["항목"]:
        return None
    return ("정산", st)


@tasks.loop(seconds=180)
async def resolve_loop():
    global _resolve_busy
    if _resolve_busy:
        return
    _resolve_busy = True
    try:
        await _drain_outbox()             # 자동완료/정리 push 재시도 (주기 드레인)
        rows = await asyncio.to_thread(sheet.fetch, "확인필요")
        if isinstance(rows, dict):        # {'err':..} → 사이클 전체 skip
            await alert_err("confirm_fetch", f"⚠️ 확인필요 읽기 실패: {str(rows.get('err',''))[:100]}", threshold=2)
            return
        clear_err("confirm_fetch")
        delete_keys, done = [], 0
        for row in rows:
            key = row.get("key")
            action = (row.get("처리") or "").strip()
            if not key or action not in ("등록", "무시"):
                continue
            if action == "무시":
                delete_keys.append(key)
                continue
            reason0 = row.get("사유") or ""
            if "[수동]" in reason0:        # 수동 갱신 대상(답장 모호 등) → 자동등록 금지, 등록 눌러도 행만 정리
                delete_keys.append(key)
                continue
            if done >= RESOLVE_BATCH:
                break
            worker = (row.get("추정작업자") or "").strip()
            if not worker:
                continue
            content = row.get("내용") or ""
            reason = row.get("사유") or ""
            link = row.get("원본링크") or ""
            try:
                ext = await asyncio.to_thread(
                    _reextract_settle if reason.startswith("정산") else _reextract_task, content)
            except Exception:
                await alert_err("ollama", "⚠️ 재추출 실패 — Ollama 확인 필요", threshold=2)
                continue
            if not ext:
                continue
            tab, payload = ext
            base = str(key).split("#")[0]          # 확인필요 key → base msg.id
            if tab == "일정":
                ch_id = _channel_from_link(link)
                client = (row.get("추정의뢰자") or "").strip()   # ⑫ 회송 경로도 의뢰자 보존(직접 경로와 대칭)
                push_items, merged = [], 0
                for f in payload:
                    f = dict(f); f["담당자"] = worker; f["링크"] = link
                    if client and not f.get("의뢰자"):           # 항목별 @@님 없을 때만 확인필요의 추정의뢰자로 폴백
                        f["의뢰자"] = client
                    cn = brain.canon(f.get("작업내용", ""))
                    hits = state.reg_find_by_canon_all(ch_id, cn) if (ch_id and cn) else []
                    if len(hits) == 1:             # 기존 행과 canon 유일 일치 → 병합 (새 행 금지)
                        r1 = await asyncio.to_thread(sheet.push, "일정",
                                [{"key": hits[0]["row_key"], "fields": f}], "upsert")
                        if isinstance(r1, str) and r1.startswith("ok"):
                            merged += 1
                            continue
                    push_items.append({"key": None, "fields": f})
                for i, it in enumerate(push_items):
                    it["key"] = f"{base}#{i}"
                r = await asyncio.to_thread(sheet.push, "일정", push_items, "upsert") if push_items else "ok:0"
                ok = (not push_items) or (isinstance(r, str) and r.startswith("ok") and not r.endswith(":0"))
                if ok:
                    if base.isdigit() and push_items:
                        state.set_msg_item_count(int(base), len(push_items))
                    if ch_id:
                        for it in push_items:      # 신규분 편입 → 이후 스냅샷과 매칭
                            state.reg_enroll(ch_id, it["key"], brain.canon(it["fields"]["작업내용"]),
                                             it["fields"]["작업내용"], it["fields"].get("날짜", "") or "",
                                             it["fields"].get("상태", "진행중"), managed=0)
                    elif push_items:
                        log.warning(f"resolve: 링크 파싱 실패 → 레지스트리 미편입 {base}")
                    delete_keys.append(key)        # push 성공 후에만 삭제
                    log.info(f"resolve(등록->일정 {len(push_items)}건/병합 {merged}): {base} / {worker}")
                else:
                    await alert_err("confirm_push", f"⚠️ 등록 실패(삭제 보류): {str(r)[:100]}")
            else:   # 정산
                fields = dict(payload); fields["받는이"] = worker; fields["링크"] = link
                r = await asyncio.to_thread(sheet.push, "정산", [{"key": f"{base}#0", "fields": fields}], "upsert")
                if isinstance(r, str) and r.startswith("ok") and not r.endswith(":0"):
                    delete_keys.append(key)
                    log.info(f"resolve(등록->정산): {base} / {worker}")
                else:
                    await alert_err("confirm_push", f"⚠️ 등록 실패(삭제 보류): {str(r)[:100]}")
            done += 1
        if delete_keys:
            await asyncio.to_thread(sheet.delete_rows, "확인필요", delete_keys)
    except Exception:
        log.exception("resolve_loop 실패")
    finally:
        _resolve_busy = False


@resolve_loop.before_loop
async def _before_resolve():
    await client.wait_until_ready()


@tree.command(name="상태", description="봇 상태")
async def status_cmd(inter: discord.Interaction):
    await inter.response.send_message(
        f"🟢 추출봇 가동 중 | 서버 {len(client.guilds)} | 플래그 {len(flagged)}건", ephemeral=True)


def _is_past(date_str, today):
    """날짜가 today보다 이전이면 True. 범위("A ~ B")는 끝 날짜 기준. 없음/모호는 False."""
    s = (date_str or "")
    if "~" in s:
        s = s.split("~")[-1].strip()         # "2026-06-28 ~ 2026-07-15" → 끝 날짜로 판정
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return False
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date() < today
    except ValueError:
        return False


async def _category_ac(inter: discord.Interaction, current: str):
    """카테고리 자동완성 — 입력값 부분일치, 최대 25개."""
    cats = inter.guild.categories if inter.guild else []
    cur = (current or "").lower()
    return [app_commands.Choice(name=c.name, value=c.name)
            for c in cats if cur in c.name.lower()][:25]


@tree.command(name="과거등록",
              description="카테고리 내 모든 채널의 지난 메시지에서 일정 스캔→등록 (지난날짜·중복 제외)")
@app_commands.describe(category="스캔할 카테고리 (입력하면 목록이 떠요)", days="며칠 전까지 (기본 30, 최대 365)",
                       force="이미 등록된 것도 다시 등록 (시트 초기화 후 재등록용)")
@app_commands.autocomplete(category=_category_ac)
async def backfill_cmd(inter: discord.Interaction, category: str = None, days: int = 30, force: bool = False):
    await inter.response.defer(ephemeral=True)
    g = inter.guild
    if g is None:
        await inter.followup.send("서버에서만 쓸 수 있어요.", ephemeral=True)
        return

    # 대상 채널: 지정 카테고리 > 현재 채널의 카테고리 > 현재 채널 1개
    cur_cat = getattr(inter.channel, "category", None)
    if category:
        cat = discord.utils.get(g.categories, name=category)
        if cat is None:
            names = ", ".join(c.name for c in g.categories) or "(없음)"
            await inter.followup.send(f"'{category}' 카테고리를 못 찾았어요.\n있는 카테고리: {names}", ephemeral=True)
            return
        channels, where = list(cat.text_channels), cat.name
    elif cur_cat is not None:
        channels, where = list(cur_cat.text_channels), cur_cat.name
    elif inter.channel is not None:
        channels, where = [inter.channel], inter.channel.name
    else:
        await inter.followup.send("대상 채널을 찾지 못했어요.", ephemeral=True)
        return

    # force=재구축: 직전 실행이 '상한 도달'로 끊긴 게 아니면(=완주했으면) 완료마커 초기화 → 전 채널 재스캔.
    #   (clearSchedule는 시트만 비우고 이 마커는 state.db에 남아, 안 지우면 force가 전채널 skip=등록0)
    if force and state.get_kv("bf_last_capped") != "1":
        for ch in channels:
            state.set_kv(f"bf_done:{ch.id}", 0)

    days = max(1, min(days, 365))
    after = datetime.now(timezone.utc) - timedelta(days=days)
    today = datetime.now().date()
    added = past = dup = calls = scanned_ch = 0
    capped = False

    for ch in channels:
        try:
            if not ch.permissions_for(g.me).read_message_history:
                continue
        except Exception:
            continue
        if force:
            done_at = float(state.get_kv(f"bf_done:{ch.id}", 0) or 0)
            if time.time() - done_at < 21600:          # 6h 내 완료 채널 → 통째 skip (이어하기)
                continue
            state.reg_reset_channel(ch.id)             # 리셋은 채널 재생 직전, 채널 단위
        scanned_ch += 1
        ch_capped = False
        try:
            async for msg in ch.history(after=after, limit=500, oldest_first=True):
                if msg.author.bot:
                    continue
                if not force and (state.get_msg_item_count(msg.id) > 0 or state.is_snap_msg(msg.id)):
                    dup += 1                                     # 이미 등록/처리된 스냅샷 → skip
                    continue
                if not force and brain.SNAP_HINT_RE.search(msg.content or "") \
                        and msg.id < state.reg_meta(ch.id)[0]:
                    dup += 1                                     # stale 스냅샷 cheap-skip (LLM 절약)
                    continue
                if calls >= 150:                                 # 분석 상한 (응답시간 보호)
                    capped = ch_capped = True
                    break
                calls += 1
                mentions = [m.display_name for m in msg.mentions if not m.bot]
                try:
                    res = await asyncio.to_thread(
                        brain.analyze, msg.content, ch.name, msg.author.display_name, mentions, [])
                except Exception:
                    continue
                if not res or res.get("tab") != "일정":
                    continue
                items = res.get("items", [])
                if _is_snapshot(msg.id, msg.content, items):
                    await _apply_snapshot(msg, items, jump(g.id, ch.id, msg.id))   # 과거날짜 필터 면제
                    added += len(items)
                    continue
                fut = [it for it in items if not _is_past(it.get("날짜"), today)]
                past += len(items) - len(fut)
                fut = await asyncio.to_thread(      # 2단 매칭은 LLM 호출 → 스레드로 (루프 블로킹 방지)
                    lambda: [it for it in fut if not _canon_dup_foreign(ch.id, msg.id, it)])
                if not fut:
                    continue
                async with _chan_locks[ch.id]:
                    await _reconcile_schedules(msg, fut, jump(g.id, ch.id, msg.id))   # 등록(이모지 X)
                    _reg_add_rows(msg, fut)
                added += len(fut)
        except Exception:
            log.exception(f"과거등록 실패: {ch.name}")
        if force and not ch_capped:
            state.set_kv(f"bf_done:{ch.id}", time.time())        # 끝까지 재생한 채널만 완료 마커
        if capped:
            break

    if force:                                       # 상한으로 끊겼는지 기록 → 다음 force가 이어하기/재구축 판단
        state.set_kv("bf_last_capped", "1" if capped else "0")

    tail = " (상한 도달 — 같은 명령(force 포함)으로 재실행하면 완료된 채널은 건너뛰고 이어서)" if capped else ""
    await inter.followup.send(
        f"📥 과거등록 [{where}] — 채널 {scanned_ch}개 / 추가 {added}건 / "
        f"지난날짜 제외 {past} / 이미등록 {dup}{tail}", ephemeral=True)


if __name__ == "__main__":
    client.run(config.TOKEN)
