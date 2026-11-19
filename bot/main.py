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
from .mood import EMO, EMOTIONS
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


icfg = settings["idle"]


@dataclass
class Pending:
    message: discord.Message
    ts: float          # time.monotonic()
    priority: bool     # 멘션 또는 봇 메시지에 대한 답장
    text: str | None = None   # 같은 사람이 연달아 보낸 메시지를 묶은 텍스트


@dataclass
class IdleState:
    """채널별 '아무도 말을 안 할 때 혼자 말 거는' 상태"""
    last_activity: float          # 마지막으로 사람이 말한 시각 (time.time())
    next_at: float                # 다음 이벤트가 가능한 시각
    stage: int = 0                # 답 없이 지나간 이벤트 횟수 (침묵 이벤트 포함)
    sent: int = 0                 # 그중 실제로 보낸 메시지 수
    day: str = ""
    day_count: int = 0


def rand_minutes(rng: list) -> float:
    return random.uniform(float(rng[0]), float(rng[1])) * 60


# 단계별 이벤트: (이름, 가중치, 지시문). silent 는 메시지를 보내지 않고 삐진 채 넘어간다.
IDLE_EVENTS: dict[int, list[tuple[str, float, str]]] = {
    0: [
        ("start", 1.0,
         "조용한 채널에 자연스럽게 말을 꺼내라. 심심하다는 투정, 방금 한 일이나 본 것, 갑자기 떠오른 생각, "
         "누군가의 근황이 궁금하다는 말 등 아무 화제나 좋다. 기억하는 사람 이름을 불러도 된다."),
    ],
    1: [
        ("nag", 3.0, "아까 말을 꺼냈는데 반응이 없었다. 가볍게 다시 불러본다. (예: 다들 어디 갔는지 궁금해하기, 누가 있는지 확인하기)"),
        ("sulk", 3.0, "아무도 답하지 않아서 살짝 서운하다. 과하지 않게, 캐릭터답게 서운함을 표현한다."),
        ("self_talk", 2.0, "답이 없으니 혼잣말을 한다. 지금 혼자 뭘 하고 있는지 중얼거리는 식으로."),
        ("silent", 2.0, ""),
    ],
    2: [
        ("sulk_hard", 3.0, "계속 아무도 답하지 않아서 진짜로 삐졌거나 체념한 상태다. 아주 짧게, 토라진 말투로."),
        ("self_talk", 3.0, "이제 포기하고 혼자 논다. 혼자 노는 이야기를 무심하게 한마디."),
        ("silent", 3.0, ""),
        ("bye", 1.0, "아무도 없으니 자러 가거나 잠시 자리를 비우겠다고 짧게 선언한다."),
    ],
}


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
        self.typing_until: dict[tuple[int, int], float] = {}   # (채널, 유저) -> 입력 중 표시가 끝나는 시각
        self.dm_buffer: dict[int, list[str]] = {}
        self.dm_tasks: dict[int, asyncio.Task] = {}
        self.dm_generating: set[int] = set()
        self.idle: dict[int, IdleState] = {}
        self.return_note: dict[int, str] = {}    # 사람들이 돌아왔을 때 다음 대답에 넣을 상황 메모
        # 런타임 상태 (DB kv 에 저장되어 재시작 후에도 유지)
        self.paused = False
        self.idle_enabled = bool(icfg["enabled"])
        self.cooldown = float(gcfg["cooldown_sec"])
        self.channels: set[int] = {int(c) for c in dcfg["channel_ids"]}

    async def setup_hook(self) -> None:
        await memory.init()
        await self.load_state()
        await engine.load_names()
        await self.tree.sync()
        group_loop.start()
        idle_loop.start()
        if pcfg["enabled"]:
            proactive_loop.change_interval(minutes=pcfg["check_interval_min"])
            proactive_loop.start()

    async def load_state(self) -> None:
        self.paused = (await memory.kv_get("paused", "0")) == "1"
        ie = await memory.kv_get("idle_enabled")
        if ie is not None:
            self.idle_enabled = ie == "1"
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
    for m in message.mentions:          # 다른 사람 멘션은 진짜 이름으로 바꿔서 보여준다
        if bot.user and m.id == bot.user.id:
            continue
        text = text.replace(f"@{m.display_name}", engine.label(m.id, m.display_name))
    ref = message.reference
    if text and ref and isinstance(ref.resolved, discord.Message):
        target = ref.resolved
        if bot.user and target.author.id == bot.user.id:
            who = persona.name
        else:
            who = engine.label(target.author.id, target.author.display_name)
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
    # 기분에 따라 쿨다운이 달라진다 (신나면 짧게, 졸리면 길게)
    return bot.cooldown * engine.mood.cooldown_mult * (1 + random.random() * gcfg["cooldown_jitter"])


def is_directed(text: str) -> bool:
    """멘션이 아니어도 캐릭터 이름/봇 이름을 불렀으면 봇에게 하는 말로 본다."""
    names = {persona.name, persona.nickname}
    if bot.user:
        names.add(bot.user.display_name)
    return any(n and n in text for n in names)


@bot.event
async def on_ready() -> None:
    log.info("로그인: %s | 모델: %s | 캐릭터: %s", bot.user, llm.model, persona.name)


@bot.event
async def on_typing(channel: discord.abc.Messageable, user: discord.abc.User, when: datetime) -> None:
    """누가 입력 중인지 기록해서, 말을 끝낼 때까지 기다리는 데 쓴다. (디스코드 입력 중 표시는 약 10초 지속)"""
    if not user.bot:
        cid = getattr(channel, "id", 0)
        bot.typing_until[(cid, user.id)] = time.monotonic() + 8


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
    mark_human_activity(message.channel.id)
    engine.mood.observe(text, directed=mentioned or is_directed(text))
    if bot.paused:
        return
    bot.pending.setdefault(message.channel.id, []).append(
        Pending(message, time.monotonic(), priority=mentioned)
    )


def mark_human_activity(channel_id: int) -> None:
    """사람이 말하면 혼자말 걸기 상태를 리셋하고, 무시당했던 경험이 있으면 다음 대답에 반영한다."""
    st = bot.idle.get(channel_id)
    now = time.time()
    if st is None:
        bot.idle[channel_id] = IdleState(now, now + rand_minutes(icfg["first_after_min"]))
        return
    if st.sent > 0:
        n = st.sent
        stage = st.stage
        engine.mood.on_return(n)
        bot.return_note[channel_id] = (
            f"네가 먼저 말을 꺼냈는데 {n}번이나 아무도 답하지 않았고, 한참 뒤에야 사람들이 돌아왔다"
            + (f" (그 사이 단계: {stage}). " if stage > n else ". ")
            + "서운했다면 서운한 티를 내거나, 반가워하거나, 아무 일 없던 척하는 등 캐릭터답게 자연스럽게 반응해도 좋다."
        )
    st.last_activity = now
    st.stage = 0
    st.sent = 0
    st.next_at = now + rand_minutes(icfg["first_after_min"])


async def handle_dm(message: discord.Message, text: str) -> None:
    """DM: 연달아 온 메시지를 잠깐 기다렸다가 한 번에 묶어서 처리한다."""
    uid = message.author.id
    await memory.set_dm_channel(uid, message.channel.id)
    await memory.add_message(message.channel.id, uid, message.author.display_name, "user", text)
    engine.mood.observe(text, directed=True)
    bot.dm_buffer.setdefault(uid, []).append(text)
    t = bot.dm_tasks.get(uid)
    if t and not t.done() and uid not in bot.dm_generating:
        t.cancel()                    # 아직 기다리는 중이면 새 메시지까지 포함해서 다시 기다린다
    bot.dm_tasks[uid] = asyncio.create_task(dm_worker(message.channel, message.author))


async def dm_worker(channel: discord.DMChannel, author: discord.abc.User) -> None:
    uid = author.id
    try:
        start = time.monotonic()
        await asyncio.sleep(gcfg["settle_sec"])
        # 아직 입력 중이면 조금 더 기다린다 (최대 max_settle_wait_sec)
        while (bot.typing_until.get((channel.id, uid), 0) > time.monotonic()
               and time.monotonic() - start < gcfg["max_settle_wait_sec"]):
            await asyncio.sleep(1)
        lo, hi = settings["typing"]["read_delay"]
        await asyncio.sleep(random.uniform(lo, hi))
        async with bot.dm_locks.setdefault(uid, asyncio.Lock()):
            texts = bot.dm_buffer.pop(uid, [])
            if not texts:
                return
            bot.dm_generating.add(uid)
            try:
                async with bot.gpu_lock:
                    async with channel.typing():
                        bubbles = await engine.reply(
                            channel.id, uid, author.display_name, "\n".join(texts), dm=True)
                if bubbles:
                    await send_bubbles(channel, bubbles)
            finally:
                bot.dm_generating.discard(uid)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("DM 응답 실패 (uid=%s)", uid)


async def respond_group(channel_id: int, target: Pending) -> None:
    msg = target.message
    channel = msg.channel
    bot.busy.add(channel_id)
    try:
        async with bot.gpu_lock:
            async with channel.typing():
                note = bot.return_note.get(channel_id)
                bubbles = await engine.reply(
                    channel_id, msg.author.id, msg.author.display_name, target.text or clean_text(msg),
                    # 멘션/답장이 아니고, 돌아온 상황 메모도 없을 때만 SKIP 가능
                    can_skip=not target.priority and not note,
                    priority=target.priority,
                    extra_note=note,
                )
                if bubbles and note:
                    bot.return_note.pop(channel_id, None)
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

        # 1) 같은 사람이 연달아 쓰는 중이면 끝날 때까지 기다린다 (마지막 메시지 후 settle_sec, 입력 중이면 더)
        latest: dict[int, float] = {}
        for p in items:
            aid = p.message.author.id
            latest[aid] = max(latest.get(aid, 0), p.ts)

        def settled(p: Pending) -> bool:
            aid = p.message.author.id
            quiet = now - latest[aid]
            if quiet < gcfg["settle_sec"]:
                return False
            typing = bot.typing_until.get((channel_id, aid), 0) > now
            return not typing or quiet >= gcfg["max_settle_wait_sec"]

        ready = [p for p in items if settled(p)]
        if not ready:
            continue

        prio = [p for p in ready if p.priority]
        cooling = now < bot.next_ok.get(channel_id, 0)
        if cooling and not (prio and gcfg["mention_bypass_cooldown"]):
            continue
        if prio:
            target = prio[-1]
        else:
            if random.random() > min(1.0, gcfg["reply_chance"] * engine.mood.reply_mult):
                items[:] = [p for p in items if p not in ready]   # 이번 메시지들은 그냥 넘어간다
                continue
            target = random.choice(ready)

        # 2) 고른 사람이 연달아 보낸 메시지는 하나로 묶어서 대답한다
        same = [p for p in ready if p.message.author.id == target.message.author.id]
        merged = Pending(
            message=same[-1].message, ts=same[-1].ts,
            priority=any(p.priority for p in same),
            text="\n".join(clean_text(p.message) for p in same),
        )
        items[:] = [p for p in items if p not in ready]   # 나머지는 대화 기록으로 맥락에만 반영
        asyncio.create_task(respond_group(channel_id, merged))


@group_loop.before_loop
async def _group_ready() -> None:
    await bot.wait_until_ready()


# ------------------ 채널이 조용할 때 혼자 말 걸기 ------------------
def pick_event(stage: int) -> tuple[str, str]:
    table = IDLE_EVENTS[min(stage, 2)]
    name, _, instr = random.choices(table, weights=[w for _, w, _ in table], k=1)[0]
    return name, instr


async def run_idle_event(channel_id: int, st: IdleState, force: bool = False) -> str:
    """이벤트 하나를 실행하고 결과 설명을 돌려준다."""
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return "채널을 찾을 수 없음"
    now = time.time()
    name, instr = pick_event(st.stage)
    growth = float(icfg["followup_growth"]) ** st.stage

    if name == "silent":
        # 서운해서 일부러 말을 안 한다 (자기도 계속 답장 안 침). 단계만 올라간다.
        engine.mood.on_ignored(st.stage, "sulk")
        st.stage += 1
        st.next_at = now + rand_minutes(icfg["followup_after_min"]) * growth
        return "silent (이번엔 아무 말도 하지 않음)"

    idle_min = int((now - st.last_activity) // 60)
    async with bot.gpu_lock:
        async with channel.typing():
            bubbles = await engine.initiate(channel_id, instr, idle_min, st.sent)
    if not bubbles:
        st.next_at = now + 600
        return "생성 결과 없음"
    await send_bubbles(channel, bubbles)
    if st.stage >= 1:                       # 앞서 보낸 말을 무시당한 뒤라서 외로움/서운함이 쌓인다
        engine.mood.on_ignored(st.stage, name)
    st.stage += 1
    st.sent += 1
    st.day_count += 1
    st.next_at = time.time() + rand_minutes(icfg["followup_after_min"]) * growth
    return f"{name} 이벤트 전송"


@tasks.loop(seconds=60)
async def idle_loop() -> None:
    """아무도 말하지 않으면 먼저 말을 꺼내고, 답이 없으면 서운해하거나 혼자 놀거나 입을 닫는 등 단계별로 반응한다."""
    if bot.paused or not bot.idle_enabled:
        return
    now_dt = engine.now()
    lo, hi = icfg["active_hours"]
    if not (lo <= now_dt.hour < hi):
        return
    today = now_dt.strftime("%Y-%m-%d")
    now = time.time()

    for ch_id in list(bot.channels):
        st = bot.idle.get(ch_id)
        if st is None:
            last = await memory.last_message_ts(ch_id) or now
            st = bot.idle[ch_id] = IdleState(last, max(now, last) + rand_minutes(icfg["first_after_min"]))
        if st.day != today:
            st.day, st.day_count = today, 0
        if now < st.next_at or ch_id in bot.busy or bot.pending.get(ch_id):
            continue
        if st.stage >= icfg["max_unanswered"] or st.day_count >= icfg["max_per_day"]:
            continue
        bot.busy.add(ch_id)
        try:
            result = await run_idle_event(ch_id, st)
            log.info("혼잣말 이벤트 (channel=%s, stage=%s): %s", ch_id, st.stage, result)
        except Exception:  # noqa: BLE001
            log.exception("혼잣말 이벤트 실패 (channel=%s)", ch_id)
            st.next_at = time.time() + 600
        finally:
            bot.busy.discard(ch_id)


@idle_loop.before_loop
async def _idle_ready() -> None:
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
        names = {"reply": "일반 답변", "memory": "기억 추출", "proactive": "DM 먼저", "idle": "혼잣말"}
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
    engine.mood.reset()                 # 새 캐릭터의 기준 기분으로 시작
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
        f"- 기분: {engine.mood.current().label}\n"
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


# ---- 9. 이름 (닉네임 대신 쓸 진짜 이름 등록) ----
@bot.tree.command(name="이름", description="유저의 진짜 이름(호칭)을 등록해요. 봇은 닉네임 대신 이 이름으로 불러요")
@app_commands.describe(action="등록/삭제/목록", user="대상 유저 (등록/삭제 시)", name="진짜 이름 (예: 민수)",
                       call="부를 때 쓸 호칭 (예: 민수야, 민수형). 생략하면 이름 그대로")
@app_commands.rename(action="동작", user="유저", name="이름", call="호칭")
@app_commands.choices(action=[
    app_commands.Choice(name="등록", value="set"),
    app_commands.Choice(name="삭제", value="del"),
    app_commands.Choice(name="목록", value="list"),
])
async def manage_names(
    interaction: discord.Interaction, action: app_commands.Choice[str],
    user: Optional[discord.User] = None, name: Optional[str] = None, call: Optional[str] = None,
) -> None:
    if action.value == "list":
        if not engine.names:
            await interaction.response.send_message("등록된 이름이 없어요.", ephemeral=True)
            return
        body = "\n".join(
            f"<@{uid}> → {real}" + (f" (호칭: {c})" if c else "") for uid, (real, c) in engine.names.items()
        )
        await interaction.response.send_message(f"**등록된 이름**\n{body}"[:1990], ephemeral=True)
        return
    if user is None:
        await interaction.response.send_message("유저를 지정해주세요.", ephemeral=True)
        return
    if action.value == "del":
        removed = await memory.del_real_name(user.id)
        engine.names.pop(user.id, None)
        await interaction.response.send_message(
            "이름 등록을 지웠어요." if removed else "등록된 이름이 없어요.", ephemeral=True)
        return
    if not name or not name.strip():
        await interaction.response.send_message("등록할 이름을 입력해주세요.", ephemeral=True)
        return
    real = name.strip()[:30]
    call_name = call.strip()[:30] if call and call.strip() else None
    await memory.set_real_name(user.id, real, call_name)
    engine.names[user.id] = (real, call_name)
    await interaction.response.send_message(
        f"{user.display_name} → **{real}**" + (f" (호칭: {call_name})" if call_name else "") + " 로 등록했어요.",
        ephemeral=True)


# ---- 10. 인식 (캐릭터가 그 사람을 어떻게 보는지) ----
@bot.tree.command(name="인식", description="캐릭터가 각 유저를 어떻게 생각하는지(인식)를 설정해요. 예: 대호 = 멍청한 개발자")
@app_commands.describe(action="설정/삭제/보기/목록", user="대상 유저",
                       view="캐릭터가 그 유저에 대해 가진 인식 (예: 멍청한 개발자. 버그만 만들고 허세 부림)")
@app_commands.rename(action="동작", user="유저", view="내용")
@app_commands.choices(action=[
    app_commands.Choice(name="설정", value="set"),
    app_commands.Choice(name="삭제", value="del"),
    app_commands.Choice(name="보기", value="show"),
    app_commands.Choice(name="목록", value="list"),
])
async def manage_views(
    interaction: discord.Interaction, action: app_commands.Choice[str],
    user: Optional[discord.User] = None, view: Optional[str] = None,
) -> None:
    if action.value == "list":
        if not engine.views:
            await interaction.response.send_message("설정된 인식이 없어요.", ephemeral=True)
            return
        def nm(uid: int) -> str:
            u = bot.get_user(uid)
            return engine.label(uid, u.display_name if u else str(uid))

        body = "\n".join(f"{nm(uid)} (<@{uid}>) = {v}" for uid, v in engine.views.items())
        await interaction.response.send_message(f"**{persona.name}의 인식**\n{body}"[:1990], ephemeral=True)
        return
    if user is None:
        await interaction.response.send_message("유저를 지정해주세요.", ephemeral=True)
        return
    who = engine.label(user.id, user.display_name)
    if action.value == "show":
        v = engine.views.get(user.id)
        await interaction.response.send_message(
            f"{who} = {v}" if v else f"{who}에 대한 인식이 설정돼 있지 않아요.", ephemeral=True)
        return
    if action.value == "del":
        removed = await memory.del_view(user.id)
        engine.views.pop(user.id, None)
        await interaction.response.send_message(
            f"{who}에 대한 인식을 지웠어요." if removed else "설정된 인식이 없어요.", ephemeral=True)
        return
    if not view or not view.strip():
        await interaction.response.send_message("인식 내용을 입력해주세요.", ephemeral=True)
        return
    text = view.strip()[:300]
    await memory.set_view(user.id, text)
    engine.views[user.id] = text
    await interaction.response.send_message(f"**{who}** = {text}\n(이제부터 이 인식대로 대해요)", ephemeral=True)


# ---- 기분/상태 ----
async def emotion_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower()
    return [
        app_commands.Choice(name=f"{e.label} ({e.id})", value=e.id)
        for e in EMOTIONS if cur in e.id or cur in e.label
    ][:25]


def bar(value: int, lo: int = 0, hi: int = 100, width: int = 10) -> str:
    n = round((value - lo) / (hi - lo) * width)
    n = max(0, min(width, n))
    return "█" * n + "░" * (width - n)


@bot.tree.command(name="기분", description="캐릭터의 기분/상태 점수를 확인하거나 감정을 바꿔요")
@app_commands.describe(action="보기/설정/초기화/목록", emotion="설정할 감정 (설정 시)", minutes="유지 시간(분, 기본 30)")
@app_commands.rename(action="동작", emotion="감정", minutes="분")
@app_commands.choices(action=[
    app_commands.Choice(name="보기", value="show"),
    app_commands.Choice(name="설정", value="set"),
    app_commands.Choice(name="초기화", value="reset"),
    app_commands.Choice(name="목록", value="list"),
])
@app_commands.autocomplete(emotion=emotion_autocomplete)
async def mood_cmd(
    interaction: discord.Interaction, action: app_commands.Choice[str],
    emotion: Optional[str] = None, minutes: Optional[app_commands.Range[int, 1, 720]] = None,
) -> None:
    m = engine.mood
    if action.value == "list":
        score = " ".join(e.label for e in EMOTIONS if not e.special)
        event = " ".join(e.label for e in EMOTIONS if e.special)
        await interaction.response.send_message(
            f"**감정 30종**\n점수로 정해지는 감정 ({sum(1 for e in EMOTIONS if not e.special)}): {score}\n"
            f"대화 사건으로 발동하는 감정 ({sum(1 for e in EMOTIONS if e.special)}): {event}", ephemeral=True)
        return
    if action.value == "reset":
        m.reset()
        await interaction.response.send_message("기분을 기준값으로 되돌렸어요.", ephemeral=True)
        return
    if action.value == "set":
        if not emotion or emotion not in EMO:
            await interaction.response.send_message(
                "감정을 골라주세요. (`/기분 동작:목록` 으로 확인)", ephemeral=True)
            return
        m.set_emotion(emotion, minutes or 30)
        await interaction.response.send_message(
            f"감정을 **{EMO[emotion].label}**(으)로 {minutes or 30}분 동안 설정했어요.", ephemeral=True)
        return
    snap = m.snapshot()
    e, sc = snap["emotion"], snap["scores"]
    lines = [
        f"**{persona.name}의 기분: {e.label}** (강도 {int(snap['intensity'] * 100)}%)"
        + (f" · 고정 {snap['override_min']}분 남음" if snap["override_min"] else ""),
        "```",
        f"기분   {bar(sc['mood'], -100, 100)} {sc['mood']:+d}",
        f"에너지 {bar(sc['energy'])} {sc['energy']}",
        f"짜증   {bar(sc['irritation'])} {sc['irritation']}",
        f"외로움 {bar(sc['lonely'])} {sc['lonely']}",
        f"흥미   {bar(sc['interest'])} {sc['interest']}",
        "```",
        f"말투: {e.tone}",
        f"-# 타이핑 속도 x{e.speed:g} · 끼어들기 x{e.reply:g} · 쿨다운 x{e.cooldown:g}",
    ]
    await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)


# ---- 11. 혼잣말 (조용할 때 먼저 말 걸기) ----
@bot.tree.command(name="혼잣말", description="채널이 조용할 때 혼자 말 거는 기능을 켜고 끄거나, 지금 바로 시험해요")
@app_commands.describe(action="켜기/끄기/지금/상태", channel="'지금'을 실행할 채널 (생략하면 현재 채널)")
@app_commands.rename(action="동작", channel="채널")
@app_commands.choices(action=[
    app_commands.Choice(name="켜기", value="on"),
    app_commands.Choice(name="끄기", value="off"),
    app_commands.Choice(name="지금", value="now"),
    app_commands.Choice(name="상태", value="status"),
])
async def idle_cmd(
    interaction: discord.Interaction, action: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None
) -> None:
    if action.value in ("on", "off"):
        bot.idle_enabled = action.value == "on"
        await memory.kv_set("idle_enabled", "1" if bot.idle_enabled else "0")
        await interaction.response.send_message(
            "혼잣말 기능을 켰어요." if bot.idle_enabled else "혼잣말 기능을 껐어요.", ephemeral=True)
        return
    if action.value == "status":
        now = time.time()
        lines = [f"혼잣말: {'켜짐' if bot.idle_enabled else '꺼짐'} (활동 시간 {icfg['active_hours'][0]}~{icfg['active_hours'][1]}시)"]
        for ch_id in sorted(bot.channels):
            st = bot.idle.get(ch_id)
            if not st:
                lines.append(f"<#{ch_id}> 아직 상태 없음")
                continue
            wait = max(0, int((st.next_at - now) // 60))
            lines.append(
                f"<#{ch_id}> 단계 {st.stage}/{icfg['max_unanswered']} · 보낸 메시지 {st.sent} · "
                f"오늘 {st.day_count}/{icfg['max_per_day']} · 다음 이벤트까지 약 {wait}분")
        await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)
        return
    # 지금: 시험용으로 이벤트를 즉시 한 번 실행
    target = channel or interaction.channel
    if not isinstance(target, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("텍스트 채널에서 실행해주세요.", ephemeral=True)
        return
    if target.id in bot.busy:
        await interaction.response.send_message("지금 이 채널에서 대답을 만드는 중이에요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    st = bot.idle.get(target.id) or IdleState(time.time() - 1800, time.time())
    bot.idle[target.id] = st
    bot.busy.add(target.id)
    try:
        result = await run_idle_event(target.id, st, force=True)
    finally:
        bot.busy.discard(target.id)
    await interaction.followup.send(f"실행 결과: {result}", ephemeral=True)


# ---- 12. 리로드 ----
@bot.tree.command(name="리로드", description="캐릭터/공통 규칙 yaml 파일을 다시 불러와요")
async def reload_persona(interaction: discord.Interaction) -> None:
    persona.reload()
    await interaction.response.send_message(
        f"다시 불러왔어요. 현재 캐릭터: **{persona.name}** (`{persona.char_id}`)", ephemeral=True)


def run() -> None:
    bot.run(discord_token(), log_handler=None)


if __name__ == "__main__":
    run()
