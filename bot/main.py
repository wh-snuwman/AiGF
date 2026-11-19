"""디스코드 봇 진입점."""
from __future__ import annotations

import asyncio
import logging
import random
import time

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
pcfg = settings["proactive"]

persona = Persona()
llm = LLM(settings["ollama"])
memory = Memory(settings["memory"]["db_path"], settings["memory"]["max_facts_per_user"])
engine = ChatEngine(settings, persona, llm, memory)

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True


class GirlBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.locks: dict[int, asyncio.Lock] = {}
        # 동시에 여러 유저가 말해도 GPU 는 하나이므로 한 번에 하나씩 생성
        self.gpu_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        await memory.init()
        await self.tree.sync()
        if pcfg["enabled"]:
            proactive_loop.change_interval(minutes=pcfg["check_interval_min"])
            proactive_loop.start()

    async def close(self) -> None:
        await memory.close()
        await super().close()


bot = GirlBot()


def is_admin(user_id: int) -> bool:
    return user_id in dcfg["admin_user_ids"]


def user_allowed(user_id: int) -> bool:
    allowed = dcfg["allowed_user_ids"]
    return not allowed or user_id in allowed


def should_respond(message: discord.Message) -> bool:
    if isinstance(message.channel, discord.DMChannel):
        return dcfg["respond_to_dm"]
    if message.channel.id in dcfg["channel_ids"]:
        return True
    if dcfg["respond_to_mention"] and bot.user in message.mentions:
        return True
    return False


async def send_bubbles(channel: discord.abc.Messageable, bubbles: list[str]) -> None:
    tcfg = settings["typing"]
    for i, text in enumerate(bubbles):
        if tcfg["enabled"]:
            async with channel.typing():
                await asyncio.sleep(engine.typing_delay(text))
        await channel.send(text)


@bot.event
async def on_ready() -> None:
    log.info("로그인: %s (모델: %s)", bot.user, settings["ollama"]["model"])


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not should_respond(message):
        return
    if not user_allowed(message.author.id):
        return

    text = message.clean_content.strip()
    if bot.user:
        text = text.replace(f"@{bot.user.display_name}", "").strip()
    if not text:
        return

    uid = message.author.id
    lock = bot.locks.setdefault(uid, asyncio.Lock())
    async with lock:
        if isinstance(message.channel, discord.DMChannel):
            await memory.set_dm_channel(uid, message.channel.id)

        first_time = await memory.message_count(uid) == 0
        # 메시지를 읽는 척 하는 지연
        lo, hi = settings["typing"]["read_delay"]
        await asyncio.sleep(random.uniform(lo, hi))

        try:
            async with bot.gpu_lock:
                async with message.channel.typing():
                    bubbles = await engine.reply(uid, message.author.display_name, text)
        except Exception:  # noqa: BLE001
            log.exception("응답 생성 실패")
            await message.channel.send("…미안, 잠깐 정신이 멍해졌어. 조금 있다가 다시 말 걸어줄래?")
            return

        if first_time:
            bubbles = [persona.greeting] + bubbles
        await send_bubbles(message.channel, bubbles)


# ------------------ 먼저 말 걸기 ------------------
@tasks.loop(minutes=10)
async def proactive_loop() -> None:
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

        lock = bot.locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            continue
        async with lock:
            try:
                user = bot.get_user(uid) or await bot.fetch_user(uid)
                channel = user.dm_channel or await user.create_dm()
                async with bot.gpu_lock:
                    bubbles = await engine.proactive(uid, user.display_name, idle_h)
                if bubbles:
                    await send_bubbles(channel, bubbles)
                    await memory.mark_proactive(uid, today, count + 1)
                    log.info("먼저 말 걸기: %s", uid)
            except Exception:  # noqa: BLE001
                log.exception("먼저 말 걸기 실패 (uid=%s)", uid)


@proactive_loop.before_loop
async def _wait_ready() -> None:
    await bot.wait_until_ready()


# ------------------ 슬래시 명령 ------------------
@bot.tree.command(name="기억보기", description="캐릭터가 나에 대해 기억하고 있는 것을 확인해요")
async def show_memory(interaction: discord.Interaction) -> None:
    facts = await memory.get_facts(interaction.user.id, 30)
    body = "\n".join(f"• {f}" for f in facts) if facts else "아직 기억하고 있는 게 없어요."
    await interaction.response.send_message(f"**{persona.name}의 기억**\n{body}", ephemeral=True)


@bot.tree.command(name="기억초기화", description="나와의 대화 기록과 기억을 모두 지워요")
async def reset_memory(interaction: discord.Interaction) -> None:
    await memory.reset_user(interaction.user.id)
    await interaction.response.send_message("대화 기록과 기억을 모두 지웠어요.", ephemeral=True)


@bot.tree.command(name="리로드", description="[관리자] 캐릭터/설정 파일 다시 불러오기")
async def reload_persona(interaction: discord.Interaction) -> None:
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("관리자만 사용할 수 있어요.", ephemeral=True)
        return
    persona.reload()
    await interaction.response.send_message(f"캐릭터 설정을 다시 불러왔어요. ({persona.name})", ephemeral=True)


def run() -> None:
    bot.run(discord_token(), log_handler=None)


if __name__ == "__main__":
    run()
