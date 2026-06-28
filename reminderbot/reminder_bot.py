# reminderbot/reminder_bot.py — 룩플 리마인더봇 (always-on / 본문 안 읽음)
#   추출봇이 메시지에 붙인 우선도 이모지(🔴🟡🔵)를 사람이 클릭 → 추적 시작
#   간격 지나면 재촉 / ✅ → 해소 / 답장 → 타이머 리셋. 본문은 절대 읽지 않음(reference 메타만).
import os
import sys
import time
import socket
import logging
import unicodedata
import discord
from discord import app_commands
from discord.ext import tasks

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config_reminder as config
import state_reminder as state

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
    await alert(f"✅ 리마인더봇 시작 — 추적 {len(tracked)}건 복원")


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
    await inter.response.send_message(
        f"🟢 리마인더봇 가동 중 | 서버 {len(client.guilds)} | 추적 {len(tracked)}건", ephemeral=True)


if __name__ == "__main__":
    if (not config.TOKEN) or ("여기에" in config.TOKEN):
        log.error("config_reminder.py 의 TOKEN 을 리마인더봇 토큰(토큰B)으로 바꿔주세요.")
        sys.exit(1)
    client.run(config.TOKEN)
