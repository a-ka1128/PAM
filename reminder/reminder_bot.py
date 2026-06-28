# =========================================================
# Reminder Bot — 리마인더 엔진(B) / 수집봇과 별개 봇
#   우선순위 이모지로 플래그 → 무응답 시 재촉 → ✅반응 또는 답장이면 종료.
#   전부 로컬. 추적 항목은 reminder.db 에 저장(재시작해도 유지).
# =========================================================
import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import sys
import logging
from datetime import datetime

import config
import db

# ---------- 로깅 ----------
_h = [logging.StreamHandler(sys.stdout)]
try:
    _h.append(logging.FileHandler("reminder.log", encoding="utf-8", delay=True))
except Exception:
    pass
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S", handlers=_h)
logger = logging.getLogger("Reminder")

# ---------- 봇 ----------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# config.PRIORITY: emoji -> (label, hours, kor)
INTERVAL = {v[0]: v[1] for v in config.PRIORITY.values()}    # label -> hours
KOR = {v[0]: v[2] for v in config.PRIORITY.values()}         # label -> 한글
EMOJI_OF = {v[0]: e for e, v in config.PRIORITY.items()}     # label -> emoji
_ready_once = False


def now_iso():
    return discord.utils.utcnow().isoformat()


def is_owner(interaction):
    return interaction.user.id == config.OWNER_ID


def resolve_targets(message):
    """재촉 대상 = 멘션된 사람(봇 제외), 없으면 작성자."""
    users = [u for u in message.mentions if not u.bot]
    if not users and not message.author.bot:
        users = [message.author]
    return users


def _log_channel():
    cid = getattr(config, "LOG_CHANNEL_ID", 0)
    return bot.get_channel(cid) if cid else None


async def notify(channel_id, text, reply_to=None):
    """채널에 메시지 전송(필요시 원본 메시지에 답글)."""
    ch = bot.get_channel(channel_id)
    if ch is None:
        return None
    ref = None
    if reply_to:
        ref = discord.MessageReference(message_id=reply_to, channel_id=channel_id, fail_if_not_exists=False)
    try:
        return await ch.send(text, reference=ref)
    except Exception:
        logger.warning(f"메시지 전송 실패 ch={channel_id}")
        return None


async def activity_log(text):
    """LOG_CHANNEL_ID 가 설정돼 있으면 활동을 그 채널에도 남김."""
    ch = _log_channel()
    if ch:
        try:
            await ch.send(text)
        except Exception:
            pass


@bot.event
async def on_ready():
    global _ready_once
    if not _ready_once:
        _ready_once = True
        logger.info(f"로그인: {bot.user} | 서버 {len(bot.guilds)}개")
        try:
            for g in bot.guilds:
                bot.tree.copy_global_to(guild=g)
                await bot.tree.sync(guild=g)
            logger.info(f"명령어 동기화: 길드 {len(bot.guilds)}개")
        except Exception:
            logger.exception("명령어 동기화 실패")
        if not reminder_loop.is_running():
            reminder_loop.start()
    else:
        logger.info("재연결됨")


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        logger.info(f"새 서버 합류: {guild.name}")
    except Exception:
        logger.exception("새 서버 동기화 실패")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    emoji = str(payload.emoji)

    # 완료(✅) 처리
    if emoji == config.DONE_EMOJI:
        if db.is_tracked(payload.message_id):
            db.resolve(payload.message_id, "done", payload.user_id, "reaction")
            logger.info(f"완료(✅): msg={payload.message_id}")
            await notify(payload.channel_id, "✅ **완료 처리** (반응)", reply_to=payload.message_id)
            await activity_log(f"✅ 완료(반응) · msg={payload.message_id}")
        return

    # 우선순위 플래그
    if emoji not in config.PRIORITY:
        return
    label, hours, kor = config.PRIORITY[emoji]
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(payload.message_id)
    except Exception:
        logger.exception("플래그 메시지 fetch 실패")
        return

    targets = resolve_targets(msg)
    rec = {
        "guild_id": payload.guild_id,
        "channel_id": payload.channel_id,
        "message_id": payload.message_id,
        "jump_url": msg.jump_url,
        "preview": (msg.content or "")[:120],
        "flagged_by": payload.user_id,
        "target_ids": json.dumps([u.id for u in targets]),
        "target_names": json.dumps([u.display_name for u in targets], ensure_ascii=False),
        "priority": label,
        "created_at": now_iso(),
    }
    is_new = db.add_item(rec)
    names_str = ", ".join(u.display_name for u in targets) or "(대상 없음)"
    logger.info(f"{'추적시작' if is_new else '우선순위변경'}({kor}): msg={payload.message_id} 대상={names_str}")
    if is_new:
        await notify(payload.channel_id,
                     f"📌 **추적 시작** — {emoji} {kor} · 담당 {names_str} · {hours}시간마다 재촉\n"
                     f"→ 완료되면 이 메시지에 **✅ 반응 또는 답장**해주세요.",
                     reply_to=payload.message_id)
        await activity_log(f"📌 추적시작 [{kor}] 담당 {names_str} · {msg.jump_url}")
    else:
        await notify(payload.channel_id, f"🔁 우선순위 변경 → {emoji} {kor}", reply_to=payload.message_id)


@bot.event
async def on_message(message: discord.Message):
    # 추적 중인 메시지에 '답장'이 달리면 = 응답 → 종료
    if (message.guild and message.reference and message.reference.message_id
            and not message.author.bot and db.is_tracked(message.reference.message_id)):
        db.resolve(message.reference.message_id, "responded", message.author.id, "reply")
        logger.info(f"응답(답장): msg={message.reference.message_id} by {message.author.display_name}")
        await notify(message.channel.id, f"✅ **완료 처리** (답장 · {message.author.display_name})",
                     reply_to=message.reference.message_id)
        await activity_log(f"✅ 완료(답장) · msg={message.reference.message_id}")
    await bot.process_commands(message)


@tasks.loop(minutes=1)
async def reminder_loop():
    for item in db.open_items():
        hours = INTERVAL.get(item["priority"], 3)
        ref = item["last_reminded_at"] or item["created_at"]
        try:
            ref_dt = datetime.fromisoformat(ref)
        except Exception:
            continue
        elapsed_h = (discord.utils.utcnow() - ref_dt).total_seconds() / 3600
        if elapsed_h >= hours:
            try:
                await send_reminder(item)
            except Exception:
                logger.exception("리마인드 전송 실패")
            db.mark_reminded(item["id"])


async def send_reminder(item):
    target_ids = json.loads(item["target_ids"] or "[]")
    kor = KOR.get(item["priority"], item["priority"])
    emoji = EMOJI_OF.get(item["priority"], "")
    text = (f"{emoji} **리마인드** ({kor}) — 아직 처리 안 된 항목이에요.\n"
            f"> {item['preview']}\n{item['jump_url']}\n"
            f"완료하셨으면 **원본 메시지에 ✅ 반응 또는 답장**해주세요.")
    delivered = False
    if config.ALERT == "dm":
        for uid in target_ids:
            try:
                user = bot.get_user(uid) or await bot.fetch_user(uid)
                await user.send(text)
                delivered = True
            except Exception:
                logger.warning(f"DM 실패 uid={uid}")
    # 채널 모드이거나 DM 전원 실패 시 채널에 멘션 핑
    if config.ALERT == "channel" or not delivered:
        ch = bot.get_channel(item["channel_id"])
        if ch:
            mentions = " ".join(f"<@{uid}>" for uid in target_ids)
            try:
                await ch.send(f"{mentions} {text}".strip())
            except Exception:
                logger.warning(f"채널 핑 실패 ch={item['channel_id']}")
    await activity_log(f"🔔 재촉 [{kor}] · {item['jump_url']}")


# ---------- 명령어 (소유자 전용) ----------
@bot.tree.command(name="추적목록", description="미해결 추적 항목을 우선순위순으로 보여줍니다")
async def list_cmd(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
    items = db.open_items()
    if not items:
        return await interaction.response.send_message("미해결 추적 항목이 없습니다. 🎉", ephemeral=True)
    lines = []
    for it in items:
        names = ", ".join(json.loads(it["target_names"] or "[]")) or "(대상 없음)"
        emoji = EMOJI_OF.get(it["priority"], "")
        age_h = (discord.utils.utcnow() - datetime.fromisoformat(it["created_at"])).total_seconds() / 3600
        lines.append(f"`#{it['id']}` {emoji} **{names}** · {age_h:.1f}h 경과 · {it['remind_count']}회 알림\n"
                     f"　{it['jump_url']}")
    embed = discord.Embed(title=f"📋 미해결 {len(items)}건",
                          description="\n".join(lines[:25]), color=0xE67E22)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="추적해제", description="추적 항목을 수동으로 종료합니다 (/추적목록 의 #번호)")
@app_commands.describe(번호="해제할 항목 번호")
async def untrack_cmd(interaction: discord.Interaction, 번호: int):
    if not is_owner(interaction):
        return await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
    ok = db.resolve_by_id(번호, "cancelled", interaction.user.id, "manual")
    if ok:
        await interaction.response.send_message(f"✅ `#{번호}` 추적 해제했습니다.", ephemeral=True)
    else:
        await interaction.response.send_message(f"`#{번호}` 를 찾을 수 없어요(이미 종료됐을 수 있음).", ephemeral=True)


# ---------- 실행 ----------
if __name__ == "__main__":
    db.init()
    logger.info(f"DB: {db.DB_PATH}")
    if config.TOKEN.startswith("여기에"):
        logger.error("config.py 의 TOKEN 을 리마인더 봇 토큰으로 바꿔주세요.")
        sys.exit(1)
    bot.run(config.TOKEN, log_handler=None)
