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


def jump(g, c, m):
    return f"https://discord.com/channels/{g}/{c}/{m}"


async def alert(text):
    try:
        u = client.get_user(config.OWNER_ID) or await client.fetch_user(config.OWNER_ID)
        if u:
            await u.send(text)
    except Exception:
        log.exception("alert(DM) 실패")


async def alert_err(key, text):
    now = time.time()
    if now - _last_alert.get(key, 0) < 600:   # 같은 종류 알림은 10분에 1번
        return
    _last_alert[key] = now
    await alert(text)


@client.event
async def on_ready():
    global flagged
    try:
        await tree.sync()
    except Exception:
        log.exception("명령어 sync 실패")
    flagged = state.load_flagged()
    log.info(f"login: {client.user} | servers {len(client.guilds)} | 플래그 {len(flagged)}")
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
        state.set_kv("last_seen", time.time())
        return 0
    after = datetime.fromtimestamp(float(last), tz=timezone.utc)
    count = 0
    for guild in client.guilds:
        for ch in getattr(guild, "text_channels", []):
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

    ctx = list(ctx_buf[msg.channel.id])
    if live:                                   # edit/catchup은 컨텍스트 오염 방지
        ctx_buf[msg.channel.id].append(msg.content or "")
    mentions = [m.display_name for m in msg.mentions if not m.bot]

    try:
        res = await asyncio.to_thread(
            brain.analyze, msg.content, msg.channel.name, msg.author.display_name, mentions, ctx)
    except Exception:
        log.exception("analyze 실패")
        await alert_err("ollama", "⚠️ 추출 실패 — Ollama(로컬 AI)가 응답하지 않는 것 같아요. 켜져 있는지 확인해 주세요.")
        return False

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
        await _reconcile_schedules(msg, items, link)
        # 이모지: live + is_task + 미추적/미플래그일 때만 1세트
        if live and res.get("is_task") and msg.id not in flagged:
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
    state.set_msg_item_count(msg.id, n)


async def _clear_schedules(msg, untrack=False):
    """이 메시지의 모든 일정 행 삭제 + 건수 0 + (옵션) 추적 해제·이모지 정리."""
    prev = state.get_msg_item_count(msg.id)
    if prev > 0:
        keys = [f"{msg.id}#{i}" for i in range(prev)]
        await asyncio.to_thread(sheet.delete_rows, "일정", keys)
    state.set_msg_item_count(msg.id, 0)
    if untrack or msg.id in flagged:
        flagged.discard(msg.id)
        state.del_flagged(msg.id)
        try:
            for e in config.PRIORITY:
                await msg.remove_reaction(e, client.user)   # 자기 것만 제거(Manage 권한 불필요·사람 reaction 보존)
        except Exception:
            pass


@client.event
async def on_message(msg):
    if msg.author.bot or msg.guild is None:
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
    # 원본 일정 건수 N은 재추출이 아니라 state 기준 (비결정성 회피)
    n = state.get_msg_item_count(ref_id)
    if n == 0:
        return (False, False)   # 원본이 일정 아님 → 일반 처리에 맡김 (플러딩·불필요 AI 호출 방지)

    # 원본이 일정일 때만 진행 문구 경량 추출
    try:
        upd = await asyncio.to_thread(brain.extract_progress, reply_msg.content)
    except Exception:
        await alert_err("ollama", "⚠️ 진행 추출 실패 — Ollama 확인 필요")
        return (False, False)
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

    # n >= 2 (모호)
    if done:
        # 완료는 메시지 단위 → 전 인덱스 상태=완료 일괄
        items = [{"key": f"{ref_id}#{i}", "fields": {"상태": "완료"}} for i in range(n)]
        r = await asyncio.to_thread(sheet.push, "일정", items, "merge")
        if isinstance(r, str) and r.endswith(":0"):
            await alert_err("reply_merge", f"⚠️ 완료 답장 반영 실패(행 부재): {ref_id}")
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
    rows = brain.parse_deadline_lines(brain.ollama(brain.DEADLINE_PROMPT.format(m=(content or "")[:400])))
    if not rows:
        return None
    items = [{"날짜": brain.norm_date(r["기한"]), "작업내용": r["작업내용"],
              "진행내용": r["진행"], "상태": brain.detect_status(r["진행"])} for r in rows]
    return ("일정", items)     # items 리스트 반환


def _reextract_settle(content):
    out = brain.ollama(brain.SETTLE_PROMPT.format(m=(content or "")[:400]))
    if "|" not in out or out.startswith("없음"):
        return None
    p = [x.strip() for x in out.split("|")] + ["", "", ""]
    if not p[0] or p[0] in brain.LABELS:
        return None
    return ("정산", {"항목": p[0], "금액": brain.norm_amount(p[1]),
                     "상태": brain.norm_settle_status(p[2])})


@tasks.loop(seconds=180)
async def resolve_loop():
    global _resolve_busy
    if _resolve_busy:
        return
    _resolve_busy = True
    try:
        rows = await asyncio.to_thread(sheet.fetch, "확인필요")
        if isinstance(rows, dict):        # {'err':..} → 사이클 전체 skip
            await alert_err("confirm_fetch", f"⚠️ 확인필요 읽기 실패: {str(rows.get('err',''))[:100]}")
            return
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
                await alert_err("ollama", "⚠️ 재추출 실패 — Ollama 확인 필요")
                continue
            if not ext:
                continue
            tab, payload = ext
            base = str(key).split("#")[0]          # 확인필요 key → base msg.id
            if tab == "일정":
                push_items = []
                for i, f in enumerate(payload):
                    f = dict(f); f["담당자"] = worker; f["링크"] = link
                    push_items.append({"key": f"{base}#{i}", "fields": f})
                r = await asyncio.to_thread(sheet.push, "일정", push_items, "upsert")
                if isinstance(r, str) and r.startswith("ok") and not r.endswith(":0"):
                    if base.isdigit():
                        state.set_msg_item_count(int(base), len(push_items))
                    delete_keys.append(key)        # push 성공 후에만 삭제
                    log.info(f"resolve(등록->일정 {len(push_items)}건): {base} / {worker}")
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
    """YYYY-MM-DD가 today보다 이전이면 True. 날짜 없음/모호는 False(지난 것 아님)."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_str or "")
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
        scanned_ch += 1
        try:
            async for msg in ch.history(after=after, limit=500, oldest_first=True):
                if msg.author.bot:
                    continue
                if not force and state.get_msg_item_count(msg.id) > 0:   # 이미 등록 → 중복 skip
                    dup += 1
                    continue
                if calls >= 150:                                 # 분석 상한 (응답시간 보호)
                    capped = True
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
                fut = [it for it in items if not _is_past(it.get("날짜"), today)]
                past += len(items) - len(fut)
                if not fut:
                    continue
                await _reconcile_schedules(msg, fut, jump(g.id, ch.id, msg.id))   # 등록(이모지 X)
                added += len(fut)
        except Exception:
            log.exception(f"과거등록 실패: {ch.name}")
        if capped:
            break

    tail = " (상한 도달 — 다시 실행하면 이어서)" if capped else ""
    await inter.followup.send(
        f"📥 과거등록 [{where}] — 채널 {scanned_ch}개 / 추가 {added}건 / "
        f"지난날짜 제외 {past} / 이미등록 {dup}{tail}", ephemeral=True)


if __name__ == "__main__":
    client.run(config.TOKEN)
