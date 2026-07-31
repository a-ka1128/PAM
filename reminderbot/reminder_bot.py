# reminderbot/reminder_bot.py — 룩플 리마인더봇 (always-on / 본문 안 읽음)
#   추출봇이 메시지에 붙인 우선도 이모지(🔴🟡🔵)를 사람이 클릭 → 추적 시작
#   간격 지나면 재촉 / ✅ → 해소 / 답장 → 타이머 리셋. 본문은 절대 읽지 않음(reference 메타만).
import os
import sys
import re
import time
import socket
import asyncio
import logging
import unicodedata
from datetime import datetime, date, timezone, timedelta
import discord
from discord import app_commands
from discord.ext import tasks

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config_reminder as config
import state_reminder as state
import sheet_reader as sheet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
    handlers=[logging.FileHandler(os.path.join(HERE, "reminder.log"), encoding="utf-8", delay=True),
              logging.StreamHandler()])
log = logging.getLogger("reminder")

# 단일 인스턴스 락 (추출봇 47291과 다른 포트)
try:
    _LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _LOCK.bind(("127.0.0.1", 47292))
    _LOCK.listen(1)
except OSError:
    log.warning("이미 다른 리마인더봇 인스턴스 실행 중 → 종료")
    sys.exit(0)

intents = discord.Intents.none()
intents.guilds = True             # get_channel / 길드 캐시 (없으면 전면 침묵 실패)
intents.guild_messages = True     # on_message 수신(메타: reference, author)
intents.guild_reactions = True    # on_raw_reaction_add 수신
intents.message_content = False   # ★ 본문 OFF = 프라이버시
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

tracked = {}   # mid -> {channel_id, interval, last, author_id, emoji}


async def alert(text):
    try:
        u = client.get_user(config.OWNER_ID) or await client.fetch_user(config.OWNER_ID)
        if u:
            await u.send(text)
    except Exception:
        log.exception("alert(DM) 실패")


def _norm_emoji(e):
    """variation selector(U+FE0F) 제거 + NFC 정규화 → Discord 라운드트립 비교 안정화 (C4)."""
    return unicodedata.normalize("NFC", str(e)).replace("️", "")


_PRIORITY_NORM = {_norm_emoji(k): v for k, v in config.PRIORITY.items()}
_DONE_NORM = _norm_emoji(config.DONE_EMOJI)


async def _extractor_flagged(message):
    """정식 플래그 식별: 이 메시지에 추출봇 계정이 PRIORITY 이모지 중 '하나라도' 달았는지.
       추출봇 add_reaction 부분실패(rate limit) 대비 '하나라도'로 완화.
       추출봇이 꺼져 있어도 reaction은 메시지에 남아있어 본문 없이 검증 가능."""
    want = set(_PRIORITY_NORM.keys())
    for r in message.reactions:
        if _norm_emoji(r.emoji) not in want:
            continue
        try:
            async for user in r.users():
                if user.id == config.EXTRACTOR_BOT_USER_ID:
                    return True
        except Exception:
            log.exception("reaction.users() 조회 실패")
    return False


@client.event
async def on_ready():
    global tracked
    try:
        await tree.sync()
    except Exception:
        log.exception("명령어 sync 실패")
    tracked = state.load_tracked()
    log.info(f"login: {client.user} (id={client.user.id}) | "
             f"servers {len(client.guilds)} | 추적복원 {len(tracked)}")
    # 설정 함정 방어: EXTRACTOR_BOT_USER_ID를 자기 id로 잘못 넣으면 전면 미동작
    if config.EXTRACTOR_BOT_USER_ID == client.user.id:
        await alert("🚨 설정 오류: EXTRACTOR_BOT_USER_ID가 리마인더봇 자신의 ID로 설정됨 "
                    "→ 모든 추적이 무시됩니다. 추출봇의 User ID로 고치세요.")
        log.error("EXTRACTOR_BOT_USER_ID == self.id (전면 미동작)")
    if not reminder_tick.is_running():
        reminder_tick.start()
    if not digest_loop.is_running():
        digest_loop.start()
    await alert(f"✅ 리마인더봇 시작 — 추적 {len(tracked)}건 복원"
                + (" · 다이제스트 ON" if config.WEBHOOK_URL else ""))


@client.event
async def on_raw_reaction_add(payload):
    # (C1) 봇이 단 reaction 전면 차단: 추출봇의 🔴🟡🔵 부착이 추적을 자동 트리거하지 않도록.
    if payload.member and payload.member.bot:
        return
    if payload.user_id == config.EXTRACTOR_BOT_USER_ID:
        return
    if client.user and payload.user_id == client.user.id:
        return

    ch = client.get_channel(payload.channel_id)
    if ch is None or getattr(ch, "guild", None) is None:
        return
    emoji = _norm_emoji(payload.emoji)

    # ✅(DONE) → 추적 해소
    if emoji == _DONE_NORM:
        if payload.message_id in tracked:
            await resolve(payload.message_id, ch, "done-react")
        return

    # 우선도 이모지(🔴🟡🔵) 사람 클릭 → 추적 시작
    if emoji not in _PRIORITY_NORM:
        return
    if payload.message_id in tracked:            # 중복 추적 가드
        return

    try:
        m = await ch.fetch_message(payload.message_id)   # 본문 미사용, 메타/reaction만
    except Exception:
        return

    # 정식 플래그 검증: 추출봇이 우선도 이모지를 단 메시지일 때만
    if not await _extractor_flagged(m):
        log.info(f"무시: 추출봇 플래그 아님 (mid={payload.message_id}, {emoji})")
        return

    t = {"channel_id": payload.channel_id,
         "interval": _PRIORITY_NORM[emoji] * 3600,
         "last": time.time(),
         "author_id": m.author.id,            # author는 message_content와 무관하게 읽힘
         "emoji": emoji}
    tracked[payload.message_id] = t
    state.save_tracked(payload.message_id, t)

    # 선택된 것 외 나머지 우선도 이모지 제거 (클릭된 이모지는 '추적중' 표식으로 남김)
    for e in config.PRIORITY:
        if _norm_emoji(e) != emoji:
            try:
                await m.clear_reaction(e)    # 추출봇이 단 것 제거 → Manage Messages 권한 필요
            except Exception:
                pass                          # 권한 없으면 이모지만 잔존, 추적은 정상

    try:
        await m.reply(f"📌 추적 시작 {payload.emoji} — <@{m.author.id}> 이 작업 챙겨주세요!\n"
                      f"완료되면 **{config.DONE_EMOJI} 를 눌러주세요.** (답장은 '아직 진행 중' 신호로 봅니다)")
    except Exception:
        pass
    log.info(f"추적시작 mid={payload.message_id} {emoji} author={m.author.id}")


@client.event
async def on_message(msg):
    # 본문 못 읽음: msg.content는 빈 문자열일 수 있음 → 절대 참조 안 함. reference 메타만 사용.
    if msg.author.bot or msg.guild is None:
        return
    ref = msg.reference.message_id if msg.reference else None
    if not ref or ref not in tracked:
        return
    # (C2) 본문을 못 읽어 완료/질문 구분 불가 → 답장은 '살아있다' 신호로만 보고 타이머 리셋.
    #      완전 해소는 ✅ 전용. (질문 답장 하나로 작업이 사라지는 회귀 방지)
    t = tracked[ref]
    t["last"] = time.time()
    state.save_tracked(ref, t)
    log.info(f"답장→타이머리셋 mid={ref}")


async def resolve(mid, ch, why):
    tracked.pop(mid, None)
    state.del_tracked(mid)
    try:
        m = await ch.fetch_message(mid)
        await m.add_reaction("☑️")
        await m.reply("✅ 완료 처리됐어요. 수고하셨습니다!")
    except Exception:
        pass
    log.info(f"resolve({why}): {mid}")


@tasks.loop(seconds=config.TICK_SECONDS)
async def reminder_tick():
    now = time.time()
    for mid, t in list(tracked.items()):
        if now - t["last"] < t["interval"]:
            continue
        ch = client.get_channel(t["channel_id"])
        if ch is None:
            continue
        try:
            m = await ch.fetch_message(mid)
        except Exception:
            # 메시지 삭제 등 → 추적 정리
            tracked.pop(mid, None)
            state.del_tracked(mid)
            continue
        # 추출봇 플래그 reaction이 사라졌으면(일정이 edit로 소멸 등) 유령추적 자동 정리
        if not await _extractor_flagged(m):
            log.info(f"유령추적 정리(플래그 reaction 소멸): {mid}")
            tracked.pop(mid, None)
            state.del_tracked(mid)
            continue
        try:
            await m.reply(f"⏰ 리마인드 {t['emoji']} — <@{t['author_id']}> 아직 안 끝났어요!\n"
                          f"완료 시 **{config.DONE_EMOJI}** 를 눌러주세요.")
            t["last"] = now
            state.save_tracked(mid, t)
        except Exception:
            pass


@reminder_tick.before_loop
async def _before_tick():
    await client.wait_until_ready()


# ── 아침 다이제스트(PM 브리핑 DM) ──────────────────────────────
KST = timezone(timedelta(hours=9))
_YMD = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
DIGEST_CAP = 12                       # 섹션당 나열 상한(초과분은 '…외 N건')


def _parse_end_date(s):
    """'2026-07-15' 또는 범위 '.. ~ 2026-08-15'의 끝날짜 → date. ??/중순 등 파싱불가 → None(마감/연체 제외)."""
    s = (s or "").strip()
    if not s:
        return None
    part = s.split("~")[-1].strip() if "~" in s else s     # 범위면 끝날짜 기준
    m = _YMD.search(part)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def build_digest():
    """일정 탭(파생 데이터)만 읽어 순수 파이썬 집계. 반환 (text, 완료행_key집합) | (None, None)=읽기실패."""
    rows = sheet.fetch("일정")
    if isinstance(rows, dict):                              # {'err':..}
        log.warning(f"다이제스트 시트 읽기 실패: {rows.get('err')}")
        return None, None

    today = datetime.now(KST).date()
    overdue, soon, completed_now = [], [], set()
    for row in rows:
        st = (row.get("상태") or "").strip()
        if st == "완료":
            key = str(row.get("key") or "")
            if key:
                completed_now.add(key)
            continue
        d = _parse_end_date(row.get("날짜"))
        if not d:
            continue                                        # 애매/범위끝미상 → 마감·연체에서 제외
        if d < today:
            overdue.append((d, row))
        elif (d - today).days <= 3:
            soon.append((d, row))
    overdue.sort(key=lambda x: x[0])
    soon.sort(key=lambda x: x[0])

    prev = state.get_done_snapshot()
    initialized = state.get_kv("digest_init") == "1"
    newly_done = len(completed_now - prev) if initialized else None     # 첫날은 기준선만

    md = lambda dt: f"{dt.month}/{dt.day}"

    def line(d, row, days_over=None):
        task = (row.get("작업내용") or "").strip() or "(내용 없음)"
        if len(task) > 300:                        # 과도한 붙여넣기 방어(가독성 + DM 페이로드 폭주 차단)
            task = task[:300] + "…"
        owner = (row.get("담당자") or "").strip()
        who = f" (담당 {owner})" if owner else ""
        link = (row.get("원본링크") or "").strip()
        tail = f"  {link}" if link else ""
        if days_over is not None:
            return f" • {task}{who} — 마감 {md(d)}, {days_over}일 지남{tail}"
        dd = (d - today).days
        dlabel = "오늘(D-0)" if dd == 0 else f"{md(d)}(D-{dd})"
        return f" • {task}{who} — {dlabel}{tail}"

    parts = [f"🌅 룩플 아침 브리핑 ({md(today)})"]
    if overdue:
        parts.append(f"\n🔴 연체 {len(overdue)}건")
        parts += [line(d, row, days_over=(today - d).days) for d, row in overdue[:DIGEST_CAP]]
        if len(overdue) > DIGEST_CAP:
            parts.append(f"   …외 {len(overdue) - DIGEST_CAP}건")
    if soon:
        parts.append(f"\n🟡 3일 내 마감 {len(soon)}건")
        parts += [line(d, row) for d, row in soon[:DIGEST_CAP]]
        if len(soon) > DIGEST_CAP:
            parts.append(f"   …외 {len(soon) - DIGEST_CAP}건")
    if not overdue and not soon:
        parts.append("\n(임박/연체 일정 없음 — 여유롭네요 👍)")
    parts.append("\n✅ 어제 완료: 집계 시작(내일부터 표시)" if newly_done is None
                 else f"\n✅ 어제 완료 {newly_done}건")
    parts += _unpaid_section(today)
    return "\n".join(parts), completed_now


DISCORD_EPOCH_MS = 1420070400000       # 2015-01-01 UTC — 스노우플레이크 기준시각


def _row_age_days(key, today):
    """행 key('<msg_id>#<n>')의 msg_id에서 작성 시각을 역산해 경과일 반환. 실패 시 None.
    정산 탭엔 날짜 컬럼이 없어(받는이/항목/금액/상태) 나이를 알 방법이 이것뿐이다."""
    mid = str(key or "").split("#")[0]
    if not mid.isdigit():
        return None
    try:
        ms = (int(mid) >> 22) + DISCORD_EPOCH_MS
        return (today - datetime.fromtimestamp(ms / 1000, KST).date()).days
    except Exception:
        return None


_AMT_RE = re.compile(r"(\d[\d,]*)(?:\.\d+)?\s*(억|만|천)?")
_AMT_UNIT = {"억": 100000000, "만": 10000, "천": 1000}


def _parse_amount(s):
    """시트 금액 → 정수(원) | None.
    ① doGet이 getDisplayValues()로 읽어 셀 표시형식이 그대로 온다("500,000") — 쉼표·통화기호를
       떼지 않으면 int()가 전부 실패해 합계가 엉뚱해진다(실측: 합계가 140원으로 나왔다).
    ② 봇이 넣는 값은 norm_amount로 정규화된 정수지만, 사람이 손으로 "20만원"처럼 적을 수 있어
       만/억/천 단위도 곱해준다(안 하면 200000이 20으로 읽혀 1만배 어긋난다)."""
    m = _AMT_RE.search((s or "").replace(" ", ""))
    if not m:
        return None
    try:
        n = int(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return n * _AMT_UNIT.get(m.group(2), 1)


def _unpaid_section(today):
    """미수금(정산 미완료) 나이순 — 오래된 것부터. 실패하면 조용히 생략(다이제스트 본체 보호)."""
    try:
        rows = sheet.fetch("정산")
    except Exception:
        return []
    if isinstance(rows, dict):
        log.warning(f"정산 탭 읽기 실패(미수금 섹션 생략): {rows.get('err')}")
        return []
    items = []
    total = 0
    for r in rows:
        if (r.get("상태") or "").strip() == "완료":
            continue
        # 받는이·항목·금액이 전부 빈 껍데기 행은 미정산이 아니다(시트에 잔재가 쌓인다)
        if not any((r.get(k) or "").strip() for k in ("받는이", "항목", "금액")):
            continue
        amt = _parse_amount(r.get("금액"))
        if amt:
            total += amt
        items.append((_row_age_days(r.get("key"), today), r))
    if not items:
        return []
    items.sort(key=lambda x: (-(x[0] if x[0] is not None else -1)))   # 오래된 순, 미상은 뒤로
    out = [f"\n💰 미정산 {len(items)}건" + (f" (합계 {total:,}원)" if total else "")]
    for age, r in items[:DIGEST_CAP]:
        who = (r.get("받는이") or "").strip()
        item = (r.get("항목") or "").strip() or "(항목 없음)"
        amt_n = _parse_amount(r.get("금액"))
        amt_s = f"{amt_n:,}원" if amt_n else "금액미상"
        age_s = f"{age}일 경과" if age is not None else "경과일 미상"
        out.append(f" • {who} {item} — {amt_s}, {age_s}")
    if len(items) > DIGEST_CAP:
        out.append(f"   …외 {len(items) - DIGEST_CAP}건")
    return out


async def _dm_long(user, text):
    """DM 2000자 한도 대비 줄 단위 분할 전송. 한 줄이 한도 초과면 줄 내부까지 강제 슬라이스(400 방지)."""
    LIMIT = 1900
    buf = ""
    for ln in text.split("\n"):
        while len(ln) > LIMIT:                     # 한 줄 자체가 한도 초과 → 강제 분할
            if buf:
                await user.send(buf); buf = ""
            await user.send(ln[:LIMIT]); ln = ln[LIMIT:]
        if buf and len(buf) + len(ln) + 1 > LIMIT:
            await user.send(buf); buf = ""
        buf = (buf + "\n" + ln) if buf else ln
    if buf:
        await user.send(buf)


@tasks.loop(minutes=5)
async def digest_loop():
    if not config.WEBHOOK_URL:
        return
    now = datetime.now(KST)
    if now.hour < config.DIGEST_HOUR:
        return
    today = now.strftime("%Y-%m-%d")
    if state.get_kv("last_digest_date") == today:          # 오늘 이미 발송
        return
    text, completed_now = await asyncio.to_thread(build_digest)
    if completed_now is None:                              # 시트 읽기 실패 → 다음 tick 재시도(날짜 미기록)
        return
    try:
        u = client.get_user(config.OWNER_ID) or await client.fetch_user(config.OWNER_ID)
        if not u:
            return
        await _dm_long(u, text)
    except Exception:
        log.exception("다이제스트 DM 실패")
        return                                             # 실패 시 스냅샷/날짜 미기록 → 재시도
    state.set_done_snapshot(completed_now)                 # 기준선 갱신(다음 diff)
    state.set_kv("digest_init", "1")
    state.set_kv("last_digest_date", today)
    log.info(f"다이제스트 발송 완료: {today} (연체/임박/완료 집계)")


@digest_loop.before_loop
async def _before_digest():
    await client.wait_until_ready()


@tree.command(name="추적목록", description="현재 재촉 중인 작업")
async def list_cmd(inter: discord.Interaction):
    if not tracked:
        await inter.response.send_message("재촉 중인 작업 없음.", ephemeral=True)
        return
    lines = [f"- https://discord.com/channels/{inter.guild_id}/{t['channel_id']}/{mid} ({t['emoji']})"
             for mid, t in tracked.items()]
    await inter.response.send_message("📋 재촉 중:\n" + "\n".join(lines), ephemeral=True)


@tree.command(name="추적해제", description="추적 해제 (메시지 ID)")
async def untrack_cmd(inter: discord.Interaction, message_id: str):
    key = int(message_id) if message_id.isdigit() else None
    if key is not None and tracked.pop(key, None) is not None:
        state.del_tracked(key)
        await inter.response.send_message("해제했어요.", ephemeral=True)
    else:
        await inter.response.send_message("그 ID는 추적 중이 아니에요.", ephemeral=True)


@tree.command(name="리마인더상태", description="리마인더봇 상태")
async def status_cmd(inter: discord.Interaction):
    dg = "ON" if config.WEBHOOK_URL else "OFF"
    await inter.response.send_message(
        f"🟢 리마인더봇 가동 중 | 서버 {len(client.guilds)} | 추적 {len(tracked)}건 | 다이제스트 {dg}",
        ephemeral=True)


@tree.command(name="브리핑", description="지금 아침 다이제스트 미리보기 DM (스케줄·기준선 미변경)")
async def digest_now_cmd(inter: discord.Interaction):
    if not config.WEBHOOK_URL:
        await inter.response.send_message("다이제스트 미설정(config WEBHOOK_URL 비어있음).", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    text, completed_now = await asyncio.to_thread(build_digest)
    if completed_now is None:
        await inter.followup.send("시트 읽기 실패 — reminder.log 확인.", ephemeral=True)
        return
    try:
        u = client.get_user(config.OWNER_ID) or await client.fetch_user(config.OWNER_ID)
        await _dm_long(u, text)                             # 미리보기: 스냅샷/날짜 저장 안 함
        await inter.followup.send("DM으로 미리보기 보냈어요. (스케줄/기준선 그대로)", ephemeral=True)
    except Exception:
        log.exception("브리핑 미리보기 실패")
        await inter.followup.send("DM 실패 — reminder.log 확인.", ephemeral=True)


if __name__ == "__main__":
    if (not config.TOKEN) or ("여기에" in config.TOKEN):
        log.error("config_reminder.py 의 TOKEN 을 리마인더봇 토큰(토큰B)으로 바꿔주세요.")
        sys.exit(1)
    client.run(config.TOKEN)
