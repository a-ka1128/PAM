# =========================================================
# Collector — 디스코드 서버 활동 수집 봇 (로컬 전용 / 1단계)
#   서버(길드)마다 data/<이름>_<ID>.db 파일에 따로 저장한다.
#   목적: 며칠치 데이터를 모아 2단계(로컬 LLM 분석)에서 자동화 거리를 찾기.
# =========================================================
import discord
from discord import app_commands
from discord.ext import commands, tasks
import re
import sys
import logging
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional

import config
import database as db

# ==========================================
# 0. 로깅 (콘솔 + 파일)
# ==========================================
_log_handlers = [logging.StreamHandler(sys.stdout)]
try:
    # delay=True: 첫 로그 시점에 파일을 연다 → 재시작 시 로그파일 잠금 충돌 회피
    _log_handlers.append(logging.FileHandler("collector.log", encoding="utf-8", delay=True))
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=_log_handlers,
)
logger = logging.getLogger("Collector")

# ==========================================
# 1. 인텐트 & 봇
# ==========================================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================
# 2. 전역 상태
# ==========================================
STATE = "stopped"          # collecting / paused / stopped
START_TS = None
_ready_once = False

URL_RE = re.compile(r"https?://([^/\s]+)", re.IGNORECASE)
KST = timezone(timedelta(hours=9))   # 한국 표준시


# ==========================================
# 3. 유틸
# ==========================================
def now_iso():
    return discord.utils.utcnow().isoformat()


def parse_kst(s):
    """'2026-06-26 23:00' (KST) 문자열 → tz-aware datetime. 실패 시 None."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def maybe_hash(user_id):
    if config.HASH_AUTHORS:
        return hashlib.sha256(str(user_id).encode()).hexdigest()[:16]
    return str(user_id)


def is_owner(interaction: discord.Interaction):
    return interaction.user.id == config.OWNER_ID


def extract_domains(text):
    if not text:
        return []
    out = []
    for host in URL_RE.findall(text):
        host = host.lower()
        if host not in out:
            out.append(host)
    return out


def channel_allowed(channel):
    if channel.id in config.EXCLUDED_CHANNEL_IDS:
        return False
    parent_id = getattr(channel, "parent_id", None)
    if parent_id and parent_id in config.EXCLUDED_CHANNEL_IDS:
        return False
    return True


def collectable_channels(guild):
    out = []
    for ch in list(guild.text_channels) + list(guild.threads):
        if not channel_allowed(ch):
            continue
        perms = ch.permissions_for(guild.me)
        if not (perms.view_channel and perms.read_message_history):
            continue
        out.append(ch)
    return out


def gdb_for(guild):
    """discord Guild 객체로 해당 서버 DB 핸들 얻기."""
    return db.for_guild(guild.id, guild.name)


# ==========================================
# 4. 메시지 저장 (해당 서버 파일로)
# ==========================================
def store_message(message: discord.Message, source: str):
    content = message.content or ""
    domains = extract_domains(content)
    is_thread = isinstance(message.channel, discord.Thread)
    guild = message.guild
    gdb = gdb_for(guild)

    rec = {
        "message_id": message.id,
        "guild_id": guild.id,
        "channel_id": message.channel.id,
        "thread_id": message.channel.id if is_thread else None,
        "author_id": maybe_hash(message.author.id),
        "author_name": str(message.author),
        "is_bot": int(message.author.bot),
        "content": content,
        "created_at": message.created_at.isoformat(),
        "edited_at": message.edited_at.isoformat() if message.edited_at else None,
        "length": len(content),
        "is_reply": int(bool(message.reference and message.reference.message_id)),
        "replied_to_id": message.reference.message_id if message.reference else None,
        "has_attachment": int(len(message.attachments) > 0),
        "attachment_count": len(message.attachments),
        "has_link": int(bool(domains)),
        "link_domains": ",".join(domains) if domains else None,
        "mention_user_count": len(message.mentions),
        "mention_role_count": len(message.role_mentions),
        "source": source,
    }

    inserted = gdb.insert_message(rec)
    if inserted:
        atts = []
        for a in message.attachments:
            ext = a.filename.rsplit(".", 1)[-1].lower() if "." in a.filename else ""
            atts.append((message.id, a.filename, ext, a.content_type, a.size))
        gdb.insert_attachments(atts)

        mentions = [(message.id, "user", u.id, str(u)) for u in message.mentions]
        mentions += [(message.id, "role", r.id, r.name) for r in message.role_mentions]
        gdb.insert_mentions(mentions)

        if source == "backfill" and message.reactions:
            for reaction in message.reactions:
                gdb.insert_reaction({
                    "message_id": message.id, "guild_id": guild.id,
                    "channel_id": message.channel.id, "emoji": str(reaction.emoji),
                    "user_id": None, "count": reaction.count,
                    "added_at": now_iso(), "source": "backfill",
                })

    if source != "backfill":
        gdb.update_cursor(message.channel.id, guild.id, message.id, rec["created_at"])
    return inserted


# ==========================================
# 5. gap-fill / 백필
# ==========================================
async def gap_fill_all():
    total = 0
    for guild in bot.guilds:
        gdb = gdb_for(guild)
        for ch in collectable_channels(guild):
            cur = gdb.get_cursor(ch.id)
            if not cur or not cur["last_message_id"]:
                continue
            try:
                async for msg in ch.history(limit=None,
                                            after=discord.Object(id=cur["last_message_id"]),
                                            oldest_first=True):
                    if store_message(msg, "gapfill"):
                        total += 1
            except discord.Forbidden:
                continue
            except Exception:
                logger.exception(f"gap-fill 실패: #{ch}")
    logger.info(f"gap-fill 저장 {total}건")
    return total


async def backfill_guild(guild, days, on_progress=None):
    after_dt = discord.utils.utcnow() - timedelta(days=days)
    chans = collectable_channels(guild)
    total = 0
    for i, ch in enumerate(chans, 1):
        ch_count = 0
        try:
            async for msg in ch.history(limit=None, after=after_dt, oldest_first=True):
                if store_message(msg, "backfill"):
                    total += 1
                    ch_count += 1
        except discord.Forbidden:
            continue
        except Exception:
            logger.exception(f"백필 실패: #{ch}")
        if on_progress:
            await on_progress(i, len(chans), ch.name, ch_count, total)
    return total


# ==========================================
# 6. 구조 스냅샷
# ==========================================
async def capture_snapshots():
    ts = now_iso()
    n = 0
    for guild in bot.guilds:
        gdb = gdb_for(guild)
        gdb.insert_guild_snapshot((ts, guild.id, guild.name, guild.member_count))
        for ch in list(guild.text_channels) + list(guild.voice_channels) + list(guild.threads):
            cat = ch.category.name if getattr(ch, "category", None) else None
            topic = getattr(ch, "topic", None)
            gdb.insert_channel_snapshot(
                (ts, guild.id, ch.id, ch.name, str(ch.type), cat, topic, getattr(ch, "position", None)))
        for role in guild.roles:
            gdb.insert_role_snapshot((ts, guild.id, role.id, role.name, role.position, len(role.members)))
        n += 1
    logger.info(f"구조 스냅샷 저장: 서버 {n}개")


@tasks.loop(hours=max(config.SNAPSHOT_INTERVAL_HOURS, 1))
async def snapshot_loop():
    await capture_snapshots()


# ==========================================
# 6.5 예약 종료 / 예약 중지
# ==========================================
def _parse_schedule_target(hours, at_str):
    """(시간, 일시) → (target_utc, error_msg). 둘 중 하나만 사용."""
    if hours is None and at_str is None:
        return None, "`시간`(N시간 후) 또는 `일시`(KST) 중 하나를 지정하세요."
    if at_str:
        kst = parse_kst(at_str)
        if not kst:
            return None, "일시 형식이 올바르지 않습니다. 예: `2026-06-26 23:00`"
        target = kst.astimezone(timezone.utc)
    else:
        if hours <= 0:
            return None, "시간은 1 이상이어야 합니다."
        target = discord.utils.utcnow() + timedelta(hours=hours)
    if target <= discord.utils.utcnow():
        return None, "이미 지난 시각입니다."
    return target, None


def _get_schedule():
    """현재 예약 (at_iso, action) 반환. 구버전 scheduled_stop_utc 는 자동 이관."""
    legacy = db.get_state("scheduled_stop_utc")
    if legacy and not db.get_state("scheduled_at_utc"):
        db.set_state("scheduled_at_utc", legacy)
        db.set_state("scheduled_action", "stop")
        db.set_state("scheduled_stop_utc", None)
    return db.get_state("scheduled_at_utc"), db.get_state("scheduled_action", "stop")


async def notify_owner(action):
    try:
        user = bot.get_user(config.OWNER_ID) or await bot.fetch_user(config.OWNER_ID)
        total = sum(g.stats()["messages"] for g in db.all_guild_dbs())
        if action == "pause":
            msg = (f"⏸️ 예약 중지 완료 — 수집을 일시중지했습니다(총 {total}건). "
                   f"다음 `/수집 시작` 때 그 사이 메시지를 메웁니다.")
        else:
            msg = f"⏹️ 예약 종료 완료 — 수집을 중단했습니다(총 {total}건). (재개: `/수집 시작`)"
        await user.send(msg)
    except Exception:
        logger.exception("예약 알림 DM 실패")


async def check_schedule(notify=True):
    """예약 시각이 지났으면 종료/중지 처리. 처리했으면 True."""
    global STATE
    at, action = _get_schedule()
    if not at:
        return False
    try:
        target = datetime.fromisoformat(at)
    except Exception:
        db.set_state("scheduled_at_utc", None)
        return False
    if discord.utils.utcnow() >= target:
        was = STATE
        STATE = "paused" if action == "pause" else "stopped"
        db.set_state("collecting_state", STATE)
        db.set_state("scheduled_at_utc", None)
        db.set_state("scheduled_action", None)
        logger.info(f"⏰ 예약 {action} 시각 도달 → 수집 {STATE}")
        if notify and was == "collecting":
            await notify_owner(action)
        return True
    return False


@tasks.loop(seconds=30)
async def schedule_loop():
    await check_schedule()


# ==========================================
# 7. 이벤트
# ==========================================
@bot.event
async def on_ready():
    global _ready_once, STATE, START_TS
    if not _ready_once:
        _ready_once = True
        START_TS = discord.utils.utcnow()
        STATE = db.get_state("collecting_state", "stopped")
        logger.info(f"로그인: {bot.user} | 서버 {len(bot.guilds)}개 | 현재 상태: {STATE}")

        # 슬래시 명령어 동기화 (초대된 모든 서버에 즉시 등록 — 서버 ID 하드코딩 없음)
        try:
            for g in bot.guilds:
                bot.tree.copy_global_to(guild=g)
                await bot.tree.sync(guild=g)
            logger.info(f"명령어 동기화 완료: 대상 길드 {len(bot.guilds)}개")
        except Exception:
            logger.exception("명령어 동기화 실패 — 명령어 이름 문제면 README의 영문 대체안 참고")

        # 구조 스냅샷 (주기 루프가 켜져 있으면 루프의 첫 즉시 실행이 담당 → 중복 방지)
        if config.SNAPSHOT_INTERVAL_HOURS > 0:
            if not snapshot_loop.is_running():
                snapshot_loop.start()
        else:
            await capture_snapshots()
        if not schedule_loop.is_running():
            schedule_loop.start()
    else:
        logger.info("게이트웨이 재연결 감지")

    # 예약 종료 시각이 이미 지났으면 gap-fill 전에 먼저 반영
    await check_schedule()

    # 🛟 매 (재)연결마다: 수집 중이면 gap-fill 으로 놓친 구간 복구.
    #   - 절전/슬립 후 깨어나 재연결되면 자던 사이 메시지를 메움
    #   - 프로세스 재시작(비정상 종료 포함)도 동일하게 커버
    if STATE == "collecting":
        logger.info("gap-fill 실행 (놓친 구간 복구)")
        await gap_fill_all()


@bot.event
async def on_guild_join(guild: discord.Guild):
    logger.info(f"새 서버 합류: {guild.name} ({guild.id}) — 명령어 등록")
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except Exception:
        logger.exception("새 서버 명령어 동기화 실패")
    await capture_snapshots()


@bot.event
async def on_message(message: discord.Message):
    if STATE == "collecting" and message.guild and channel_allowed(message.channel):
        try:
            store_message(message, "live")
        except Exception:
            logger.exception("on_message 저장 실패")
    await bot.process_commands(message)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if payload.guild_id is None:
        return
    data = payload.data
    if "content" in data:
        try:
            guild = bot.get_guild(payload.guild_id)
            db.for_guild(payload.guild_id, guild.name if guild else None) \
              .mark_edited(payload.message_id, data.get("content"), now_iso())
        except Exception:
            logger.exception("edit 처리 실패")


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.guild_id is None:
        return
    try:
        guild = bot.get_guild(payload.guild_id)
        db.for_guild(payload.guild_id, guild.name if guild else None) \
          .mark_deleted(payload.message_id, now_iso())
    except Exception:
        logger.exception("delete 처리 실패")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if STATE != "collecting" or payload.guild_id is None:
        return
    try:
        guild = bot.get_guild(payload.guild_id)
        db.for_guild(payload.guild_id, guild.name if guild else None).insert_reaction({
            "message_id": payload.message_id,
            "guild_id": payload.guild_id,
            "channel_id": payload.channel_id,
            "emoji": str(payload.emoji),
            "user_id": maybe_hash(payload.user_id),
            "count": 1,
            "added_at": now_iso(),
            "source": "live",
        })
    except Exception:
        logger.exception("reaction 처리 실패")


@bot.event
async def on_member_join(member: discord.Member):
    if STATE == "collecting":
        gdb_for(member.guild).insert_member_event(
            (member.guild.id, maybe_hash(member.id), str(member), "join", now_iso()))


@bot.event
async def on_member_remove(member: discord.Member):
    if STATE == "collecting":
        gdb_for(member.guild).insert_member_event(
            (member.guild.id, maybe_hash(member.id), str(member), "leave", now_iso()))


@bot.event
async def on_voice_state_update(member, before, after):
    if STATE != "collecting":
        return
    if before.channel is None and after.channel is not None:
        et, ch = "join", after.channel
    elif before.channel is not None and after.channel is None:
        et, ch = "leave", before.channel
    elif before.channel and after.channel and before.channel.id != after.channel.id:
        et, ch = "move", after.channel
    else:
        return
    gdb_for(member.guild).insert_voice_event(
        (member.guild.id, ch.id, ch.name, maybe_hash(member.id), str(member), et, now_iso()))


# ==========================================
# 8. 명령어 (모두 소유자 전용)
# ==========================================
collect = app_commands.Group(name="수집", description="데이터 수집 제어 (소유자 전용)")


@collect.command(name="시작", description="수집 시작 (중지 후라면 그동안 놓친 메시지를 메웁니다)")
async def collect_start(interaction: discord.Interaction):
    global STATE
    if not is_owner(interaction):
        return await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
    prev = STATE
    STATE = "collecting"
    db.set_state("collecting_state", STATE)
    if prev == "paused":
        await interaction.response.send_message("▶️ 수집 시작 — 중지된 동안 놓친 메시지 메우는 중...", ephemeral=True)
        filled = await gap_fill_all()
        await interaction.followup.send(f"✅ gap-fill 완료: {filled}건 보충. 이후 실시간 수집 중.", ephemeral=True)
    else:
        await interaction.response.send_message(f"▶️ 수집 시작 (이전: {prev}). 실시간 수집 중.", ephemeral=True)
    logger.info(f"수집 시작 (prev={prev})")


@collect.command(name="중지", description="컴퓨터 끄기 전 일시중지 (다음 시작 때 빈 구간을 메웁니다)")
async def collect_pause(interaction: discord.Interaction):
    global STATE
    if not is_owner(interaction):
        return await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
    STATE = "paused"
    db.set_state("collecting_state", STATE)
    await interaction.response.send_message(
        "⏸️ 수집 일시중지. 컴퓨터 꺼도 됩니다 — 다음 `/수집 시작` 때 그 사이 메시지를 메웁니다.", ephemeral=True)
    logger.info("수집 중지(pause)")


@collect.command(name="종료", description="수집 세션 종료 (이후 공백은 메우지 않습니다)")
async def collect_end(interaction: discord.Interaction):
    global STATE
    if not is_owner(interaction):
        return await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
    STATE = "stopped"
    db.set_state("collecting_state", STATE)
    await interaction.response.send_message("⏹️ 수집 종료. (다시 모으려면 `/수집 시작`)", ephemeral=True)
    logger.info("수집 종료(stop)")


bot.tree.add_command(collect)


@bot.tree.command(name="백필", description="이 서버의 과거 메시지를 지정한 일수만큼 가져옵니다")
@app_commands.describe(days="가져올 과거 일수 (예: 14)")
@app_commands.rename(days="일수")
async def backfill_cmd(interaction: discord.Interaction, days: int = config.DEFAULT_BACKFILL_DAYS):
    if not is_owner(interaction):
        return await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
    if interaction.guild is None:
        return await interaction.response.send_message("서버 안에서 실행해주세요.", ephemeral=True)

    await interaction.response.send_message(
        f"📥 백필 시작: 최근 {days}일 · 서버 '{interaction.guild.name}'", ephemeral=True)
    prog = await interaction.followup.send("준비 중...", ephemeral=True, wait=True)

    async def on_progress(i, n, ch_name, ch_count, total):
        try:
            await prog.edit(content=f"📥 백필 진행: 채널 {i}/{n} · #{ch_name} ({ch_count}건) · 누적 {total}건")
        except Exception:
            pass

    total = await backfill_guild(interaction.guild, days, on_progress)
    await prog.edit(content=f"✅ 백필 완료: '{interaction.guild.name}' 최근 {days}일 · 새로 {total}건 저장(중복 제외).")
    logger.info(f"백필 완료 guild={interaction.guild.id} days={days} total={total}")


@bot.tree.command(name="예약종료", description="지정 시각에 수집을 자동 종료 (이후 공백은 안 메움)")
@app_commands.describe(시간="지금부터 N시간 뒤 (예: 72)", 일시="특정 KST 일시 (예: 2026-06-26 23:00)")
async def schedule_stop_cmd(interaction: discord.Interaction,
                            시간: Optional[int] = None, 일시: Optional[str] = None):
    if not is_owner(interaction):
        return await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
    target, err = _parse_schedule_target(시간, 일시)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    db.set_state("scheduled_at_utc", target.isoformat())
    db.set_state("scheduled_action", "stop")
    kst_str = target.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    await interaction.response.send_message(
        f"⏹️ 예약 완료: **{kst_str} (KST)** 에 수집 **종료**.", ephemeral=True)
    logger.info(f"예약 종료 설정: {target.isoformat()}")


@bot.tree.command(name="예약중지", description="지정 시각에 수집을 자동 일시중지 (다음 시작 때 빈 구간 메움)")
@app_commands.describe(시간="지금부터 N시간 뒤 (예: 8)", 일시="특정 KST 일시 (예: 2026-06-25 03:00)")
async def schedule_pause_cmd(interaction: discord.Interaction,
                             시간: Optional[int] = None, 일시: Optional[str] = None):
    if not is_owner(interaction):
        return await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
    target, err = _parse_schedule_target(시간, 일시)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    db.set_state("scheduled_at_utc", target.isoformat())
    db.set_state("scheduled_action", "pause")
    kst_str = target.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    await interaction.response.send_message(
        f"⏸️ 예약 완료: **{kst_str} (KST)** 에 수집 **일시중지**. (다음 `/수집 시작` 때 빈 구간 메움)",
        ephemeral=True)
    logger.info(f"예약 중지 설정: {target.isoformat()}")


@bot.tree.command(name="예약취소", description="예약된 자동 종료/중지를 취소합니다")
async def schedule_cancel_cmd(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
    if db.get_state("scheduled_at_utc") or db.get_state("scheduled_stop_utc"):
        db.set_state("scheduled_at_utc", None)
        db.set_state("scheduled_action", None)
        db.set_state("scheduled_stop_utc", None)
        await interaction.response.send_message("✅ 예약을 취소했습니다.", ephemeral=True)
    else:
        await interaction.response.send_message("예약된 종료/중지가 없습니다.", ephemeral=True)


@bot.tree.command(name="상태", description="수집 상태와 통계를 보여줍니다")
async def status_cmd(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
    statlist = [(g, g.stats()) for g in db.all_guild_dbs()]
    total = sum(s["messages"] for _, s in statlist)
    icon = {"collecting": "🟢", "paused": "⏸️", "stopped": "⏹️"}.get(STATE, "❔")
    lines = [f"{icon} 상태: **{STATE}**  ·  서버 {len(statlist)}개",
             f"💬 총 메시지: **{total}**"]
    if START_TS:
        lines.append(f"⏱ 가동: {str(discord.utils.utcnow() - START_TS).split('.')[0]}")
    for g, st in statlist:
        guild = bot.get_guild(g.guild_id)
        name = guild.name if guild else g.guild_id
        lines.append(f"　· **{name}**: 메시지 {st['messages']} · 반응 {st['reactions']} · "
                     f"음성 {st['voice']} · 채널 {st['channels']}")
    at, action = _get_schedule()
    if at:
        try:
            kst_str = datetime.fromisoformat(at).astimezone(KST).strftime("%Y-%m-%d %H:%M")
            lines.append(f"⏰ 예약 {'중지' if action == 'pause' else '종료'}: {kst_str} (KST)")
        except Exception:
            pass
    embed = discord.Embed(title="Collector 상태 (서버별 파일)", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================
# 9. 실행
# ==========================================
if __name__ == "__main__":
    db.init()
    db.open_existing_guild_files()
    logger.info(f"데이터 폴더: {db.DATA_DIR}")
    if config.TOKEN.startswith("여기에"):
        logger.error("config.py 의 TOKEN 을 실제 봇 토큰으로 바꿔주세요.")
        sys.exit(1)
    bot.run(config.TOKEN, log_handler=None)
