"""디스코드 봇 진입점: 서버 일원처럼 채팅 + 관리자 전용 슬래시 명령."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

from .chat import ChatEngine
from .config import discord_token, load_settings
from .llm import LLM
from .memory import Memory
from .persona import Persona

settings = load_settings()
logging.basicConfig(
    level=getattr(logging, settings.get("log_level", "INFO")),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

dcfg = settings["discord"]
gcfg = settings["group"]
pcfg = settings["proactive"]
ADMIN_IDS: set[int] = {int(x) for x in dcfg["admin_user_ids"]}

persona = Persona(settings.get("character", ""))
llm = LLM(settings["ollama"])
memory = Memory(settings["memory"]["db_path"], settings["memory"]["max_facts_per_user"])
engine = ChatEngine(settings, persona, llm, memory)

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True


@dataclass
class Pending:
    message: discord.Message
    ts: float          # time.monotonic()
    priority: bool     # 멘션 또는 봇 메시지에 대한 답장


class AdminTree(app_commands.CommandTree):
    """모든 슬래시 명령을 관리자만 쓸 수 있게 한다."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in ADMIN_IDS:
            return True
        await interaction.response.send_message("관리자만 사용할 수 있어요.", ephemeral=True)
        return False


class GirlBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.tree = AdminTree(self)
        self.started_at = time.time()
        self.gpu_lock = engine.gpu_lock
        self.dm_locks: dict[int, asyncio.Lock] = {}
        self.pending: dict[int, list[Pending]] = {}
        self.next_ok: dict[int, float] = {}      # 채널별 다음 발언 가능 시각 (monotonic)
        self.busy: set[int] = set()
        # 런타임 상태 (DB kv 에 저장되어 재시작 후에도 유지)
        self.paused = False
        self.cooldown = float(gcfg["cooldown_sec"])
        self.channels: set[int] = {int(c) for c in dcfg["channel_ids"]}

    async def setup_hook(self) -> None:
        await memory.init()
        await self.load_state()
        await self.tree.sync()
        group_loop.start()
        if pcfg["enabled"]:
            proactive_loop.change_interval(minutes=pcfg["check_interval_min"])
            proactive_loop.start()

    async def load_state(self) -> None:
        self.paused = (await memory.kv_get("paused", "0")) == "1"
        cd = await memory.kv_get("cooldown")
        if cd:
            self.cooldown = float(cd)
        ch = await memory.kv_get("channels")
        if ch:
            self.channels = {int(c) for c in json.loads(ch)}
        char = await memory.kv_get("character")
        if char:
            persona.load(char)

    async def save_channels(self) -> None:
        await memory.kv_set("channels", json.dumps(sorted(self.channels)))

    async def close(self) -> None:
        await memory.close()
        await super().close()


bot = GirlBot()


# ============================================================
#  대화 처리
# ============================================================
def user_allowed(user_id: int) -> bool:
    allowed = dcfg["allowed_user_ids"]
    return not allowed or user_id in allowed or user_id in ADMIN_IDS


def clean_text(message: discord.Message) -> str:
    """멘션 정리 + 답장이면 '누구의 어떤 말에 답장'인지 앞에 붙인다 (상황 파악용)."""
    text = message.clean_content.strip()
    if bot.user:
        text = text.replace(f"@{bot.user.display_name}", "").strip()
    ref = message.reference
    if text and ref and isinstance(ref.resolved, discord.Message):
        target = ref.resolved
        who = persona.name if (bot.user and target.author.id == bot.user.id) else target.author.display_name
        quoted = target.clean_content.replace("\n", " ").strip()
        quoted = quoted if len(quoted) <= 30 else quoted[:29] + "…"
        text = f'({who}의 "{quoted}"에 답장) {text}'
    return text


def is_priority(message: discord.Message) -> bool:
    """봇을 멘션했거나, 봇의 메시지에 답장한 경우"""
    if bot.user and bot.user in message.mentions:
        return True
    ref = message.reference
    if ref and isinstance(ref.resolved, discord.Message):
        return ref.resolved.author.id == (bot.user.id if bot.user else 0)
    return False


async def send_bubbles(
    channel: discord.abc.Messageable, bubbles: list[str], reply_to: discord.Message | None = None
) -> None:
    tcfg = settings["typing"]
    for i, text in enumerate(bubbles):
        if tcfg["enabled"]:
            async with channel.typing():
                await asyncio.sleep(engine.typing_delay(text))
        if i == 0 and reply_to is not None:
            try:
                await reply_to.reply(text, mention_author=False)
                continue
            except discord.HTTPException:
                pass
        await channel.send(text)


def next_cooldown() -> float:
    return bot.cooldown * (1 + random.random() * gcfg["cooldown_jitter"])


@bot.event
async def on_ready() -> None:
    log.info("로그인: %s | 모델: %s | 캐릭터: %s", bot.user, llm.model, persona.name)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not user_allowed(message.author.id):
        return
    text = clean_text(message)
    if not text:
        return

    # ----- DM: 1:1 이므로 바로 대답 -----
    if isinstance(message.channel, discord.DMChannel):
        if dcfg["respond_to_dm"] and not bot.paused:
            await handle_dm(message, text)
        return

    # ----- 서버 채널: 기록해 두고, 쿨다운 후 랜덤하게 골라서 대답 -----
    in_channel = message.channel.id in bot.channels
    mentioned = dcfg["respond_to_mention"] and is_priority(message)
    if not (in_channel or mentioned):
        return
    await memory.add_message(message.channel.id, message.author.id, message.author.display_name, "user", text)
    if bot.paused:
        return
    bot.pending.setdefault(message.channel.id, []).append(
        Pending(message, time.monotonic(), priority=mentioned)
    )


async def handle_dm(message: discord.Message, text: str) -> None:
    uid = message.author.id
    lock = bot.dm_locks.setdefault(uid, asyncio.Lock())
    async with lock:
        await memory.set_dm_channel(uid, message.channel.id)
        await memory.add_message(message.channel.id, uid, message.author.display_name, "user", text)
        lo, hi = settings["typing"]["read_delay"]
        await asyncio.sleep(random.uniform(lo, hi))
        try:
            async with bot.gpu_lock:
                async with message.channel.typing():
                    bubbles = await engine.reply(
                        message.channel.id, uid, message.author.display_name, text, dm=True)
        except Exception:  # noqa: BLE001
            log.exception("DM 응답 생성 실패")
            return
        if bubbles:
            await send_bubbles(message.channel, bubbles)


async def respond_group(channel_id: int, target: Pending) -> None:
    msg = target.message
    channel = msg.channel
    bot.busy.add(channel_id)
    try:
        async with bot.gpu_lock:
            async with channel.typing():
                bubbles = await engine.reply(
                    channel_id, msg.author.id, msg.author.display_name, clean_text(msg),
                    can_skip=not target.priority,   # 멘션/답장이 아니면 끼어들 이유가 없을 때 SKIP 가능
                )
        if bubbles:
            # 그 사이에 다른 메시지가 올라왔으면 '답장'으로 누구에게 하는 말인지 표시
            latest = getattr(channel, "last_message_id", None)
            reply_to = msg if latest and latest != msg.id else None
            await send_bubbles(channel, bubbles, reply_to)
    except Exception:  # noqa: BLE001
        log.exception("서버 응답 생성 실패 (channel=%s)", channel_id)
    finally:
        # 대답이 완전히 끝난 시점부터 쿨다운 타이머 시작
        bot.next_ok[channel_id] = time.monotonic() + next_cooldown()
        bot.busy.discard(channel_id)


@tasks.loop(seconds=2)
async def group_loop() -> None:
    """채널별로 쿨다운이 끝났으면, 그동안 올라온 메시지 중 하나를 골라 대답한다."""
    if bot.paused:
        return
    now = time.monotonic()
    for channel_id, items in list(bot.pending.items()):
        items[:] = [p for p in items if now - p.ts <= gcfg["max_pending_age_sec"]]
        if not items or channel_id in bot.busy:
            continue
        prio = [p for p in items if p.priority]
        cooling = now < bot.next_ok.get(channel_id, 0)
        if cooling and not (prio and gcfg["mention_bypass_cooldown"]):
            continue
        if prio:
            target = prio[-1]
        else:
            if random.random() > gcfg["reply_chance"]:
                items.clear()          # 이번 메시지들은 그냥 넘어간다
                continue
            target = random.choice(items)
        items.clear()                  # 나머지 메시지는 대화 기록으로 맥락에만 반영
        asyncio.create_task(respond_group(channel_id, target))


@group_loop.before_loop
async def _group_ready() -> None:
    await bot.wait_until_ready()


# ------------------ 먼저 말 걸기 (DM) ------------------
@tasks.loop(minutes=10)
async def proactive_loop() -> None:
    if bot.paused:
        return
    now = engine.now()
    lo, hi = pcfg["active_hours"]
    if not (lo <= now.hour < hi):
        return
    today = now.strftime("%Y-%m-%d")
    for u in await memory.proactive_candidates():
        uid = u["user_id"]
        if not user_allowed(uid):
            continue
        last = max(u["last_user_ts"] or 0, u["last_bot_ts"] or 0)
        idle_h = (time.time() - last) / 3600
        if idle_h < pcfg["idle_hours_min"]:
            continue
        count = u["proactive_count"] if u["proactive_day"] == today else 0
        if count >= pcfg["max_per_day"] or random.random() > pcfg["chance"]:
            continue
        lock = bot.dm_locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            continue
        async with lock:
            try:
                user = bot.get_user(uid) or await bot.fetch_user(uid)
                channel = user.dm_channel or await user.create_dm()
                async with bot.gpu_lock:
                    bubbles = await engine.proactive(channel.id, uid, user.display_name, idle_h)
                if bubbles:
                    await send_bubbles(channel, bubbles)
                    await memory.mark_proactive(uid, today, count + 1)
            except Exception:  # noqa: BLE001
                log.exception("먼저 말 걸기 실패 (uid=%s)", uid)


@proactive_loop.before_loop
async def _pro_ready() -> None:
    await bot.wait_until_ready()


# ============================================================
#  슬래시 명령 (전부 관리자 전용 — AdminTree.interaction_check)
# ============================================================
def fmt(n: int | float | None) -> str:
    return f"{int(n or 0):,}"


def kst(ts: float) -> str:
    return datetime.fromtimestamp(ts, engine.tz).strftime("%m-%d %H:%M")


def fmt_uptime(sec: float) -> str:
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return (f"{d}일 " if d else "") + f"{h}시간 {m}분"


def trunc(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


# ---- 1. 사용량-총 ----
@bot.tree.command(name="사용량-총", description="지금까지 사용한 전체 토큰 사용량을 보여줘요")
async def usage_total(interaction: discord.Interaction) -> None:
    u = await memory.usage_total()
    n, p, c = u["all"]
    lines = [
        f"**전체 토큰 사용량**" + (f" (기록 시작 {kst(u['since'])})" if u["since"] else ""),
        f"```총 {fmt(p + c)} 토큰 | 호출 {fmt(n)}회",
        f"입력(프롬프트) {fmt(p)} / 출력(생성) {fmt(c)}",
        f"최근 24시간 {fmt(u['day'][1] + u['day'][2])} ({fmt(u['day'][0])}회)",
        f"최근 7일   {fmt(u['week'][1] + u['week'][2])} ({fmt(u['week'][0])}회)```",
    ]
    if u["by_kind"]:
        names = {"reply": "일반 답변", "memory": "기억 추출", "proactive": "먼저 말걸기"}
        lines.append("**용도별**\n```" + "\n".join(
            f"{names.get(k, k):<8} {fmt((pp or 0) + (cc or 0)):>12} 토큰 ({fmt(nn)}회)" for k, nn, pp, cc in u["by_kind"]
        ) + "```")
    if u["by_character"]:
        lines.append("**캐릭터별**\n```" + "\n".join(
            f"{(k or '-'):<10} {fmt((pp or 0) + (cc or 0)):>12} 토큰 ({fmt(nn)}회)" for k, nn, pp, cc in u["by_character"]
        ) + "```")
    lines.append("-# 입력 토큰은 Ollama가 캐시를 쓰면 실제보다 적게 잡힐 수 있어요.")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---- 2. 사용량-유저 ----
@bot.tree.command(name="사용량-유저", description="유저별 토큰 사용량, 그리고 그 유저에게 한 답변 내역을 확인해요")
@app_commands.describe(user="특정 유저를 고르면 그 유저에게 한 답변 내역까지 보여줘요 (생략하면 전체 순위)")
@app_commands.rename(user="유저")
async def usage_user(interaction: discord.Interaction, user: Optional[discord.User] = None) -> None:
    if user is None:
        rows = await memory.usage_by_user(15)
        if not rows:
            await interaction.response.send_message("아직 기록이 없어요.", ephemeral=True)
            return
        body = "\n".join(
            f"{i:>2}. {trunc(name or str(uid), 14):<14} {fmt((p or 0) + (c or 0)):>10} 토큰  답변 {fmt(n)}회"
            for i, (uid, name, n, p, c) in enumerate(rows, 1)
        )
        await interaction.response.send_message(
            f"**유저별 토큰 사용량 (상위 {len(rows)}명)**\n```{body}```\n"
            "-# 개인 내역은 `/사용량-유저 유저:@이름` 으로 확인", ephemeral=True)
        return

    d = await memory.usage_user_detail(user.id, 10)
    name, n, p, c, first, last = d["summary"]
    if not n:
        await interaction.response.send_message(f"{user.display_name} 님에게 한 답변 기록이 없어요.", ephemeral=True)
        return
    lines = [
        f"**{user.display_name}** 님에게 쓴 토큰",
        f"```총 {fmt(p + c)} 토큰 (입력 {fmt(p)} / 출력 {fmt(c)})",
        f"답변 {fmt(n)}회 | {kst(first)} ~ {kst(last)}```",
        "**최근 답변 내역**",
    ]
    for ts, kind, char, ch, pt, ct, snip in d["recent"]:
        where = f"<#{ch}>" if ch else "-"
        lines.append(f"`{kst(ts)}` {where} [{char}/{kind}] {fmt(pt + ct)}토큰\n> {trunc(snip or '', 90)}")
    await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)


# ---- 3. 캐릭터스왑 ----
async def character_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower()
    return [
        app_commands.Choice(name=f"{name} ({cid})", value=cid)
        for cid, name in Persona.available()
        if cur in cid.lower() or cur in name.lower()
    ][:25]


@bot.tree.command(name="캐릭터스왑", description="봇의 캐릭터를 바꿔요")
@app_commands.describe(character="바꿀 캐릭터", clear_history="이전 캐릭터와의 대화 기록을 지울지 (기본: 지움)",
                       greet="바꾼 뒤 이 채널에 캐릭터 첫 인사를 보낼지 (기본: 보냄)")
@app_commands.rename(character="캐릭터", clear_history="기록초기화", greet="인사")
@app_commands.autocomplete(character=character_autocomplete)
async def swap_character(
    interaction: discord.Interaction, character: str, clear_history: bool = True, greet: bool = True
) -> None:
    ids = {cid for cid, _ in Persona.available()}
    if character not in ids:
        await interaction.response.send_message(
            "없는 캐릭터예요. 목록: " + ", ".join(sorted(ids)), ephemeral=True)
        return
    persona.load(character)
    await memory.kv_set("character", persona.char_id)
    if clear_history:
        await memory.clear_all_history()
    bot.pending.clear()
    await interaction.response.send_message(
        f"캐릭터를 **{persona.name}** (`{persona.char_id}`)(으)로 바꿨어요."
        + (" 대화 기록을 초기화했어요." if clear_history else ""), ephemeral=True)
    if greet and persona.greeting and interaction.channel:
        await interaction.channel.send(persona.greeting)


# ---- 4. 상태 ----
@bot.tree.command(name="상태", description="봇 현재 상태를 확인해요")
async def status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        loaded = await llm.loaded_models()
        gpu = "\n".join(
            f"  {m}: {s / 1e9:.1f}GB (VRAM {v / 1e9:.1f}GB)" for m, s, v in loaded
        ) or "  (현재 로드된 모델 없음)"
    except Exception as e:  # noqa: BLE001
        gpu = f"  Ollama 연결 실패: {e}"
    total_msgs, n_ch = await memory.message_counts()
    ch = " ".join(f"<#{c}>" for c in sorted(bot.channels)) or "(없음 — 멘션/DM만 반응)"
    pend = sum(len(v) for v in bot.pending.values())
    await interaction.followup.send(
        f"**봇 상태**\n"
        f"- 캐릭터: **{persona.name}** (`{persona.char_id}`)\n"
        f"- 모델: `{llm.model}`\n"
        f"- 상태: {'⏸ 일시정지' if bot.paused else '▶ 동작 중'}\n"
        f"- 쿨다운: {bot.cooldown:g}초 (+최대 {int(gcfg['cooldown_jitter'] * 100)}% 랜덤)\n"
        f"- 대화 채널: {ch}\n"
        f"- 대기 중 메시지: {pend}개 | 저장된 기록 {fmt(total_msgs)}개 ({n_ch}개 채널)\n"
        f"- 가동 시간: {fmt_uptime(time.time() - bot.started_at)} | 디스코드 지연 {bot.latency * 1000:.0f}ms\n"
        f"- Ollama 로드된 모델:\n{gpu}",
        ephemeral=True)


# ---- 5. 일시정지 ----
@bot.tree.command(name="일시정지", description="봇의 대답을 일시정지/재개해요 (실행할 때마다 전환)")
async def pause(interaction: discord.Interaction) -> None:
    bot.paused = not bot.paused
    await memory.kv_set("paused", "1" if bot.paused else "0")
    if bot.paused:
        bot.pending.clear()
    await interaction.response.send_message(
        "⏸ 일시정지했어요. 이제 대답하지 않아요." if bot.paused else "▶ 다시 대화에 참여해요.", ephemeral=True)


# ---- 6. 쿨다운 ----
@bot.tree.command(name="쿨다운", description="대답 후 다음 대답까지 기다리는 시간(초)을 설정해요")
@app_commands.describe(seconds="0~600초 (생략하면 현재 값 확인)")
@app_commands.rename(seconds="초")
async def set_cooldown(interaction: discord.Interaction, seconds: Optional[app_commands.Range[int, 0, 600]] = None) -> None:
    if seconds is None:
        await interaction.response.send_message(f"현재 쿨다운: {bot.cooldown:g}초", ephemeral=True)
        return
    bot.cooldown = float(seconds)
    await memory.kv_set("cooldown", str(seconds))
    await interaction.response.send_message(f"쿨다운을 **{seconds}초**로 바꿨어요.", ephemeral=True)


# ---- 7. 채널 ----
@bot.tree.command(name="채널", description="봇이 대화에 참여할 채널을 관리해요")
@app_commands.describe(action="추가/제거/목록", channel="대상 채널 (생략하면 현재 채널)")
@app_commands.rename(action="동작", channel="채널")
@app_commands.choices(action=[
    app_commands.Choice(name="추가", value="add"),
    app_commands.Choice(name="제거", value="remove"),
    app_commands.Choice(name="목록", value="list"),
])
async def manage_channel(
    interaction: discord.Interaction, action: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None
) -> None:
    target = channel or interaction.channel
    if action.value == "list":
        txt = " ".join(f"<#{c}>" for c in sorted(bot.channels)) or "(없음)"
        await interaction.response.send_message(f"대화 참여 채널: {txt}", ephemeral=True)
        return
    if target is None:
        await interaction.response.send_message("채널을 지정해주세요.", ephemeral=True)
        return
    if action.value == "add":
        bot.channels.add(target.id)
        msg = f"<#{target.id}> 채널에 참여해요."
    else:
        bot.channels.discard(target.id)
        bot.pending.pop(target.id, None)
        msg = f"<#{target.id}> 채널에서 빠져요."
    await bot.save_channels()
    await interaction.response.send_message(msg, ephemeral=True)


# ---- 8. 기억 ----
@bot.tree.command(name="기억", description="특정 유저에 대한 기억을 보거나 초기화해요")
@app_commands.describe(action="보기/초기화", user="대상 유저")
@app_commands.rename(action="동작", user="유저")
@app_commands.choices(action=[
    app_commands.Choice(name="보기", value="show"),
    app_commands.Choice(name="초기화", value="reset"),
])
async def manage_memory(
    interaction: discord.Interaction, action: app_commands.Choice[str], user: discord.User
) -> None:
    if action.value == "reset":
        await memory.reset_user(user.id)
        await interaction.response.send_message(
            f"{user.display_name} 님의 기억과 대화 기록을 지웠어요.", ephemeral=True)
        return
    facts = await memory.get_facts(user.id, 40)
    body = "\n".join(f"• {f}" for f in facts) if facts else "아직 기억하는 게 없어요."
    await interaction.response.send_message(
        f"**{persona.name}이(가) 기억하는 {user.display_name}**\n{body}"[:1990], ephemeral=True)


# ---- 9. 리로드 ----
@bot.tree.command(name="리로드", description="캐릭터/공통 규칙 yaml 파일을 다시 불러와요")
async def reload_persona(interaction: discord.Interaction) -> None:
    persona.reload()
    await interaction.response.send_message(
        f"다시 불러왔어요. 현재 캐릭터: **{persona.name}** (`{persona.char_id}`)", ephemeral=True)


def run() -> None:
    bot.run(discord_token(), log_handler=None)


if __name__ == "__main__":
    run()
