"""디스코드 봇 진입점: 서버 일원처럼 채팅 + 관리자 전용 슬래시 명령."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

from . import initiative, targeting, vision
from .chat import ChatEngine
from .guard import redact
from . import trace as tracelib
from .config import discord_token, load_settings
from .emoji import Emojis
from .fight import Fight
from .guilds import DM, Hub, Proxy, char_uid
from .llm import LLM, ModelMissing
from .web import ipv6_reachable
from .mood import EMO, EMOTIONS
from . import stats as statslib
from .mood import Mood
from .persona import Persona

settings = load_settings()
logging.basicConfig(
    level=getattr(logging, settings.get("log_level", "INFO")),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


class _RedactFilter(logging.Filter):
    """로그에 토큰/API 키 모양 문자열이 찍히면 가린다."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.exc_info and isinstance(record.exc_info[1], ModelMissing):
                note = str(record.exc_info[1])
                record.exc_info = None                          # 모델 없음은 해결 방법이 메시지에 있으니 긴 트레이스백은 생략
                record.msg, record.args = f"{record.getMessage()} → {note}", ()
            msg = record.getMessage()
            red = redact(msg)
            if red != msg:
                record.msg, record.args = red, ()
        except Exception:  # noqa: BLE001
            pass
        return True


for _h in logging.getLogger().handlers:
    _h.addFilter(_RedactFilter())
for _noisy in ("primp", "ddgs", "ddgs.ddgs", "httpcore", "hpack"):       # 검색 라이브러리가 요청마다 INFO 를 쏟아내서 로그가 묻힌다
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("bot")

dcfg = settings["discord"]
gcfg = settings["group"]
pcfg = settings["proactive"]
ADMIN_IDS: set[int] = {int(x) for x in dcfg["admin_user_ids"]}

llm = LLM(settings["ollama"])
emojis = Emojis()
ecfg = settings.get("emoji", {})
vcfg = settings.get("vision", {})
inicfg = settings.get("initiative", {})
# 서버 채널에서는 웹훅으로 캐릭터 이름과 아바타를 표시한다. 여러 캐릭터는 가끔 서로 대화한다.
mcfg = {"webhook": True, "webhook_name": "AIGF", "chain_chance": 0.45, "chain_decay": 0.5, "max_chain": 2,
        "chain_delay_sec": [2.5, 6.0], "human_window_sec": 600, **settings.get("multi", {})}

# 서버(길드)마다 따로 저장/동작한다 (bot/guilds.py). engine/memory/persona/ratio 는 '지금 처리 중인 서버'의 객체로 연결되는 프록시.
hub = Hub(settings, llm, defaults={
    "idle": bool(settings["idle"]["enabled"]), "init": bool(inicfg.get("enabled", True)),
    "cooldown": float(gcfg["cooldown_sec"]), "channel_ids": dcfg["channel_ids"]},
    data_dir=Path(settings["memory"]["db_path"]).parent / "guilds")       # data/guilds/<서버id>/
engine = Proxy(hub, "engine")
memory = Proxy(hub, "memory")
persona = Proxy(hub, "persona")
ratio = Proxy(hub, "ratio")

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
    uid: int = 0                                  # 말한 사람 (리액션이면 리액션 단 사람)
    uname: str = ""
    images: list = field(default_factory=list)    # 첨부 이미지(바이트)
    human_reply: bool = False                     # 다른 사람 메시지에 대한 답장인가
    kind: str = "msg"                             # msg / reaction
    bubbles: float = 1.0                          # 채팅 비율 제어가 정한 말풍선 가중치
    meta: dict = field(default_factory=dict)      # 대상 선정 과정(후보/점수) — 디버깅 기록용


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
    # 적극성이 높을수록 먼저 말 거는 간격이 짧아진다 (4.0 이면 약 1/2.6)
    scale = max(1.0, engine.mood.engage) ** 0.7
    return random.uniform(float(rng[0]), float(rng[1])) * 60 / scale


def settle_sec() -> float:
    """연달아 보낸 메시지를 묶기 위한 대기시간. 적극성이 높으면 더 빨리 반응한다."""
    return max(0.6, gcfg["settle_sec"] / max(1.0, engine.mood.engage) ** 0.5)


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

    async def _call(self, interaction: discord.Interaction) -> None:
        await super()._call(interaction)
        if interaction.type is discord.InteractionType.autocomplete or interaction.user.id not in ADMIN_IDS:
            return
        try:
            hub.sync_peers()          # 적극성/싸움 기본값/기록·웹 켜기 같은 서버 공통 설정을 같은 서버의 모든 캐릭터에 맞춘다
        except Exception:  # noqa: BLE001
            log.exception("서버 공통 설정 동기화 실패")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in ADMIN_IDS:
            gid = interaction.guild_id or DM
            await hub.ensure(gid, getattr(interaction.guild, "name", ""))
            hub.use(gid)                       # 이 명령은 이 서버의 데이터/설정만 바꾼다
            return True
        if interaction.type is discord.InteractionType.autocomplete:
            await interaction.response.autocomplete([])
        else:
            await interaction.response.send_message("관리자만 사용할 수 있어요.", ephemeral=True)
        return False


class GirlBot(discord.Client):
    def __init__(self) -> None:
        # 보안: 모델이 뭐라고 쓰든 @everyone/@here/역할 호출은 발송되지 않게 하고, 유저 멘션(싸움 호출)과 답장 알림만 허용한다
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions(
            everyone=False, roles=False, users=True, replied_user=True))
        self.tree = AdminTree(self)
        self.started_at = time.time()
        self.gpu_lock = ChatEngine.gpu_lock
        self.dm_locks: dict[int, asyncio.Lock] = {}
        self.pending: dict[int, list[Pending]] = {}
        self.next_ok: dict[int, float] = {}      # 채널별 다음 발언 가능 시각 (monotonic)
        self.busy: set[int] = set()
        self.model_warned = 0.0                       # 모델 없음 오류 로그를 1분에 한 번만 찍기 위한 시각
        self.typing_until: dict[tuple[int, int], float] = {}   # (채널, 유저) -> 입력 중 표시가 끝나는 시각
        self.dm_buffer: dict[int, list[str]] = {}
        self.dm_tasks: dict[int, asyncio.Task] = {}
        self.dm_generating: set[int] = set()
        self.idle: dict[int, IdleState] = {}
        self.return_note: dict[int, str] = {}    # 사람들이 돌아왔을 때 다음 대답에 넣을 상황 메모
        self.chan_events: dict[int, deque] = {}     # 채널별 (시각, 유저) — 인원 규모 계산용
        self.last_replied: dict[int, float] = {} # 유저별로 마지막 대답한 시각
        self.last_target: dict[int, int] = {}    # 채널별 직전에 대답한 상대
        self.last_bot_msg: dict[int, float] = {}         # 채널별 봇이 마지막으로 말한 시각
        self.last_speaker: dict[int, str] = {}           # 채널별 마지막 발언자 이름
        self.init_times: dict[int, deque] = {}           # 채널별 먼저 말한 시각들 (시간당 한도용)
        self.init_unanswered: dict[int, int] = {}        # 먼저 말했는데 아직 사람이 답하지 않은 횟수
        self.init_gap: dict[int, float] = {}             # 채널별 이번 능동 대화 쿨타임(랜덤)
        self.init_target: dict[int, tuple] = {}          # 채널별 마지막으로 먼저 말 건 대상 (uid, 시각)
        self.webhooks: dict[tuple[int, str], discord.Webhook] = {}   # (채널(부모) id, 캐릭터 id) → 이 봇이 만든 웹훅
        self.webhook_ver: dict[tuple[int, str], float] = {}          # 웹훅에 반영한 프로필 사진 버전 (파일 수정 시각)
        self.webhook_failed: dict[int, float] = {}       # 웹훅을 못 만든 채널 (잠시 뒤 다시 시도)
        self.our_webhooks: set[int] = set()              # 이 봇의 웹훅 id (캐릭터 메시지에 답장했는지 알아보는 데 사용)
        self.last_char: dict[int, str] = {}              # 채널별 마지막으로 말한 캐릭터
        self.last_human_ts: dict[int, float] = {}        # 채널별 사람이 마지막으로 말한 시각
        self.unaddressed: dict[int, list] = {}           # 대답 후보에서 탈락해 답을 못 받은 메시지 (uid, 이름, 글, 시각)

    # 런타임 상태: 서버마다 따로 (서버별 DB 의 kv 에 저장되어 재시작 후에도 유지). 지금 처리 중인 서버의 값을 읽고 쓴다.
    @property
    def paused(self) -> bool:
        return hub.ctx().state.paused

    @paused.setter
    def paused(self, v: bool) -> None:
        hub.ctx().state.paused = v

    @property
    def idle_enabled(self) -> bool:
        return hub.ctx().state.idle_enabled

    @idle_enabled.setter
    def idle_enabled(self, v: bool) -> None:
        hub.ctx().state.idle_enabled = v

    @property
    def init_enabled(self) -> bool:
        return hub.ctx().state.init_enabled

    @init_enabled.setter
    def init_enabled(self, v: bool) -> None:
        hub.ctx().state.init_enabled = v

    @property
    def cooldown(self) -> float:
        return hub.ctx().state.cooldown

    @cooldown.setter
    def cooldown(self, v: float) -> None:
        hub.ctx().state.cooldown = v

    @property
    def channels(self) -> set[int]:
        return hub.ctx().state.channels

    async def setup_hook(self) -> None:
        # 서버별 저장소(DB, 상태)는 로그인 후 on_ready 에서 서버마다 만든다 (채널이 어느 서버 소속인지 알아야 하므로)
        # 슬래시 명령 동기화도 on_ready 에서 서버별로 한다 (바로 반영되고, 옛 전역 명령과 중복 표시되지 않음)
        group_loop.start()
        idle_loop.start()
        fight_loop.start()
        initiative_loop.start()
        web_loop.start()
        if pcfg["enabled"]:
            proactive_loop.change_interval(minutes=pcfg["check_interval_min"])
            proactive_loop.start()

    async def save_channels(self) -> None:
        await hub.save_channels()

    async def close(self) -> None:
        await hub.close()
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
    for cid in at_chars(text):           # '@케이' → '케이' (모델에게는 이름을 부른 것으로 보이게)
        for n in set(char_names()[cid]) - {""}:
            text = re.sub(r"@\s?" + re.escape(n), n, text)
    for m in message.mentions:          # 다른 사람 멘션은 진짜 이름으로 바꿔서 보여준다
        if bot.user and m.id == bot.user.id:
            continue
        text = text.replace(f"@{m.display_name}", engine.label(m.id, m.display_name))
    if not text and ((bot.user and bot.user in message.mentions) or at_chars(message.content)):
        text = "(말없이 이름만 불렀다)"          # '@이름' 만 보낸 부름도 대답한다 (예전엔 빈 메시지로 보고 무시했다)
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


def char_names() -> dict[str, tuple[str, str]]:
    """현재 서버의 캐릭터 id → (이름, 별명)"""
    return {cid: (c.persona.name, c.persona.nickname) for cid, c in hub.ctx().chars.items()}


def is_our_webhook(msg: discord.Message) -> bool:
    """이 봇의 웹훅(캐릭터)이 보낸 메시지인가"""
    if not getattr(msg, "webhook_id", None):
        return False
    return msg.webhook_id in bot.our_webhooks or msg.author.name in {n for n, _ in char_names().values()}


def replied_char(message: discord.Message | None) -> str | None:
    """사용자가 어느 캐릭터의 메시지에 답장했는가 (웹훅 메시지의 표시 이름으로 판별)"""
    ref = getattr(message, "reference", None)
    if not ref or not isinstance(ref.resolved, discord.Message) or not is_our_webhook(ref.resolved):
        return None
    for cid, (name, nick) in char_names().items():
        if ref.resolved.author.name in (name, nick):
            return cid
    return None


def at_chars(text: str) -> list[str]:
    """'@케이' 처럼 글자로 쓴 캐릭터 멘션 (웹훅 캐릭터는 디스코드 멘션을 걸 수 없어서). 이름/별명 모두 인식, 띄어쓰기 허용."""
    out = []
    for cid, (name, nick) in char_names().items():
        for n in {name, nick} - {""}:
            if re.search(r"(?<![\w@])@\s?" + re.escape(n), text or ""):
                out.append(cid)
                break
    return out


def named_chars(text: str) -> list[str]:
    """글에서 이름(또는 별명)으로 불린 캐릭터들"""
    body = re.sub(r"^\(.*?에 답장\)\s*", "", text)
    return [cid for cid, (name, nick) in char_names().items() if (name and name in body) or (nick and nick in body)]


def active_fight(channel_id: int, uid: int):
    """이 유저와 싸우는 중인 캐릭터가 있으면 (캐릭터 id, Fight)"""
    for cid, c in hub.ctx().chars.items():
        f = c.engine.fights.get(channel_id, uid)
        if f is not None:
            return cid, f
    return None


def pick_speaker(channel_id: int, text: str, uid: int, message: discord.Message | None = None) -> str:
    """사람의 메시지에 어느 캐릭터가 대답할지: 싸우는 중인 캐릭터 > 답장한 캐릭터 > 이름이 불린 캐릭터 > 말 많은 정도로 가중 랜덤"""
    ctx = hub.ctx()
    if len(ctx.chars) == 1:
        return ctx.primary
    hit = active_fight(channel_id, uid)
    if hit:
        return hit[0]
    rc = replied_char(message)
    if rc in ctx.chars:
        return rc
    named = at_chars(message.content if message is not None else text) or named_chars(text)   # '@이름' 으로 부른 캐릭터가 최우선
    pool = named or list(ctx.chars)
    weights = []
    for cid in pool:
        c = ctx.chars[cid]
        w = float(c.persona.data.get("talk_weight", 1.0)) * max(0.2, c.engine.mood.reply_mult)
        if bot.last_char.get(channel_id) == cid and not named:
            w *= 0.55                                  # 같은 캐릭터만 계속 말하지 않게
        weights.append(max(0.05, w))
    return random.choices(pool, weights=weights, k=1)[0]


def pick_free_speaker(channel_id: int) -> str:
    """조용한 채널에서 먼저 말을 꺼낼 캐릭터"""
    ctx = hub.ctx()
    pool = list(ctx.chars)
    weights = [max(0.05, float(ctx.chars[c].persona.data.get("talk_weight", 1.0)))
               * (0.4 if bot.last_char.get(channel_id) == c else 1.0) for c in pool]
    return random.choices(pool, weights=weights, k=1)[0]


def is_priority(message: discord.Message) -> bool:
    """봇을 멘션했거나, 봇(또는 캐릭터)의 메시지에 답장한 경우"""
    if bot.user and bot.user in message.mentions:
        return True
    if at_chars(message.content):                        # '@케이' 글자 멘션도 멘션으로 본다
        return True
    ref = message.reference
    if ref and isinstance(ref.resolved, discord.Message):
        return ref.resolved.author.id == (bot.user.id if bot.user else 0) or is_our_webhook(ref.resolved)
    return False


def replies_to_human(message: discord.Message) -> bool:
    """봇(캐릭터)이 아닌 다른 사람의 메시지에 대한 답장인가"""
    ref = message.reference
    return bool(ref and isinstance(ref.resolved, discord.Message)
                and bot.user and ref.resolved.author.id != bot.user.id and not is_our_webhook(ref.resolved))


AVATAR_DIR = Path(__file__).resolve().parent.parent / "data" / "avatars"   # /캐릭터 동작:프로필 로 올린 프로필 사진 (git 에 안 올라감)


DEFAULT_AVATAR_DIR = Path(__file__).resolve().parent.parent / "config" / "characters" / "avatars"   # 저장소에 들어 있는 기본 사진


def avatar_file(cid: str) -> Path | None:
    """올린 사진(data/avatars) > 기본 사진(config/characters/avatars) 순서."""
    for d in (AVATAR_DIR, DEFAULT_AVATAR_DIR):
        p = d / f"{cid}.png"
        if p.is_file():
            return p
    return None


def avatar_version(cid: str) -> float:
    p = avatar_file(cid)
    return p.stat().st_mtime if p else 0.0


async def get_webhook(channel, cid: str | None = None) -> discord.Webhook | None:
    """채널의 캐릭터용 웹훅 (없으면 만든다). 캐릭터마다 웹훅을 따로 둬서 프로필 사진을 웹훅 자체에 저장한다
    (메시지마다 넘기는 이미지 주소는 만료될 수 있지만 웹훅 아바타는 디스코드에 계속 남는다). 권한이 없으면 None."""
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    pid = getattr(parent, "id", None)
    if pid is None:
        return None
    key = (pid, cid or "")
    wh = bot.webhooks.get(key)
    ver = avatar_version(cid) if cid else 0.0
    if wh is None:
        if time.time() - bot.webhook_failed.get(pid, 0) < 600:
            return None
        try:
            name = f"{mcfg['webhook_name']}-{cid}"[:80] if cid else mcfg["webhook_name"]
            hooks = await parent.webhooks()
            wh = next((h for h in hooks if h.name == name and h.user and bot.user and h.user.id == bot.user.id), None)
            if wh is None:
                p = avatar_file(cid) if cid else None
                wh = await parent.create_webhook(name=name, avatar=p.read_bytes() if p else None,
                                                 reason="캐릭터마다 다른 이름/아바타로 말하기")
                bot.webhook_ver[key] = ver
                log.info("캐릭터 웹훅 생성: 채널=%s 캐릭터=%s 프로필사진=%s", pid, cid, "있음" if ver else "없음")
            bot.webhooks[key] = wh
            bot.our_webhooks.add(wh.id)
        except (discord.Forbidden, discord.HTTPException, AttributeError, OSError) as e:
            bot.webhook_failed[pid] = time.time()
            log.warning("웹훅을 만들 수 없어요 (채널 %s): %s → 웹훅 관리 권한을 주세요. 그동안은 이름 앞붙임으로 말해요.", pid, e)
            return None
    if cid and bot.webhook_ver.get(key) != ver:                    # 예전에 사진 없이 만든 웹훅이거나 사진이 바뀜 → 웹훅 아바타를 맞춘다
        try:
            p = avatar_file(cid)
            wh = await wh.edit(avatar=p.read_bytes() if p else None, reason="캐릭터 프로필 적용")
            bot.webhooks[key] = wh
            bot.webhook_ver[key] = ver
            log.info("캐릭터 웹훅 프로필 적용: 채널=%s 캐릭터=%s (%s)", pid, cid, p.name if p else "사진 없음")
        except Exception as e:  # noqa: BLE001
            bot.webhook_ver[key] = ver                              # 같은 오류를 매 메시지마다 반복하지 않는다 (사진을 바꾸면 다시 시도)
            log.warning("웹훅 프로필 적용 실패 (채널 %s, %s): %s: %s", pid, cid, type(e).__name__, e)
    return wh


async def send_bubbles(
    channel: discord.abc.Messageable, bubbles: list[str], reply_to: discord.Message | None = None
) -> None:
    tcfg = settings["typing"]
    trace_id = engine.last_trace_id          # 방금 끝난 응답의 기록과 연결
    t_send = time.time()
    sent_ids: list[int] = []
    rendered: list[str] = []
    use_webhook = mcfg.get("webhook", True) and not isinstance(channel, discord.DMChannel)
    wh = await get_webhook(channel, persona.char_id) if use_webhook else None
    cname = persona.name
    avatar = None if avatar_file(persona.char_id) else (persona.data.get("avatar_url") or None)   # 올린 사진이 있으면 웹훅 자체 아바타를 쓴다
    if not avatar and not avatar_file(persona.char_id) and persona.char_id == hub.ctx().primary and bot.user:
        avatar = bot.user.display_avatar.url          # 아바타를 따로 안 정한 기본 캐릭터는 봇 계정 프로필 사진으로 (웹훅이어도 같은 얼굴)
    for i, raw in enumerate(bubbles):
        # 서버 커스텀 이모지 `:이름:` 을 실제 이모지로 바꿔서 보낸다
        text = emojis.render(raw) if ecfg.get("enabled", True) else raw
        rendered.append(text)
        if tcfg["enabled"]:
            # 첫 말풍선은 답을 만드는 동안 이미 '입력 중'이었으니 거의 바로 보내고, 이어지는 말풍선만 짧게 타이핑 연출
            delay = min(engine.typing_delay(raw), 0.4) if i == 0 else min(engine.typing_delay(raw), 1.5)
            async with channel.typing():
                await asyncio.sleep(delay)
        if wh is not None:
            try:
                kw: dict = {"content": text, "username": cname[:80], "wait": True,
                            "allowed_mentions": discord.AllowedMentions(everyone=False, roles=False, users=True)}
                if avatar:
                    kw["avatar_url"] = avatar
                if isinstance(channel, discord.Thread):
                    kw["thread"] = channel
                m_ = await wh.send(**kw)
                sent_ids.append(m_.id)
                continue
            except discord.HTTPException as e:
                log.warning("웹훅 전송 실패, 일반 메시지로 대체: %s", e)
                bot.webhooks.pop((getattr(getattr(channel, "parent", channel), "id", 0), persona.char_id), None)
        if i == 0 and reply_to is not None and not (bot.user and reply_to.author.id == bot.user.id) and not use_webhook:
            try:
                m_ = await reply_to.reply(text, mention_author=False)
                sent_ids.append(m_.id)
                continue
            except discord.HTTPException:
                pass
        if use_webhook and i == 0:
            text = f"**{cname}** {text}"                          # 웹훅을 쓸 수 없을 때는 누가 말하는지 이름을 앞에 붙인다
        m_ = await channel.send(text)
        sent_ids.append(m_.id)
    cid = getattr(channel, "id", None)
    engine.tracer.log("send", trace_id=trace_id, channel_id=cid, bubbles=bubbles, rendered=rendered,
                      message_ids=sent_ids, reply_to=reply_to.id if reply_to is not None else None,
                      sec=round(time.time() - t_send, 2))
    if cid is not None and not isinstance(channel, discord.DMChannel):
        ratio.record(cid, "b", len(bubbles))              # 채팅 비율 통계에 봇 메시지 수 기록
        bot.last_bot_msg[cid] = time.time()


def crowd_now(channel_id: int) -> tuple[int, int]:
    """최근 대화에 참여한 (사람 수, 메시지 수)"""
    return targeting.crowd(list(bot.chan_events.get(channel_id, ())), gcfg["crowd_window_sec"])


def next_cooldown(channel_id: int | None = None) -> float:
    """다음 발언까지의 쿨다운. 기분(신나면 짧게)과 대화 인원(많을수록 짧게)이 반영된다."""
    base = bot.cooldown * engine.mood.cooldown_mult * (1 + random.random() * gcfg["cooldown_jitter"])
    base /= max(0.5, engine.mood.engage)         # 적극성이 높을수록 쿨다운이 짧다
    if channel_id is not None:
        users, msgs = crowd_now(channel_id)
        base *= targeting.crowd_factor(users, msgs, gcfg["crowd_boost"], gcfg["crowd_min_ratio"])
        base *= ratio.adjust(channel_id).cooldown          # 봇이 너무 조용하면 짧게, 너무 말이 많으면 길게
    return max(1.0, base)


def is_directed(text: str) -> bool:
    """멘션이 아니어도 캐릭터 이름/봇 이름을 불렀으면 봇에게 하는 말로 본다."""
    names = {n for pair in char_names().values() for n in pair}
    if bot.user:
        names.add(bot.user.display_name)
    return any(n and n in text for n in names)


async def gpu_lines() -> list[tuple[str, float, float, float]]:
    """로드된 모델의 [(이름, 전체 GB, VRAM GB, VRAM 적재율 %)]"""
    out = []
    for name, size, vram in await llm.loaded_models():
        pct = 100.0 * vram / size if size else 100.0
        out.append((name, size / 1e9, vram / 1e9, pct))
    return out


async def check_ipv6() -> None:
    ok = await asyncio.to_thread(ipv6_reachable)
    if ok is False:
        log.warning("이 서버는 IPv6 로 나갈 수 없어요 → 인터넷 검색 엔진 일부가 간헐적으로 실패할 수 있어요(봇은 실패한 엔진을 쉬게 하고 다른 엔진으로 자동 전환합니다). "
                    "근본 해결: README '문제 해결'의 IPv6 안내")


async def check_model() -> bool:
    """시작할 때 Ollama 연결/모델 설치 여부를 확인하고, 문제가 있으면 해결 방법을 크게 알린다."""
    ok, note = await llm.check()
    if ok:
        (log.warning if "대신 씁니다" in note else log.info)("모델 점검: %s", note)
    else:
        log.error("=" * 70)
        log.error("모델 점검 실패 → 봇이 대답하지 못합니다.\n  %s", note)
        log.error("=" * 70)
    return ok


async def warmup_and_report() -> None:
    if not await check_model():
        return                                   # 모델이 없는데 웜업하면 오류만 쌓인다 (이후 호출 때 자동으로 다시 점검한다)
    seen: set[str] = set()
    for g in list(hub.ctxs.values()):             # 고정 프롬프트를 미리 계산해 KV 캐시에 올린다 (캐릭터가 다른 서버/캐릭터는 각각)
        for c in list(g.chars.values()):
            if c.persona.char_id not in seen:
                seen.add(c.persona.char_id)
                await llm.warmup(c.engine.warm_messages())
    await engine.corrector.warmup()               # 문법 교정용 작은 모델도 VRAM 에 올려 둔다 (없으면 알리고 자동으로 꺼짐)
    try:
        for name, size, vram, pct in await gpu_lines():
            if pct < 99:
                log.warning(
                    "⚠ %s: VRAM 적재율 %.0f%% (모델 %.1fGB 중 %.1fGB만 GPU) → 나머지가 CPU 에서 돌아서 매우 느립니다. "
                    "같은 GPU 를 쓰는 다른 프로그램(이미지 생성 서버 등)을 끄거나 num_ctx 를 줄이세요.",
                    name, pct, size, vram)
            else:
                log.info("%s: GPU 100%% 적재 (%.1fGB)", name, size)
    except Exception as e:  # noqa: BLE001
        log.warning("GPU 적재 상태 확인 실패: %s", e)


def apply_emojis(ctx) -> None:
    """서버별(캐릭터별) 엔진에 이모지 안내/선택기를 연결한다 (새 서버 저장소/캐릭터가 만들어질 때도 호출)."""
    for c in ctx.chars.values():
        if ecfg.get("enabled", True) and ecfg.get("custom", True):
            c.engine.emoji_hint = emojis.prompt_block(ecfg.get("max_custom_in_prompt", 40))
            c.engine.mood.emoji_picker = emojis.text_token        # 감정 표현에 서버 이모지를 섞는다
        else:
            c.engine.emoji_hint = ""


def setup_ctx(ctx) -> None:
    apply_emojis(ctx)
    for c in ctx.chars.values():
        # 대화가 바쁠 때는 기억 추출을 미룬다 (GPU 양보)
        c.engine.busy_check = lambda: bool(bot.busy) or any(bot.pending.values()) or bot.gpu_lock.locked()


hub.on_create = setup_ctx


def refresh_emojis() -> None:
    if ecfg.get("enabled", True) and ecfg.get("custom", True):
        emojis.refresh(bot.guilds)
        log.info("서버 커스텀 이모지 %d개 인식", len(emojis.custom))
    for c in hub.ctxs.values():
        apply_emojis(c)


async def maybe_react(msg: discord.Message, chance: float, text: str | None = None) -> bool:
    """대답 대신(또는 대답 없이) 기분에 맞는 이모지 리액션을 단다. LLM 을 쓰지 않는다."""
    if not ecfg.get("enabled", True) or random.random() > chance:
        return False
    pick = emojis.pick_reaction(text if text is not None else msg_text_plain(msg),
                                engine.mood.current().id, has_image=vision.has_image(msg))
    try:
        if isinstance(pick, tuple):
            name, eid, anim = pick
            await msg.add_reaction(discord.PartialEmoji(name=name, id=eid, animated=anim))
        else:
            await msg.add_reaction(pick)
        engine.tracer.log("react", channel_id=msg.channel.id, message_id=msg.id, user_id=msg.author.id,
                          user_name=msg.author.display_name, text=text if text is not None else msg_text_plain(msg),
                          emoji=str(pick if not isinstance(pick, tuple) else f":{pick[0]}:"), emotion=engine.mood.current().label)
        return True
    except discord.HTTPException:
        return False


def msg_text_plain(msg: discord.Message) -> str:
    return (msg.clean_content or "").strip()


_ALL_COMMANDS: list = []       # 전역에서 비우기 전에 보관해 두는 명령 목록 (새 서버에 들어갈 때 사용)
_cmds_synced = False
_synced_guilds: set[int] = set()


async def sync_commands() -> None:
    """슬래시 명령을 봇이 들어가 있는 모든 서버에 즉시 동기화한다.
    전역 등록은 반영이 늦고 옛 목록이 캐시되는 경우가 있어서, 서버별로 등록하고 전역 명령은 비워서 중복을 막는다."""
    global _cmds_synced
    if not _ALL_COMMANDS:
        _ALL_COMMANDS.extend(bot.tree.get_commands())
    try:
        for g in bot.guilds:
            if g.id in _synced_guilds:                 # 재접속(on_ready 반복) 때 다시 동기화하지 않는다 (호출 제한 방지)
                continue
            bot.tree.copy_global_to(guild=g)
            synced = await bot.tree.sync(guild=g)
            _synced_guilds.add(g.id)
            log.info("슬래시 명령 %d개 동기화: 서버 '%s' → %s", len(synced), g.name, ", ".join(c.name for c in synced))
        if not _cmds_synced:
            bot.tree.clear_commands(guild=None)       # 전역 명령(옛 버전 포함)을 비운다
            await bot.tree.sync()
        _cmds_synced = True
    except Exception:  # noqa: BLE001
        log.exception("슬래시 명령 동기화 실패 — 봇 초대 시 applications.commands 권한을 선택했는지 확인하세요")


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    """새 서버에 초대되면 그 서버의 저장소를 만들고 명령을 등록한다."""
    await hub.ensure(guild.id, guild.name)
    for c in _ALL_COMMANDS:
        bot.tree.add_command(c, guild=guild, override=True)
    try:
        await bot.tree.sync(guild=guild)
        _synced_guilds.add(guild.id)
    except Exception:  # noqa: BLE001
        log.exception("새 서버 명령 동기화 실패")


async def prepare_guilds() -> None:
    """서버마다 독립된 저장소를 만든다. 서버별 분리 이전의 단일 DB 가 있으면 처음 한 번만 서버별로 나눠 가진다."""
    def owner(cid: int):
        ch = bot.get_channel(cid)
        g = getattr(ch, "guild", None)
        return g.id if g is not None else None
    hub.owner_of = owner
    hub.is_dm = lambda cid: isinstance(bot.get_channel(cid), discord.DMChannel)
    legacy = Path(settings["memory"]["db_path"])
    first_run = legacy.exists() and not (hub.root.exists() and any(hub.root.iterdir()))   # 서버별 저장소가 아직 하나도 없을 때만
    hub.legacy_path, hub.legacy_open = legacy, bool(first_run)
    await hub.ensure(DM, "DM")
    for g in bot.guilds:
        await hub.ensure(g.id, g.name)
    await hub.finish_legacy()
    log.info("서버별 저장소: %s", ", ".join(f"{c.name}({'dm' if c.gid == DM else c.gid})" for c in hub.ctxs.values()))


@bot.event
async def on_ready() -> None:
    log.info("로그인: %s | 모델: %s", bot.user, llm.model)
    await prepare_guilds()
    await sync_commands()
    refresh_emojis()
    if settings.get("web", {}).get("allowed", True):
        asyncio.create_task(check_ipv6())      # IPv6 가 막힌 서버인지 알려 둔다 (검색 엔진 일부가 간헐적으로 실패하는 원인)
    asyncio.create_task(warmup_and_report())   # 첫 응답이 느리지 않도록 모델을 미리 올리고 GPU 적재 상태를 점검한다
    if not getattr(bot, "_caught_up", False):  # 재연결 때마다 하지 않고 처음 켜질 때 한 번만
        bot._caught_up = True
        asyncio.create_task(catch_up_missed())


async def catch_up_missed(minutes: int = 15) -> None:
    """봇이 꺼져 있던(재시작하던) 동안 봇을 멘션/답장한 메시지는 디스코드가 다시 보내 주지 않는다.
    켜질 때 등록 채널마다 최근 기록을 보고, 아직 봇이 대답하지 않은 마지막 멘션 하나만 처리 대기열에 넣는다."""
    since = discord.utils.utcnow() - timedelta(minutes=minutes)
    done: set[int] = set()
    for g in list(bot.guilds):
        hub.use(g.id)
        # 1) 처리 도중에 꺼져서 끝내지 못한 메시지(QUEUED/PROCESSING)를 DB 상태표에서 찾아 다시 대기열에 넣는다
        try:
            await memory.msg_cleanup(7)
            for mid, cid_, _uid, _att in await memory.msg_unfinished(minutes * 60):
                ch = bot.get_channel(cid_)
                try:
                    m = await ch.fetch_message(mid) if ch is not None else None
                except discord.HTTPException:
                    m = None
                if m is None:
                    await memory.msg_set(mid, "SKIPPED", "gone")
                    continue
                log.info("재시작 전에 끝내지 못한 메시지를 다시 처리해요: channel=%s message=%s", cid_, mid)
                _replay_ids.add(mid)
                done.add(mid)
                await on_message(m)
        except Exception:  # noqa: BLE001
            log.debug("미처리 메시지 복구 실패", exc_info=True)
        for ch_id in list(bot.channels):
            ch = bot.get_channel(ch_id)
            if ch is None or not hasattr(ch, "history"):
                continue
            try:
                last = None
                async for m in ch.history(limit=20, after=since, oldest_first=True):
                    if m.author.bot or is_our_webhook(m):
                        last = None                         # 봇(캐릭터)이 그 뒤에 말했으면 이미 대답한 것
                    elif is_priority(m):
                        last = m
                if last is not None and last.id not in done:
                    _replay_ids.add(last.id)
                    log.info("꺼져 있던 동안 온 멘션에 대답: channel=%s user=%s", ch_id, last.author.display_name)
                    await on_message(last)
            except Exception:  # noqa: BLE001  (권한 없음 등: 이 채널만 건너뜀)
                log.debug("놓친 멘션 확인 실패: channel=%s", ch_id, exc_info=True)


@bot.event
async def on_guild_emojis_update(guild: discord.Guild, before, after) -> None:
    refresh_emojis()


@bot.event
async def on_typing(channel: discord.abc.Messageable, user: discord.abc.User, when: datetime) -> None:
    """누가 입력 중인지 기록해서, 말을 끝낼 때까지 기다리는 데 쓴다. (디스코드 입력 중 표시는 약 10초 지속)"""
    if not user.bot:
        cid = getattr(channel, "id", 0)
        bot.typing_until[(cid, user.id)] = time.monotonic() + 8


_replay_ids: set[int] = set()          # 재시작 복구로 다시 넣는 메시지 (중복 제거 건너뜀)
_bg_tasks: set[asyncio.Task] = set()


def mstat(ids, status: str, note: str = "", attempt: bool = False) -> None:
    """메시지 처리 상태를 서버별 DB 에 기록한다 (RECEIVED QUEUED PROCESSING RESPONDED SKIPPED FAILED). 실패해도 대화는 계속된다."""
    try:
        mem = hub.ctx().memory
    except Exception:  # noqa: BLE001
        return
    if mem is None or getattr(mem, "db", None) is None:
        return

    async def run() -> None:
        for mid in ([ids] if isinstance(ids, int) else list(ids)):
            try:
                await mem.msg_set(mid, status, note, attempt)
            except Exception:  # noqa: BLE001
                log.debug("메시지 상태 기록 실패", exc_info=True)
    t = asyncio.get_running_loop().create_task(run())
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def trace_recv(message: discord.Message, text: str, outcome: str, **kw) -> None:
    """받은 메시지를 처리 결과와 함께 기록한다 (디버깅용)."""
    if outcome == "recorded":
        mstat(message.id, "QUEUED")
    elif outcome.startswith("dropped") or outcome.startswith("recorded:"):
        mstat(message.id, "SKIPPED", outcome)
    if (outcome.startswith("dropped") or outcome == "recorded:paused") and is_priority(message):
        log.info("멘션에 대답하지 않음(%s): channel=%s user=%s", outcome, message.channel.id, message.author.display_name)
    if not engine.tracer.enabled:
        return
    ch = message.channel
    ref = getattr(message.reference, "message_id", None) if message.reference else None
    v = engine.guard.inspect(text) if text else None
    engine.tracer.log(
        "recv", channel_id=ch.id, channel=getattr(ch, "name", "DM"), guild=getattr(message.guild, "name", None),
        message_id=message.id, user_id=message.author.id, user_name=message.author.display_name,
        label=engine.label(message.author.id, message.author.display_name), text=text,
        attachments=[(a.content_type or a.filename) for a in message.attachments], reply_to=ref,
        guard={"level": v.level, "score": v.score, "kinds": v.kinds} if v else None,
        emotion=engine.mood.current().label, outcome=outcome, **kw)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    gid = message.guild.id if message.guild else DM
    await hub.ensure(gid, getattr(message.guild, "name", "DM"))
    hub.use(gid)                                   # 이후 engine/memory/persona/ratio 는 이 서버의 것
    hub.chan_guild[message.channel.id] = gid
    if message.id in _replay_ids:
        _replay_ids.discard(message.id)                     # 재시작 복구로 다시 들어온 메시지는 중복으로 보지 않는다
    elif gid != DM:
        try:
            if await memory.msg_seen(message.id, message.channel.id, message.author.id):
                log.debug("중복 메시지 무시: %s", message.id)   # 게이트웨이 재연결 등으로 같은 메시지가 두 번 오는 경우
                return
        except Exception:  # noqa: BLE001
            log.debug("메시지 중복 확인 실패", exc_info=True)
    if not user_allowed(message.author.id):
        trace_recv(message, message.content or "", "dropped:not_allowed")
        return
    text = clean_text(message)
    n_img = sum(1 for a in message.attachments if (a.content_type or "").lower().startswith("image/"))
    if not text and not n_img:
        trace_recv(message, "", "dropped:empty")
        return
    if n_img:                                   # 대화 기록에는 이미지 자체 대신 표시만 남긴다
        text = (text + " " if text else "") + f"(이미지 {n_img}장 첨부)"
    # 명령/주입을 반복 시도한 사람은 잠시 무시한다 (관리자는 제외)
    if engine.guard.is_muted(message.author.id) and message.author.id not in ADMIN_IDS:
        trace_recv(message, text, "dropped:muted")
        return
    if message.author.id not in ADMIN_IDS and not engine.guard.rate_ok(message.author.id):
        trace_recv(message, text, "dropped:rate_limit")
        return                                  # 도배(1분에 너무 많은 메시지)는 무시해서 GPU 독점을 막는다

    # ----- DM: 1:1 이므로 바로 대답 -----
    if isinstance(message.channel, discord.DMChannel):
        if dcfg["respond_to_dm"] and not bot.paused:
            trace_recv(message, text, "dm")
            await handle_dm(message, text, n_img)
        else:
            trace_recv(message, text, "dropped:dm_off_or_paused")
        return

    # ----- 서버 채널: 기록해 두고, 쿨다운 후 점수로 골라서 대답 -----
    in_channel = message.channel.id in bot.channels
    mentioned = dcfg["respond_to_mention"] and is_priority(message)
    if not (in_channel or mentioned):
        trace_recv(message, text, "dropped:channel_not_monitored")
        return
    ch, uid, display = message.channel.id, message.author.id, message.author.display_name
    who = engine.label(uid, display)
    stored = guard_store_text(message.author, text)
    await memory.add_message(ch, uid, display, "user", stored)
    mark_human_activity(ch)
    directed = bool(mentioned) or is_directed(text)
    ats = at_chars(message.content)                             # '@이름' 으로 부른 캐릭터들
    hits = ats or named_chars(text)                             # 이름이 불린 캐릭터들
    rc = replied_char(message)                                  # 답장한 캐릭터
    for cid, c in hub.ctx().chars.items():                      # 모든 캐릭터가 듣고 기분이 움직이지만, '나한테 한 말'은 해당 캐릭터만
        d = (bool(mentioned) and rc is None and not ats) or cid in hits or cid == rc
        c.engine.mood.observe(stored, directed=d, uid=uid, who=who)
    bot.last_human_ts[ch] = time.time()
    bot.chan_events.setdefault(ch, deque(maxlen=300)).append((time.time(), uid))
    ratio.record(ch, "h")
    # 먼저 한 말에 '답이 왔다'고 보는 경우: 봇을 부르거나(멘션/이름/답장) 먼저 말 건 상대가 3분 안에 말했을 때만.
    # 사람들끼리만 떠드는 건 답이 아니다. (그래서 혼자 계속 말 거는 일을 막는다)
    it = bot.init_target.get(ch)
    if directed or (it and it[0] == uid and time.time() - it[1] < 180):
        bot.init_unanswered[ch] = 0
    bot.last_speaker[ch] = who

    # ---- 말싸움: 도발/모욕을 받았고 기분이 상했으면 붙는다 ----
    in_fight = active_fight(ch, uid) is not None
    if (not in_fight and directed and not bot.paused
            and engine.guard.inspect(text).level != "block"):
        ctx_ = hub.ctx()
        for cid in (hits or ([rc] if rc else [ctx_.primary])):  # 시비를 받은 캐릭터가 싸움에 붙을지 정한다
            c = ctx_.chars.get(cid)
            if c is None:
                continue
            e, m = c.engine, c.engine.mood
            if e.fights.should_start(ch, uid, text, directed, m.current().id, m.s["irritation"], m.affinity(uid)):
                e.fights.start(ch, uid, display)
                m.on_fight_start(uid, who)
                in_fight = True
                e.tracer.log("event", what="fight_start", channel_id=ch, user_id=uid, user_name=display,
                             detail=text, emotion=m.current().label, irritation=round(m.s["irritation"], 2), char=cid)
                log.info("말싸움 시작: channel=%s user=%s(%s) char=%s", ch, uid, display, cid)
                break
    if bot.paused:
        trace_recv(message, text, "recorded:paused", directed=directed, mentioned=bool(mentioned), in_fight=in_fight,
                   stored=stored)
        return
    images = await vision.collect_images(message, vcfg) if n_img else []
    hr = replies_to_human(message)
    bot.pending.setdefault(ch, []).append(Pending(
        message, time.monotonic(), priority=bool(mentioned) or in_fight,
        uid=uid, uname=display, images=images, human_reply=hr,
    ))
    trace_recv(message, text, "recorded", directed=directed, mentioned=bool(mentioned), in_fight=in_fight,
               replies_to_human=hr, images=len(images), stored=stored if stored != text else None,
               pending_in_channel=len(bot.pending.get(ch, [])))


def guard_store_text(author: discord.abc.User, text: str) -> str:
    """대화 기록에는 무력화한 텍스트만 저장한다. 차단 수준의 시도가 반복되면 잠시 무시(뮤트)한다."""
    verdict = engine.guard.inspect(text)
    if verdict.level == "block":
        log.warning("인젝션 시도 user=%s(%s) score=%s kinds=%s", author.id, author.display_name,
                    verdict.score, verdict.kinds)
        if engine.guard.note_attempt(author.id) and author.id not in ADMIN_IDS:
            minutes = engine.guard.cfg["mute_minutes"]
            log.warning("반복 시도로 %s분간 무시: %s", minutes, author.id)
            engine.mood.set_emotion("annoyed", minutes)
    return engine.guard.history_text(text, verdict)


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


dm_images: dict[int, list[bytes]] = {}


async def handle_dm(message: discord.Message, text: str, n_img: int = 0) -> None:
    """DM: 연달아 온 메시지를 잠깐 기다렸다가 한 번에 묶어서 처리한다."""
    uid = message.author.id
    await memory.set_dm_channel(uid, message.channel.id)
    stored = guard_store_text(message.author, text)
    await memory.add_message(message.channel.id, uid, message.author.display_name, "user", stored)
    engine.mood.observe(stored, directed=True, uid=uid, who=engine.label(uid, message.author.display_name))
    bot.dm_buffer.setdefault(uid, []).append(text)
    if n_img:
        dm_images.setdefault(uid, []).extend(await vision.collect_images(message, vcfg))
    t = bot.dm_tasks.get(uid)
    if t and not t.done() and uid not in bot.dm_generating:
        t.cancel()                    # 아직 기다리는 중이면 새 메시지까지 포함해서 다시 기다린다
    bot.dm_tasks[uid] = asyncio.create_task(dm_worker(message.channel, message.author))


async def dm_worker(channel: discord.DMChannel, author: discord.abc.User) -> None:
    uid = author.id
    try:
        start = time.monotonic()
        await asyncio.sleep(settle_sec())
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
            images = dm_images.pop(uid, [])[: vcfg.get("max_images", 2)]
            bot.dm_generating.add(uid)
            try:
                async with bot.gpu_lock:
                    async with channel.typing():
                        bubbles = await engine.reply(
                            channel.id, uid, author.display_name, "\n".join(texts), dm=True,
                            images=images or None)
                if bubbles:
                    await send_bubbles(channel, bubbles)
            finally:
                bot.dm_generating.discard(uid)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("DM 응답 실패 (uid=%s)", uid)


def _requeue(channel_id: int, target: Pending) -> None:
    target.ts = time.monotonic()
    bot.pending.setdefault(channel_id, []).append(target)


async def respond_group(channel_id: int, target: Pending) -> None:
    msg = target.message
    channel = msg.channel
    uid = target.uid or msg.author.id
    uname = target.uname or msg.author.display_name
    bot.busy.add(channel_id)
    t0 = time.monotonic()
    ids = target.meta.get("merged_ids") or [msg.id]
    if target.kind == "msg":
        mstat(ids, "PROCESSING", attempt=True)
    try:
        fight = engine.fights.get(channel_id, uid)
        async with bot.gpu_lock:
            tracelib.set_meta(selection=target.meta, kind_in=target.kind, wait_sec=round(time.monotonic() - target.ts, 1),
                              message_id=msg.id, channel_name=getattr(channel, "name", None),
                              fight_state=(fight.advantage if fight else None))
            async with channel.typing():
                note = bot.return_note.get(channel_id)
                extra = note
                if target.kind == "reaction":
                    extra = ((note + " ") if note else "") + (
                        "상대가 네 메시지에 이모지 리액션을 달았다. 그 리액션에 대한 짧은 반응 한마디만 해라. "
                        "(고마움, 장난, 놀림 등. 길게 말하지 마라)")
                bubbles = await engine.reply(
                    channel_id, uid, uname, target.text or clean_text(msg),
                    # 멘션/답장/싸움이 아니고, 돌아온 상황 메모도 없을 때만 SKIP 가능
                    can_skip=not target.priority and not note and fight is None and target.kind == "msg" and ignorable(uid),
                    priority=target.priority, extra_note=extra,
                    images=target.images or None, fight=fight, bubble_bias=target.bubbles,
                )
                if bubbles and note:
                    bot.return_note.pop(channel_id, None)
        t_gen = time.monotonic()
        if bubbles:
            bot.last_replied[uid] = time.time()
            bot.last_target[channel_id] = uid
            unpark(channel_id, uid)
            # 그 사이에 다른 메시지가 올라왔으면 '답장'으로 누구에게 하는 말인지 표시
            latest = getattr(channel, "last_message_id", None)
            reply_to = msg if (latest and latest != msg.id and target.kind == "msg") else None
            await send_bubbles(channel, bubbles, reply_to)
            cur = hub.ctx().char().cid
            bot.last_char[channel_id] = cur
            maybe_chain(channel, cur, bubbles, 1)                  # 다른 캐릭터가 그 말에 받아칠 수도 있다
            mstat(ids, "RESPONDED")
            t_end = time.monotonic()
            # 어디서 시간이 걸리는지 로그로 남긴다: 수신 후 대기(묶음/쿨다운/GPU 대기) · 생성(LLM) · 전송
            log.info("응답 완료: 메시지 수신 후 %.1fs (대기 %.1fs · 생성 %.1fs · 전송 %.1fs)",
                     t_end - target.ts, t0 - target.ts, t_gen - t0, t_end - t_gen)
        elif engine.last_skip and target.kind == "msg":
            mstat(ids, "SKIPPED", "model_skip")
            await maybe_react(msg, ecfg.get("react_on_skip", 0.5), target.text)   # 글로 안 답하면 이모지로라도 반응
        elif target.kind == "msg":
            mstat(ids, "SKIPPED", "no_output")
    except ModelMissing as e:
        mstat(ids, "FAILED", "model_missing")
        if time.monotonic() - bot.model_warned > 60:                   # 같은 오류가 메시지마다 길게 찍히지 않게 1분에 한 번만
            bot.model_warned = time.monotonic()
            log.error("모델을 쓸 수 없어 대답하지 못했어요 (channel=%s): %s", channel_id, e)
    except Exception:  # noqa: BLE001
        log.exception("서버 응답 생성 실패 (channel=%s)", channel_id)
        attempt = int(target.meta.get("attempt", 0))
        if target.kind == "msg" and attempt < 2:                 # 일시적 오류: 백오프(3초, 6초) 뒤 다시 대기열에 넣는다
            target.meta["attempt"] = attempt + 1
            mstat(ids, "QUEUED", f"retry{attempt + 1}")
            asyncio.get_running_loop().call_later(3 * (attempt + 1), _requeue, channel_id, target)
        else:
            mstat(ids, "FAILED", "exception")
    finally:
        # 대답이 완전히 끝난 시점부터 쿨다운 타이머 시작 (싸움 중에는 거의 바로, 인원이 많으면 짧게)
        in_fight = engine.fights.get(channel_id, uid) is not None
        bot.next_ok[channel_id] = time.monotonic() + (1.0 if in_fight else next_cooldown(channel_id))
        bot.busy.discard(channel_id)


SKIP_AFF_BELOW = 10          # 호감도(0~100)가 이 미만인 상대에게만 '아예 대답하지 않는' 일이 가끔 생긴다


def ignorable(uid: int | None) -> bool:
    """이 상대의 말은 가끔 무시해도 되는가: 호감도가 10 미만일 때만 (그 외에는 말을 걸면 대답한다)."""
    try:
        m = engine.mood
        return m.aff100(m.affinity(uid)) < SKIP_AFF_BELOW
    except Exception:  # noqa: BLE001
        return False


def to_candidate(p: Pending, now: float, channel_id: int) -> targeting.Candidate:
    uid = p.uid or p.message.author.id
    text = p.text or clean_text(p.message)
    return targeting.Candidate(
        uid=uid, text=text, age=now - p.ts, priority=p.priority,
        in_fight=active_fight(channel_id, uid) is not None, directed=is_directed(text),
        human_reply=p.human_reply, has_image=bool(p.images), affinity=engine.mood.affinity(uid),
        since_replied=time.time() - bot.last_replied.get(uid, 0),
        is_last_target=bot.last_target.get(channel_id) == uid,
    )


@tasks.loop(seconds=0.5)
async def group_loop() -> None:
    """채널별로 쿨다운이 끝났으면, 그동안 올라온 메시지 중 점수가 높은 사람을 골라 대답한다."""
    now = time.monotonic()
    for channel_id, items in list(bot.pending.items()):
        hub.use(hub.guild_of(channel_id))          # 이 채널이 속한 서버의 설정/저장소로 처리
        if bot.paused:
            continue
        for p in [p for p in items if now - p.ts > gcfg["max_pending_age_sec"]]:
            park_unaddressed(channel_id, p)             # 오래 기다려도 대답 못 한 메시지는 능동 대화가 챙긴다
            mstat(p.message.id, "SKIPPED", "expired")
        items[:] = [p for p in items if now - p.ts <= gcfg["max_pending_age_sec"]]
        if not items or channel_id in bot.busy:
            continue

        # 1) 같은 사람이 연달아 쓰는 중이면 끝날 때까지 기다린다 (마지막 메시지 후 settle_sec, 입력 중이면 더)
        latest: dict[int, float] = {}
        for p in items:
            u = p.uid or p.message.author.id
            latest[u] = max(latest.get(u, 0), p.ts)

        def settled(p: Pending) -> bool:
            if p.kind == "reaction":
                return True
            u = p.uid or p.message.author.id
            quiet = now - latest[u]
            if quiet < settle_sec():
                return False
            typing = bot.typing_until.get((channel_id, u), 0) > now
            return not typing or quiet >= gcfg["max_settle_wait_sec"]

        ready = [p for p in items if settled(p)]
        if not ready:
            continue

        # 2) ㅋㅋ 같은 사소한 말은 LLM 없이 이모지만 달고 넘어간다 (속도 + 자연스러움)
        trivial = [p for p in ready if p.kind == "msg" and not p.priority and not p.images
                   and emojis.is_trivial(p.text or clean_text(p.message))]
        if trivial:
            for p in trivial:
                engine.tracer.log("decision", action="trivial_react_only", channel_id=channel_id, user_id=p.uid,
                                  user_name=p.uname, detail=p.text or clean_text(p.message))
                asyncio.create_task(maybe_react(p.message, ecfg.get("trivial_react_chance", 0.5)))
                mstat(p.message.id, "SKIPPED", "trivial_react")
            ready = [p for p in ready if p not in trivial]
            items[:] = [p for p in items if p not in trivial]
            if not ready:
                continue

        prio = [p for p in ready if p.priority]
        users, _ = crowd_now(channel_id)
        adj = ratio.adjust(channel_id)                  # 채팅 비율 보정 (대답 확률, 말풍선 수)
        cooling = now < bot.next_ok.get(channel_id, 0)
        if cooling and not (prio and gcfg["mention_bypass_cooldown"]):
            continue
        if prio:
            pool = prio
        else:
            chance = (gcfg["reply_chance"] + targeting.chance_boost(users, gcfg["crowd_chance_per_user"])
                      + 0.2 * (engine.mood.engage - 1.0))      # 적극성이 높을수록 더 많은 말에 대답한다
            p_reply = min(1.0, chance * engine.mood.reply_mult * adj.reply)
            others_ok = [p for p in ready if not ignorable(p.uid or p.message.author.id)]
            if others_ok and users <= 1 and not ratio.over_cap(channel_id) and not engine.mood.schedule.asleep():
                p_reply = 1.0                              # 호감도 10 이상인 사람이 혼자 말하면 항상 대답한다 (무시는 호감도 10 미만일 때만)
            elif not others_ok:
                p_reply = min(p_reply, 0.5)                # 호감도 10 미만인 사람만 있으면 가끔 대답을 안 한다
            if random.random() > p_reply:
                engine.tracer.log("decision", action="skip_chance", channel_id=channel_id,
                                  detail=f"대답 확률 {p_reply:.2f} 탈락 (사람 {users}명, 비율보정 x{adj.reply:.2f})",
                                  messages=[{"user_id": p.uid, "user_name": p.uname, "text": p.text or clean_text(p.message)}
                                            for p in ready][:6])
                # 글로는 대답하지 않지만 이모지 리액션은 달 수 있다. 답을 못 받은 메시지는 보관해 둔다
                for p in ready:
                    asyncio.create_task(maybe_react(p.message, ecfg.get("react_chance", 0.12)))
                    park_unaddressed(channel_id, p)
                    mstat(p.message.id, "SKIPPED", "skip_chance")
                items[:] = [p for p in items if p not in ready]
                continue
            pool = ready

        # 3) 대상 점수(멘션, 싸움, 이름 부름, 질문, 호감도, 오래 대답 못 받음, 최신성...)로 가중 랜덤 선택
        cands = [to_candidate(p, now, channel_id) for p in pool]
        target = pool[targeting.pick(cands)]
        tuid = target.uid or target.message.author.id
        sel_meta = {
            "pool": [{"user_id": c.uid, "name": p.uname, "text": c.text[:80], "score": round(targeting.score(c), 2),
                      "priority": c.priority, "in_fight": c.in_fight, "directed": c.directed,
                      "human_reply": c.human_reply, "affinity": round(c.affinity, 2),
                      "since_replied": int(min(c.since_replied, 99999)), "age": round(c.age, 1)}
                     for c, p in zip(cands, pool)],
            "chosen_user": tuid, "chosen_name": target.uname, "priority_pool": bool(prio),
            "crowd_users": users, "ratio_reply_x": round(adj.reply, 2), "ratio_bubbles_x": round(adj.bubbles, 2),
            "pending_total": len(items), "cooldown_s": round(bot.cooldown, 1), "engage": engine.mood.engage,
        }

        # 4) 고른 사람이 연달아 보낸 메시지는 하나로 묶어서 대답한다. 다른 사람 메시지는 남겨서 다음 순서에 대답
        if target.kind == "reaction":
            same = [target]
            text = target.text
            imgs: list = []
        else:
            same = [p for p in ready if (p.uid or p.message.author.id) == tuid and p.kind == "msg"] or [target]
            text = "\n".join(t for t in (p.text or clean_text(p.message) for p in same) if t)
            imgs = [im for p in same for im in p.images][: vcfg.get("max_images", 2)]
            if imgs:
                text = (text + "\n" if text else "") + f"(이미지 {len(imgs)}장 첨부)"
        merged = Pending(
            message=same[-1].message, ts=same[-1].ts,
            priority=any(p.priority for p in same), text=text,
            uid=tuid, uname=target.uname or target.message.author.display_name,
            images=imgs, kind=target.kind, bubbles=adj.bubbles,
            meta={**sel_meta, "merged_count": len(same),
                  "merged_texts": [(p.text or clean_text(p.message))[:100] for p in same],
                  "merged_ids": [p.message.id for p in same],
                  "left_pending": [{"user_id": p.uid, "text": (p.text or clean_text(p.message))[:60]}
                                   for p in items if p not in same][:5]},
        )
        items[:] = [p for p in items if p not in same]
        speaker = pick_speaker(channel_id, text, tuid, merged.message)   # 여러 캐릭터 중 누가 대답할지
        merged.meta["speaker"] = speaker
        hub.use_char(speaker)
        asyncio.create_task(respond_group(channel_id, merged))           # (현재 캐릭터가 작업으로 이어진다)
        hub.use_char(None)


def maybe_chain(channel, speaker_cid: str, bubbles: list[str], depth: int) -> None:
    """방금 한 캐릭터의 말에 같은 방의 다른 캐릭터가 가끔 받아친다. 사람이 한동안 없으면 안 하고, 횟수/확률을 줄여 가며 끝없이 이어지지 않게 한다."""
    ctx = hub.ctx()
    if len(ctx.chars) < 2 or bot.paused or depth > int(mcfg["max_chain"]) or not bubbles or not ctx.state.chain_auto:
        return
    ch = channel.id
    if time.time() - bot.last_human_ts.get(ch, 0) > float(mcfg["human_window_sec"]):
        return                                               # 사람이 없으면 캐릭터끼리만 계속 떠들지 않는다
    if ratio.over_cap(ch):
        return                                               # 봇이 이미 너무 말을 많이 했다
    p = float(mcfg["chain_chance"]) * float(mcfg["chain_decay"]) ** (depth - 1) * min(1.6, max(0.6, engine.mood.engage))
    if random.random() >= p:
        return
    others = [c for c in ctx.chars if c != speaker_cid]
    weights = [max(0.05, float(ctx.chars[c].persona.data.get("talk_weight", 1.0)) * max(0.2, ctx.chars[c].engine.mood.reply_mult))
               for c in others]
    cid = random.choices(others, weights=weights, k=1)[0]
    asyncio.create_task(respond_chain(channel, ctx.gid, cid, speaker_cid, " ".join(bubbles), depth))


def peer_note(pname: str, topic: str = "") -> str:
    """캐릭터가 다른 캐릭터 말에 답할 때의 지시. 서로 질문만 되돌려주며 같은 틀을 반복하는 핑퐁을 막는다."""
    return (f"방금 같은 방 친구 캐릭터 {pname}이(가) 한 말에 대한 반응이다. 사람이 아니라 친구 캐릭터에게 하는 말이다. "
            "네 말투(반말/존댓말)를 그대로 지키고 상대의 말투나 문장 틀을 따라 하지 마라. "
            "질문을 되돌려주지 말고 구체적인 의견이나 제안이나 경험 하나를 말해서 대화를 앞으로 진행시켜라."
            + (f" (주제: {topic})" if topic else ""))


def _bigrams(s: str) -> set[str]:
    t = re.sub(r"[^가-힣A-Za-z0-9]", "", s)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def stale_talk(new: str, recent: list[str], th: float = 0.6) -> bool:
    """새 말이 최근 캐릭터 대화와 거의 같은 내용이면 True (같은 질문을 주고받는 핑퐁 감지).
    짧은 쪽 글자쌍의 th 이상이 겹치면 같은 말로 본다 ('그럼 뭘 하고 싶은데' ~ '이번엔 뭘 하고 싶은데 정하든가')."""
    a = _bigrams(new)
    if len(a) < 3:
        return False
    return any(len(a & b) / max(1, min(len(a), len(b))) >= th for b in (_bigrams(r) for r in recent[-4:]) if len(b) >= 3)


async def respond_chain(channel, gid: int, cid: str, from_cid: str, text: str, depth: int) -> None:
    """캐릭터 cid 가 같은 방 캐릭터 from_cid 의 말에 반응한다 (사람 메시지가 기다리고 있으면 사람이 먼저)."""
    hub.use(gid)
    hub.use_char(cid)
    ch = channel.id
    await asyncio.sleep(random.uniform(*[float(x) for x in mcfg["chain_delay_sec"]]))
    if ch in bot.busy or bot.paused or bot.pending.get(ch):
        return
    bot.busy.add(ch)
    try:
        ctx = hub.ctx()
        from_name = ctx.chars[from_cid].persona.name if from_cid in ctx.chars else "친구"
        async with bot.gpu_lock:
            async with channel.typing():
                bubbles = await engine.reply(
                    ch, char_uid(from_cid), from_name, text, can_skip=True, priority=False,
                    extra_note=peer_note(from_name))
        if bubbles and stale_talk(" ".join(bubbles), [text]):
            bubbles = []                                      # 상대 말을 거의 그대로 되돌려주는 말이면 보내지 않는다
        if bubbles:
            await send_bubbles(channel, bubbles)
            bot.last_char[ch] = cid
            maybe_chain(channel, cid, bubbles, depth + 1)
    except Exception:  # noqa: BLE001
        log.exception("캐릭터 간 대화 실패 (channel=%s, %s→%s)", ch, from_cid, cid)
    finally:
        bot.busy.discard(ch)
        bot.next_ok[ch] = max(bot.next_ok.get(ch, 0), time.monotonic() + 2.0)


async def run_fight_open(channel, f: Fight) -> None:
    """/싸움 명령으로 시작한 싸움: 이미 기분이 나쁜 봇이 상대를 멘션하며 먼저 욕하고 시비를 건다."""
    bot.busy.add(channel.id)
    try:
        async with bot.gpu_lock:
            async with channel.typing():
                bubbles = await engine.reply(
                    channel.id, f.user_id, f.user_name, "(네가 먼저 시비를 걸어 싸움을 시작한다)",
                    fight=f, priority=True, phase="open")
        if bubbles:
            bubbles[0] = f"<@{f.user_id}> {bubbles[0]}"          # 상대를 호출(멘션)하며 시작
            await send_bubbles(channel, bubbles)
    except Exception:  # noqa: BLE001
        log.exception("싸움 선공 실패")
    finally:
        bot.busy.discard(channel.id)
        bot.next_ok[channel.id] = time.monotonic() + 1.0


# ------------------ 능동 대화: 스스로 상황을 분석해서 먼저 말하기 ------------------
def park_unaddressed(channel_id: int, p: Pending) -> None:
    """대답 후보에서 탈락한(답을 못 받은) 메시지를 보관한다. 나중에 능동 대화가 그 사람에게 먼저 말을 건다."""
    if p.kind != "msg" or p.priority:
        return
    text = (p.text or clean_text(p.message)).strip()
    if len(text) < 4 or emojis.is_trivial(text):
        return
    uid = p.uid or p.message.author.id
    lst = bot.unaddressed.setdefault(channel_id, [])
    lst[:] = [x for x in lst if x[0] != uid]                        # 사람당 최신 1개만
    lst.append((uid, p.uname or p.message.author.display_name, text, time.time()))
    del lst[:-8]


def unpark(channel_id: int, uid: int) -> None:
    lst = bot.unaddressed.get(channel_id)
    if lst:
        lst[:] = [x for x in lst if x[0] != uid]


def roll_gap(channel_id: int) -> float:
    """다음 능동 대화까지의 랜덤 쿨타임 (적극성이 높으면 짧게)"""
    lo, hi = inicfg.get("min_gap_sec", [45, 180])
    g = random.uniform(float(lo), float(hi)) / max(1.0, engine.mood.engage) ** 0.5
    g *= 1 + 0.8 * bot.init_unanswered.get(channel_id, 0)        # 답이 없을수록 더 오래 기다린다
    bot.init_gap[channel_id] = g
    return g


def build_snapshot(channel_id: int) -> initiative.Snapshot:
    now = time.time()
    ev = list(bot.chan_events.get(channel_id, ()))
    last_h = max((t for t, _ in ev), default=0.0)
    m3 = [(t, u) for t, u in ev if now - t <= 180]
    prev = [(t, u) for t, u in ev if 180 < now - t <= 780]
    adj = ratio.adjust(channel_id)
    inits = [t for t in bot.init_times.get(channel_id, ()) if now - t <= 3600]
    lb = bot.last_bot_msg.get(channel_id)
    un = [(u, n, tx, now - t) for u, n, tx, t in bot.unaddressed.get(channel_id, []) if now - t <= 300]
    return initiative.Snapshot(
        silence=(now - last_h) if last_h else 9999.0,
        msgs_3m=len(m3), msgs_prev=len(prev), users_3m=len({u for _, u in m3}),
        since_bot=(now - lb) if lb else 9999.0,
        last_init_age=(now - inits[-1]) if inits else 9999.0,
        unanswered_inits=bot.init_unanswered.get(channel_id, 0), inits_hour=len(inits),
        deficit=ratio.deficit(channel_id), hour=engine.now().hour,
        gap_needed=bot.init_gap.get(channel_id) or roll_gap(channel_id),
        unaddressed=un, last_speaker=bot.last_speaker.get(channel_id, ""),
        ratio=adj.ratio, target=adj.target,
    )


async def run_initiative(channel_id: int, dec: initiative.Decision, snap: initiative.Snapshot,
                         adj) -> None:
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    bot.busy.add(channel_id)
    try:
        target = (dec.target[0], dec.target[1]) if dec.target else None
        async with bot.gpu_lock:
            async with channel.typing():
                bubbles = await engine.initiate(
                    channel_id, initiative.instruction(dec, hour=engine.now().hour, weekday=engine.now().weekday()),
                    int(snap.silence // 60), 0,
                    analysis=initiative.analysis_text(snap), target=target, bubble_bias=adj.bubbles)
        if bubbles:
            if dec.kind == "ask_user" and target:
                bubbles[0] = f"<@{target[0]}> {bubbles[0]}"      # 답을 못 받은 사람을 호출하며 먼저 말을 건다
                unpark(channel_id, target[0])
                bot.init_target[channel_id] = (target[0], time.time())
            await send_bubbles(channel, bubbles)
            bot.init_times.setdefault(channel_id, deque(maxlen=60)).append(time.time())
            bot.init_unanswered[channel_id] = bot.init_unanswered.get(channel_id, 0) + 1
            log.info("먼저 말걸기(%s): channel=%s p=%.3f [%s]", dec.kind, channel_id, dec.p, dec.reason)
    except Exception:  # noqa: BLE001
        log.exception("능동 대화 실패 (channel=%s)", channel_id)
    finally:
        roll_gap(channel_id)
        bot.busy.discard(channel_id)


@tasks.loop(seconds=3)
async def initiative_loop() -> None:
    for gid in hub.gids():
        hub.use(gid)
        try:
            await _initiative_tick()
        except Exception:  # noqa: BLE001  (한 서버의 오류가 다른 서버를 멈추지 않게)
            log.exception("initiative_loop 처리 실패 (서버 %s)", gid)


async def _initiative_tick() -> None:
    """채널별로 침묵, 대화 속도 감소, 채팅 비율 부족, 답 못 받은 사람, 랜덤 쿨타임을 종합해 먼저 말할지 정한다."""
    if bot.paused or not bot.init_enabled or not inicfg.get("enabled", True):
        return
    for ch_id in list(bot.channels):
        if bot.gpu_lock.locked():
            return                                   # GPU 가 바쁘면 이번 턴은 건너뛴다 (대답 우선)
        if ch_id in bot.busy or bot.pending.get(ch_id) or engine.fights.in_channel(ch_id):
            continue
        snap = build_snapshot(ch_id)
        adj = ratio.adjust(ch_id)
        if engine.mood.schedule.asleep():
            continue                                        # 자는 시간이면 먼저 말을 걸지 않는다
        sched = engine.mood.schedule.effects()[1]           # 바쁜 시간대엔 덜 한다
        dec = initiative.decide(snap, inicfg, engine.mood.engage, adj.initiate * sched)
        if dec.fire:
            hub.use_char(pick_free_speaker(ch_id))          # 캐릭터가 여럿이면 그중 하나가 먼저 말을 꺼낸다
            await run_initiative(ch_id, dec, snap, adj)
            bot.last_char[ch_id] = hub.ctx().char().cid
            hub.use_char(None)


@initiative_loop.before_loop
async def _init_ready() -> None:
    await bot.wait_until_ready()


@tasks.loop(minutes=15)
async def web_loop() -> None:
    for gid in hub.gids():
        for cid in list(hub.ctx(gid).chars):
            hub.use(gid)
            hub.use_char(cid)
            try:
                await _web_tick()
            except Exception:  # noqa: BLE001  (한 서버의 오류가 다른 서버를 멈추지 않게)
                log.exception("web_loop 처리 실패 (서버 %s)", gid)


async def _web_tick() -> None:
    """유행어/캐릭터 소식을 백그라운드에서 미리 받아 캐시한다 (답변 속도에 영향 없음). 오래된 항목만 갱신."""
    w = engine.web
    if not w.enabled or not w.stale([engine.web_key(), "slang"]):
        return
    try:
        await w.refresh(engine.web_topics())
    except Exception:  # noqa: BLE001
        log.exception("웹 정보 갱신 실패")


@web_loop.before_loop
async def _web_ready() -> None:
    await bot.wait_until_ready()
    await asyncio.sleep(45)                      # 시작 직후 모델 웜업과 겹치지 않게


@tasks.loop(seconds=5)
async def fight_loop() -> None:
    for gid in hub.gids():
        for cid in list(hub.ctx(gid).chars):
            hub.use(gid)
            hub.use_char(cid)
            try:
                await _fight_tick()
            except Exception:  # noqa: BLE001  (한 서버의 오류가 다른 서버를 멈추지 않게)
                log.exception("fight_loop 처리 실패 (서버 %s)", gid)


async def _fight_tick() -> None:
    """상대가 대꾸하지 않으면 봇이 먼저 다시 도발하고(재촉), 한동안 말이 없는 싸움은 마무리한다."""
    if bot.paused:
        return
    now_t = time.time()
    fcfg = engine.fights.cfg
    for f in list(engine.fights.active.values()):
        if f.ending or f.channel_id in bot.busy or not f.last_bot:
            continue
        if f.nudges < fcfg.get("max_nudges", 2) and f.last_bot >= f.last_user and now_t - f.last_bot >= fcfg.get("nudge_after_sec", 25):
            ch = bot.get_channel(f.channel_id)
            if ch is None:
                continue
            bot.busy.add(f.channel_id)
            try:
                async with bot.gpu_lock:
                    async with ch.typing():
                        bubbles = await engine.reply(f.channel_id, f.user_id, f.user_name, "(상대가 대꾸하지 않음)",
                                                     fight=f, priority=True, phase="nudge")
                if bubbles:
                    await send_bubbles(ch, bubbles)
            except Exception:  # noqa: BLE001
                log.exception("싸움 재촉 실패")
            finally:
                bot.busy.discard(f.channel_id)
    for f in engine.fights.timed_out():
        ch = bot.get_channel(f.channel_id)
        if ch is None:
            engine.fights.finish(f)
            continue
        if f.channel_id in bot.busy:
            f.ending = ""                      # 바쁘면 다음 턴에 다시 시도
            continue
        bot.busy.add(f.channel_id)
        try:
            async with bot.gpu_lock:
                async with ch.typing():
                    bubbles = await engine.reply(
                        f.channel_id, f.user_id, f.user_name, "(상대가 말없이 사라짐)", fight=f, priority=True)
            if bubbles:
                await send_bubbles(ch, bubbles)
        except Exception:  # noqa: BLE001
            log.exception("말싸움 마무리 실패")
            engine.fights.finish(f)
        finally:
            bot.busy.discard(f.channel_id)
            bot.next_ok[f.channel_id] = time.monotonic() + next_cooldown(f.channel_id)


@fight_loop.before_loop
async def _fight_ready() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    """봇 메시지에 누가 이모지 리액션을 달면 그에 대해 반응한다."""
    gid = payload.guild_id or DM
    if gid not in hub.ctxs:
        return
    hub.use(gid)
    if not ecfg.get("enabled", True) or bot.paused:
        return
    if (bot.user and payload.user_id == bot.user.id) or payload.channel_id not in bot.channels:
        return
    member = payload.member
    if member is None or member.bot or not user_allowed(member.id):
        return
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    if not bot.user or msg.author.id != bot.user.id:
        return
    emoji = str(payload.emoji)
    who = engine.label(member.id, member.display_name)
    snippet = (msg.clean_content or "").replace("\n", " ").strip()[:30]
    note = f'(네 말 "{snippet}"에 {who}이(가) {emoji} 리액션을 남김)'
    await memory.add_message(channel.id, member.id, member.display_name, "user", note)
    engine.mood.on_reaction(member.id, Emojis.classify_reaction(emoji), who)
    if random.random() < ecfg.get("respond_to_reaction", 0.6):
        bot.pending.setdefault(channel.id, []).append(Pending(
            msg, time.monotonic(), priority=False, text=note, uid=member.id,
            uname=member.display_name, kind="reaction"))


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
    for gid in hub.gids():
        hub.use(gid)
        try:
            await _idle_tick()
        except Exception:  # noqa: BLE001  (한 서버의 오류가 다른 서버를 멈추지 않게)
            log.exception("idle_loop 처리 실패 (서버 %s)", gid)


async def _idle_tick() -> None:
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
        if st.stage >= icfg["max_unanswered"] or st.day_count >= icfg["max_per_day"] * max(1.0, engine.mood.engage):
            continue
        bot.busy.add(ch_id)
        try:
            hub.use_char(pick_free_speaker(ch_id))
            result = await run_idle_event(ch_id, st)
            bot.last_char[ch_id] = hub.ctx().char().cid
            hub.use_char(None)
            log.info("혼잣말 이벤트 (channel=%s, stage=%s): %s", ch_id, st.stage, result)
            if result == "채널을 찾을 수 없음":          # 지워졌거나 봇이 못 보는 채널: 매 분 다시 시도해 로그만 쌓이지 않게 1시간 쉰다
                st.next_at = time.time() + 3600
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
    hub.use(DM)                                    # DM 은 DM 전용 저장소
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


# ---- 1. 사용량 (전체 + 유저별 통합) ----
@bot.tree.command(name="사용량", description="(별칭: /통계 동작:토큰) 토큰 사용량을 보여줘요. 유저를 고르면 그 유저에게 한 답변 내역까지, 생략하면 전체 + 유저 순위")
@app_commands.describe(user="특정 유저를 고르면 그 유저에게 한 답변 내역을 보여줘요 (생략하면 전체 요약 + 순위)")
@app_commands.rename(user="유저")
async def usage_cmd(interaction: discord.Interaction, user: Optional[discord.User] = None) -> None:
    if user is not None:
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
        return

    u = await memory.usage_total()
    n, p, c = u["all"]
    names = {"reply": "답변", "memory": "기억", "proactive": "DM먼저", "idle": "먼저말걸기", "guard": "차단"}
    lines = [
        "**전체 토큰 사용량**" + (f" (기록 시작 {kst(u['since'])})" if u["since"] else ""),
        f"```총 {fmt(p + c)} 토큰 | 호출 {fmt(n)}회 (입력 {fmt(p)} / 출력 {fmt(c)})",
        f"24시간 {fmt(u['day'][1] + u['day'][2])} ({fmt(u['day'][0])}회) · 7일 {fmt(u['week'][1] + u['week'][2])} ({fmt(u['week'][0])}회)```",
    ]
    if u["by_kind"]:
        lines.append("**용도별** " + " · ".join(
            f"{names.get(k, k)} {fmt((pp or 0) + (cc or 0))}" for k, nn, pp, cc in u["by_kind"]))
    if u["by_character"]:
        lines.append("**캐릭터별** " + " · ".join(
            f"{k or '-'} {fmt((pp or 0) + (cc or 0))}" for k, nn, pp, cc in u["by_character"]))
    rows = await memory.usage_by_user(8)
    if rows:
        body = "\n".join(
            f"{i:>2}. {trunc(name or str(uid), 14):<14} {fmt((pp or 0) + (cc or 0)):>10} 토큰  답변 {fmt(nn)}회"
            for i, (uid, name, nn, pp, cc) in enumerate(rows, 1))
        lines.append(f"**유저 순위**\n```{body}```")
    lines.append("-# 개인 내역: `/사용량 유저:@이름` · 입력 토큰은 캐시 사용 시 실제보다 적게 잡힐 수 있어요.")
    await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)


# ---- 3. 캐릭터스왑 ----
async def character_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower()
    return [
        app_commands.Choice(name=f"{name} ({cid})", value=cid)
        for cid, name in Persona.available()
        if cur in cid.lower() or cur in name.lower()
    ][:25]


async def guild_char_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """이 서버에 있는 캐릭터만 보여 준다"""
    ctx = hub.ctxs.get(interaction.guild_id or DM)
    cur = current.lower()
    return [
        app_commands.Choice(name=f"{c.persona.name} ({cid})", value=cid)
        for cid, c in (ctx.chars.items() if ctx else [])
        if cur in cid.lower() or cur in c.persona.name.lower()
    ][:25]


@bot.tree.command(name="캐릭터스왑", description="(별칭: /캐릭터 로 통합됨) 이 서버의 캐릭터를 교체하거나, 캐릭터를 더 추가/제거해요")
@app_commands.describe(character="대상 캐릭터 (목록일 땐 생략)", mode="교체(기본) / 추가(같이 활동) / 제거 / 목록",
                       old="교체할 기존 캐릭터 (생략하면 기본 캐릭터)",
                       clear_history="교체할 때 대화 기록을 지울지 (기본: 지움)",
                       greet="바꾼 뒤 이 채널에 캐릭터 첫 인사를 보낼지 (기본: 보냄)")
@app_commands.rename(character="캐릭터", mode="동작", old="기존", clear_history="기록초기화", greet="인사")
@app_commands.choices(mode=[
    app_commands.Choice(name="교체", value="replace"),
    app_commands.Choice(name="추가", value="add"),
    app_commands.Choice(name="제거", value="remove"),
    app_commands.Choice(name="목록", value="list"),
])
@app_commands.autocomplete(character=character_autocomplete, old=guild_char_autocomplete)
async def swap_character(
    interaction: discord.Interaction, character: Optional[str] = None,
    mode: Optional[app_commands.Choice[str]] = None, old: Optional[str] = None,
    clear_history: bool = True, greet: bool = True,
) -> None:
    act = mode.value if mode else "replace"
    ctx = hub.ctx()
    avail = dict(Persona.available())
    if act == "list":
        here = "\n".join(f"{'⭐' if cid == ctx.primary else '•'} **{c.persona.name}** (`{cid}`) · 감정 {c.engine.mood.current().label} · "
                         f"{'아바타 있음' if c.persona.data.get('avatar_url') else '아바타 없음(yaml 에 avatar_url)'}"
                         for cid, c in ctx.chars.items())
        rest = ", ".join(f"{n}(`{i}`)" for i, n in avail.items() if i not in ctx.chars) or "(없음)"
        await interaction.response.send_message(
            f"**이 서버의 캐릭터** ({len(ctx.chars)}명, ⭐=기본)\n{here}\n추가할 수 있는 캐릭터: {rest}", ephemeral=True)
        return
    if not character or character not in avail:
        await interaction.response.send_message("없는 캐릭터예요. 목록: " + ", ".join(sorted(avail)), ephemeral=True)
        return
    channel = interaction.channel
    if act == "add":
        if character in ctx.chars:
            await interaction.response.send_message("이미 이 서버에 있는 캐릭터예요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        c = await hub.add_character(ctx, character)
        extra = ""
        if channel is not None and isinstance(channel, (discord.TextChannel, discord.Thread)):
            if await get_webhook(channel, c.cid) is None:
                extra = "\n⚠ 이 채널에 웹훅을 만들 수 없어요. 봇에게 **웹훅 관리** 권한을 주세요 (그전에는 이름을 앞에 붙여서 말해요)."
        await interaction.followup.send(
            f"**{c.persona.name}** (`{c.cid}`) 을(를) 이 서버에 추가했어요. 지금 {len(ctx.chars)}명이 함께 있어요."
            f"{'' if (c.persona.data.get('avatar_url') or avatar_file(c.cid)) else chr(10) + '-# 프로필 사진은 `/캐릭터 동작:프로필 캐릭터:… 이미지:(첨부)` 로 바꿀 수 있어요.'}{extra}",
            ephemeral=True)
        if greet and c.persona.greeting and channel is not None:
            hub.use_char(c.cid)
            await send_bubbles(channel, [c.persona.greeting])
        return
    if act == "remove":
        if character not in ctx.chars:
            await interaction.response.send_message("이 서버에 없는 캐릭터예요.", ephemeral=True)
            return
        if not await hub.remove_character(ctx, character):
            await interaction.response.send_message("마지막 한 명은 뺄 수 없어요. 다른 캐릭터로 교체하세요.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"`{character}` 을(를) 뺐어요. (기분/호감도는 저장돼 있어서 다시 추가하면 이어져요) 남은 캐릭터: "
            + ", ".join(c.persona.name for c in ctx.chars.values()), ephemeral=True)
        return
    # ---- 교체 ----
    target = old or ctx.primary
    if target not in ctx.chars:
        await interaction.response.send_message("교체할 기존 캐릭터가 이 서버에 없어요. (`/캐릭터스왑 동작:목록`)", ephemeral=True)
        return
    if character in ctx.chars and character != target:
        await interaction.response.send_message("그 캐릭터는 이미 이 서버에 있어요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    c = await hub.replace_character(ctx, target, character)     # 이전 캐릭터의 기분/호감도는 저장하고, 새 캐릭터의 저장된 상태를 불러온다
    if clear_history:
        await memory.clear_all_history()
    bot.pending.clear()
    await interaction.followup.send(
        f"캐릭터를 **{c.persona.name}** (`{c.cid}`)(으)로 바꿨어요." + (" 대화 기록을 초기화했어요." if clear_history else ""),
        ephemeral=True)
    if greet and c.persona.greeting and channel is not None:
        hub.use_char(c.cid)
        await send_bubbles(channel, [c.persona.greeting])


# ---- 3-2. 캐릭터 (부르기/내보내기/목록 + 캐릭터끼리 대화 제어) ----
talking: set[int] = set()          # 지금 캐릭터끼리 대화 중인 채널


async def run_talk(channel, gid: int, starter: str, topic: str, rounds: int, uid: int, uname: str) -> None:
    """관리자가 시킨 캐릭터끼리 대화: starter 가 먼저 말을 꺼내고, 다른 캐릭터가 번갈아 rounds 번 이어 간다 (/캐릭터 동작:대화중지 로 멈춤)."""
    ch = channel.id
    talking.add(ch)
    prev: str | None = None
    last_text = ""
    said: list[str] = []                                      # 이번 캐릭터 대화에서 나온 말 (핑퐁 감지)
    stale = 0
    try:
        for i in range(rounds):
            hub.use(gid)
            if ch not in talking or bot.paused:
                break
            ctx = hub.ctx()
            if i == 0 or prev not in ctx.chars:
                cid = starter if starter in ctx.chars else next(iter(ctx.chars))
            else:
                cid = random.choice([c for c in ctx.chars if c != prev] or [prev])
            hub.use_char(cid)
            for _ in range(90):                                   # 사람 대화가 진행 중이면 끝나길 기다린다
                if ch not in bot.busy:
                    break
                await asyncio.sleep(1)
            if ch in bot.busy or ch not in talking:
                break
            bot.busy.add(ch)
            try:
                names = ", ".join(c.persona.name for k, c in ctx.chars.items() if k != cid)
                if i == 0 or prev is None:
                    text = f"(관리자가 캐릭터끼리 대화를 시켰다) 주제: {topic}" if topic else "(심심해서 같은 방 친구 캐릭터에게 말을 건다)"
                    note = (f"같은 방 친구 캐릭터({names})에게 먼저 말을 꺼내라. 사람에게 하는 말이 아니다."
                            + (f" 주제: {topic}" if topic else ""))
                    from_uid, from_name = uid, uname
                else:
                    pname = ctx.chars[prev].persona.name
                    text = last_text
                    note = peer_note(pname, topic)
                    from_uid, from_name = char_uid(prev), pname
                async with bot.gpu_lock:
                    async with channel.typing():
                        bubbles = await engine.reply(ch, from_uid, from_name, text, can_skip=False, priority=True, extra_note=note)
                if bubbles:
                    joined = " ".join(bubbles)
                    stale = stale + 1 if stale_talk(joined, said) else 0
                    if stale >= 2:                                 # 같은 말을 두 번 연속 주고받으면 대화를 자연스럽게 끝낸다
                        log.info("캐릭터 대화가 같은 말을 반복해서 멈춤 (channel=%s)", ch)
                        break
                    await send_bubbles(channel, bubbles)
                    bot.last_char[ch] = cid
                    prev, last_text = cid, joined
                    said.append(joined)
            finally:
                bot.busy.discard(ch)
            await asyncio.sleep(random.uniform(*[float(x) for x in mcfg["chain_delay_sec"]]))
    except Exception:  # noqa: BLE001
        log.exception("캐릭터 대화 실패 (channel=%s)", ch)
    finally:
        talking.discard(ch)
        bot.next_ok[ch] = max(bot.next_ok.get(ch, 0), time.monotonic() + 2.0)


AVATAR_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")


async def set_avatar(interaction: discord.Interaction, character: Optional[str], image: Optional[discord.Attachment]) -> None:
    """/캐릭터 동작:프로필 — 올린 사진을 512px 정사각형 PNG 로 저장하고(data/avatars/<id>.png), 캐릭터 웹훅 아바타를 바꾼다.
    이미지를 비우면 저장한 사진을 지워 원래(yaml avatar_url 또는 봇 프로필)로 되돌린다. 모든 서버에 공통."""
    ids = dict(Persona.available())
    cid = character or hub.ctx().primary
    if cid not in ids:
        await interaction.response.send_message("그런 캐릭터가 없어요. 캐릭터를 골라 주세요.", ephemeral=True)
        return
    name = ids[cid]
    if image is None:
        p = AVATAR_DIR / f"{cid}.png"
        p = p if p.is_file() else None
        if p:
            p.unlink(missing_ok=True)
        await interaction.response.send_message(
            f"**{name}** 프로필 사진을 원래대로 되돌렸어요." if p else f"**{name}** 은(는) 올린 프로필 사진이 없어요. `이미지`에 사진을 첨부해 주세요.",
            ephemeral=True)
        return
    ctype = (image.content_type or "").split(";")[0].lower()
    if ctype not in AVATAR_TYPES or image.size > 8_000_000:
        await interaction.response.send_message("png/jpg/webp/gif 이미지, 8MB 이하만 쓸 수 있어요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        raw = await image.read()
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(raw)).convert("RGBA")
        w, h = im.size
        s = min(w, h)
        top = 0 if h <= w else min(int(h * 0.08), h - s)            # 세로로 긴 그림은 얼굴이 있는 윗부분 쪽으로
        im = im.crop(((w - s) // 2, top, (w - s) // 2 + s, top + s)).resize((512, 512), Image.LANCZOS)
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        im.save(AVATAR_DIR / f"{cid}.png", optimize=True)
    except Exception as e:  # noqa: BLE001
        log.warning("프로필 사진 저장 실패 (%s): %s", cid, e)
        await interaction.followup.send("이미지를 읽지 못했어요. 다른 사진으로 해 주세요.", ephemeral=True)
        return
    note = ""
    if not mcfg.get("webhook", True):
        note = "\n-# 웹훅 설정이 꺼져 있어요. 웹훅을 켜면 서버 채널에서 이 사진을 사용해요."
    await interaction.followup.send(f"**{name}** 프로필 사진을 바꿨어요. 다음 말부터 적용돼요.{note}", ephemeral=True)


@bot.tree.command(name="캐릭터", description="캐릭터를 부르거나 내보내고, 캐릭터끼리 대화를 시작/중지해요")
@app_commands.describe(mode="부르기/내보내기/목록 · 대화시작/대화중지 · 상호작용켜기/끄기(캐릭터끼리 알아서 받아치기) · 상태",
                       character="부를/내보낼 캐릭터, 대화시작일 땐 먼저 말할 캐릭터 (생략하면 아무나)",
                       topic="대화시작할 때 이야기 주제 (생략하면 자유)", rounds="대화시작: 말을 주고받을 횟수 (기본 6, 최대 500)",
                       image="프로필: 새 프로필 사진 (png/jpg/webp/gif, 8MB 이하). 비우면 원래 사진으로 되돌림")
@app_commands.rename(mode="동작", character="캐릭터", topic="주제", rounds="횟수", image="이미지")
@app_commands.choices(mode=[
    app_commands.Choice(name="부르기", value="add"),
    app_commands.Choice(name="내보내기", value="remove"),
    app_commands.Choice(name="교체", value="replace"),
    app_commands.Choice(name="목록", value="list"),
    app_commands.Choice(name="대화시작", value="talk"),
    app_commands.Choice(name="대화중지", value="stop"),
    app_commands.Choice(name="상호작용켜기", value="auto_on"),
    app_commands.Choice(name="상호작용끄기", value="auto_off"),
    app_commands.Choice(name="상태", value="status"),
    app_commands.Choice(name="프로필", value="avatar"),
])
@app_commands.autocomplete(character=character_autocomplete)
async def character_cmd(
    interaction: discord.Interaction, mode: app_commands.Choice[str], character: Optional[str] = None,
    topic: Optional[str] = None, rounds: app_commands.Range[int, 2, 500] = 6,
    image: Optional[discord.Attachment] = None,
) -> None:
    act = mode.value
    ctx = hub.ctx()
    if act == "avatar":
        await set_avatar(interaction, character, image)
        return
    if act in ("add", "remove", "list", "replace"):
        if act != "list" and not character:
            await interaction.response.send_message("캐릭터를 골라 주세요. (`/캐릭터 동작:목록`으로 확인)", ephemeral=True)
            return
        await swap_character.callback(interaction, character, app_commands.Choice(name=act, value=act), None, True, True)
        return
    ch = interaction.channel
    if act == "status":
        names = ", ".join(c.persona.name + (" (기본)" if cid == ctx.primary else "") for cid, c in ctx.chars.items())
        await interaction.response.send_message(
            f"활성 캐릭터: {names or '없음'}\n상호작용(캐릭터끼리 받아치기): {'켜짐' if ctx.state.chain_auto else '꺼짐'}"
            f" · 지금 이 채널 캐릭터 대화: {'진행 중' if ch is not None and ch.id in talking else '없음'}", ephemeral=True)
        return
    if act in ("auto_on", "auto_off"):
        ctx.state.chain_auto = act == "auto_on"
        await memory.kv_set("chain_auto", "1" if ctx.state.chain_auto else "0")
        await interaction.response.send_message(
            "상호작용 **켜짐**: 한 캐릭터가 사람에게 대답하면 다른 캐릭터가 가끔 받아쳐요." if ctx.state.chain_auto
            else "상호작용 **꺼짐**: 캐릭터끼리 알아서 말을 주고받지 않아요. (`동작:대화시작`으로 직접 시킬 수는 있어요)", ephemeral=True)
        return
    if act == "stop":
        was = ch is not None and ch.id in talking
        if ch is not None:
            talking.discard(ch.id)
        await interaction.response.send_message("캐릭터 대화를 멈춰요." if was else "지금 진행 중인 캐릭터 대화가 없어요.", ephemeral=True)
        return
    # ---- 대화시작 ----
    if len(ctx.chars) < 2:
        await interaction.response.send_message("캐릭터가 한 명뿐이에요. 먼저 `/캐릭터 동작:부르기 캐릭터:…`로 불러 주세요.", ephemeral=True)
        return
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("서버 채팅 채널에서만 쓸 수 있어요.", ephemeral=True)
        return
    if ch.id in talking:
        await interaction.response.send_message("이미 캐릭터끼리 대화 중이에요. `동작:대화중지`로 멈출 수 있어요.", ephemeral=True)
        return
    if bot.paused:
        await interaction.response.send_message("지금 일시정지 상태예요. `/상태 동작:일시정지`로 다시 켜 주세요.", ephemeral=True)
        return
    if character and character not in ctx.chars:
        await interaction.response.send_message("그 캐릭터는 이 서버에 없어요. 먼저 부르기로 불러 주세요.", ephemeral=True)
        return
    starter = character or random.choice(list(ctx.chars))
    topic = (topic or "").strip()[:80]
    await interaction.response.send_message(
        f"{ctx.chars[starter].persona.name} 부터 캐릭터끼리 {rounds}번 이야기해요." + (f" 주제: {topic}" if topic else ""), ephemeral=True)
    asyncio.create_task(run_talk(ch, ctx.gid, starter, topic, int(rounds), interaction.user.id, interaction.user.display_name))


# ---- 4. 상태 (보기 + 일시정지 + 리로드 통합) ----
@bot.tree.command(name="상태", description="봇 상태를 보거나, 일시정지/재개, 설정 파일 리로드를 해요")
@app_commands.describe(action="보기(기본) / 일시정지·재개(전환) / 리로드(캐릭터·사람 파일 다시 읽기)")
@app_commands.rename(action="동작")
@app_commands.choices(action=[
    app_commands.Choice(name="보기", value="show"),
    app_commands.Choice(name="일시정지", value="pause"),
    app_commands.Choice(name="리로드", value="reload"),
])
async def status(interaction: discord.Interaction, action: Optional[app_commands.Choice[str]] = None) -> None:
    act = action.value if action else "show"
    if act == "pause":
        bot.paused = not bot.paused
        await memory.kv_set("paused", "1" if bot.paused else "0")
        if bot.paused:
            bot.pending.clear()
        await interaction.response.send_message(
            "⏸ 일시정지했어요. 이제 대답하지 않아요." if bot.paused else "▶ 다시 대화에 참여해요.", ephemeral=True)
        return
    if act == "reload":
        ctx_ = hub.ctx()
        for c in ctx_.chars.values():
            c.persona.reload()
        hub.refresh_peers(ctx_)
        n = engine.people.reload()
        err = ("\n형식 오류: " + "; ".join(engine.people.errors[:4])) if engine.people.errors else ""
        ew = engine.mood.profile.warnings
        err += ("\n감정 설정 경고: " + "; ".join(ew[:4])) if ew else ""
        await interaction.response.send_message(
            f"다시 불러왔어요. 캐릭터 **{persona.name}** (`{persona.char_id}`) · 감정 프리셋 `{engine.mood.profile.preset}`, people.txt {n}명.{err}", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        rows = await gpu_lines()
        gpu = "\n".join(
            f"  {m}: {s:.1f}GB (VRAM {v:.1f}GB, GPU 적재 {p:.0f}%)"
            + ("  ⚠ 일부가 CPU 에서 돌아 느립니다!" if p < 99 else "")
            for m, s, v, p in rows
        ) or "  (현재 로드된 모델 없음)"
    except Exception as e:  # noqa: BLE001
        gpu = f"  Ollama 연결 실패: {e}"
    total_msgs, n_ch = await memory.message_counts()
    chs = sorted(bot.channels)
    ch = " ".join(f"<#{c}>" for c in chs) or "(없음 — 멘션/DM만 반응)"
    ratios = " · ".join(
        f"<#{c}> {a.ratio:.2f}/{a.target:.2f}(사람{a.human}·봇{a.bot})" for c in chs for a in [ratio.adjust(c)]) or "-"
    pend = sum(len(v) for v in bot.pending.values())
    avg = llm.avg_stats()
    speed = (f"평균 {avg[0]:.1f}초 (모델로드 {avg[1]:.1f} · 프롬프트 {avg[2]:.1f} · 생성 {avg[3]:.1f}) — 최근 {len(llm.stats)}회"
             if avg else "아직 기록 없음")
    nf = len(engine.fights.active)
    lvl = engine.fights.cfg.get("level_text") or engine.fights.cfg["level"]
    chars_line = ", ".join(f"{c.persona.name}{'⭐' if cid == hub.ctx().primary else ''}" for cid, c in hub.ctx().chars.items())
    div_line = " / ".join(f"<#{c}> {engine.div.stats(c)}" for c in sorted(bot.channels)[:3]) or "기록 없음"
    await interaction.followup.send(
        f"**봇 상태** {'⏸ 일시정지' if bot.paused else '▶ 동작 중'}\n"
        f"- 캐릭터 **{persona.name}** (`{persona.char_id}`) · 모델 `{llm.model}` · 기분 {engine.mood.current().label}\n"
        f"- 서버 캐릭터({len(hub.ctx().chars)}명): {chars_line}\n"
        f"- 모델 점검: {'✅' if llm.model_ok else '⚠' if llm.model_ok is False else '(미확인)'} {trunc(llm.model_note, 300)}\n"
        f"- 응답 속도: {speed}\n"
        f"- 적극성 {engine.mood.engage:g} · 쿨다운 {bot.cooldown:g}초 · 채팅 비율(현재/목표): {ratios}\n"
        f"- 먼저 말걸기: {'켜짐' if bot.init_enabled else '꺼짐'} (10~24시) · 혼잣말 이벤트 {'켜짐' if bot.idle_enabled else '꺼짐'}\n"
        f"- 말싸움: {'켜짐' if engine.fights.cfg['enabled'] else '꺼짐'} (수위 {trunc(str(lvl), 30)}) · 진행 중 {nf}건\n"
        f"- 감정 프리셋 `{engine.mood.profile.preset}` · 웹 정보 {'켜짐' if engine.web.enabled else '꺼짐'}\n"
        f"- 반복 감시: {div_line}\n"
        f"- 서버 이모지 {len(emojis.custom)}개 · 이미지 인식 {'켜짐' if vcfg.get('enabled', True) else '꺼짐'}\n"
        f"- 대화 채널: {ch}\n"
        f"- 대기 메시지 {pend}개 · 저장 기록 {fmt(total_msgs)}개({n_ch}개 채널) · 가동 {fmt_uptime(time.time() - bot.started_at)} · 지연 {bot.latency * 1000:.0f}ms\n"
        f"- Ollama:\n{gpu}",
        ephemeral=True)


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


# ---- 사람 (호칭/이미지 목록·보기·리로드 + 기억 보기/초기화 통합. 호칭/이미지 수정은 config/people.txt 파일에서) ----
@bot.tree.command(name="사람", description="사람 설정(people.txt)과 캐릭터가 기억하는 내용을 보거나 초기화해요")
@app_commands.describe(action="목록/보기/리로드/기억보기/기억초기화", user="대상 유저 (보기·기억 동작)")
@app_commands.rename(action="동작", user="유저")
@app_commands.choices(action=[
    app_commands.Choice(name="목록", value="list"),
    app_commands.Choice(name="보기", value="show"),
    app_commands.Choice(name="리로드", value="reload"),
    app_commands.Choice(name="기억보기", value="mem"),
    app_commands.Choice(name="기억초기화", value="memreset"),
])
async def people_cmd(
    interaction: discord.Interaction, action: app_commands.Choice[str], user: Optional[discord.User] = None
) -> None:
    ppl = engine.people
    act = action.value
    if act == "reload":
        n = ppl.reload()
        msg = f"people.txt 를 다시 불러왔어요. ({n}명)"
        if ppl.errors:
            msg += "\n형식 오류:\n" + "\n".join(f"- {e}" for e in ppl.errors[:8])
        await interaction.response.send_message(msg[:1990], ephemeral=True)
        return
    if act in ("show", "mem", "memreset"):
        if user is None:
            await interaction.response.send_message("유저를 지정해주세요.", ephemeral=True)
            return
        if act == "memreset":
            await memory.reset_user(user.id)
            await interaction.response.send_message(f"{user.display_name} 님의 기억과 대화 기록을 지웠어요.", ephemeral=True)
            return
        if act == "mem":
            facts = await memory.get_facts(user.id, 40)
            body = "\n".join(f"• {f}" for f in facts) if facts else "아직 기억하는 게 없어요."
            await interaction.response.send_message(
                f"**{persona.name}이(가) 기억하는 {user.display_name}**\n{body}"[:1990], ephemeral=True)
            return
        name = ppl.names.get(user.id)
        view = ppl.views.get(user.id)
        aff = engine.mood.affinity(user.id)
        label, _ = engine.mood.aff_label(aff)
        score = engine.mood.aff100(aff)
        nfacts = len(await memory.get_facts(user.id, 200))
        await interaction.response.send_message(
            f"**{user.display_name}** (`{user.id}`)\n- 부르는 이름: {name[0] if name else '(없음, 닉네임 사용)'}\n"
            f"- 이미지: {view or '(없음)'}\n- 호감도: {score}점 {label} · 기억 {nfacts}개", ephemeral=True)
        return
    ids = sorted(set(ppl.names) | set(ppl.views))
    if not ids:
        await interaction.response.send_message(
            f"등록된 사람이 없어요. 서버의 `{ppl.path}` 파일에 한 줄씩 적으세요.\n"
            "형식: `유저아이디:캐릭터가부를이름:사람에대한AI의이미지`", ephemeral=True)
        return
    body = "\n".join(
        f"<@{uid}> → {(ppl.names.get(uid) or ('-',))[0]} / {ppl.views.get(uid, '(이미지 없음)')}" for uid in ids)
    await interaction.response.send_message(f"**people.txt ({len(ids)}명)**\n{body}"[:1990], ephemeral=True)


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
@app_commands.describe(action="보기/설정/초기화/목록", emotion="설정할 감정 (설정 시)", minutes="유지 시간(분, 기본 30)",
                       character="대상 캐릭터 (여러 캐릭터일 때, 생략하면 기본 캐릭터)")
@app_commands.rename(action="동작", emotion="감정", minutes="분", character="캐릭터")
@app_commands.choices(action=[
    app_commands.Choice(name="보기", value="show"),
    app_commands.Choice(name="설정", value="set"),
    app_commands.Choice(name="초기화", value="reset"),
    app_commands.Choice(name="목록", value="list"),
])
@app_commands.autocomplete(emotion=emotion_autocomplete, character=guild_char_autocomplete)
async def mood_cmd(
    interaction: discord.Interaction, action: app_commands.Choice[str],
    emotion: Optional[str] = None, minutes: Optional[app_commands.Range[int, 1, 720]] = None,
    character: Optional[str] = None,
) -> None:
    if character and character in hub.ctx().chars:
        hub.use_char(character)
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
        f"말투: {engine.mood.tone_of(e.id)}",
        "조절: " + " / ".join(snap["knobs"]),
        "마음속: " + (" / ".join(snap["thoughts"]) if snap["thoughts"] else "(특별한 일 없음)"),
        f"-# 타이핑 속도 x{e.speed:g} · 끼어들기 x{e.reply:g} · 쿨다운 x{e.cooldown:g}",
    ]
    await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)


LEVEL_DESC = {1: "순화된 말싸움만", 2: "가벼운 욕까지", 3: "센 욕설까지 (가족·외모·성적·장애·지역·혐오 비하와 위협은 계속 금지)"}


# ---- 적극성 (적극성 + 쿨다운 + 채팅 비율 통합) ----
@bot.tree.command(name="적극성", description="대화 적극성, 쿨다운, 채팅 비율을 한 번에 조절해요. 값을 생략하면 현재 설정을 보여줘요")
@app_commands.describe(level="적극성 0.5(느긋)~1.0(기본)~4.0(폭주)", cooldown="대답 후 다음 대답까지 기본 쿨다운(초, 0~600)",
                       chat_ratio="채팅 비율 중심값: 사람 메시지 1개당 봇 메시지 수 (0.3~4.0, 기본 1.25 → 1.0~1.5 랜덤)")
@app_commands.rename(level="단계", cooldown="쿨다운", chat_ratio="비율")
async def engagement_cmd(
    interaction: discord.Interaction,
    level: Optional[app_commands.Range[float, 0.5, 4.0]] = None,
    cooldown: Optional[app_commands.Range[int, 0, 600]] = None,
    chat_ratio: Optional[app_commands.Range[float, 0.3, 4.0]] = None,
) -> None:
    m = engine.mood
    msgs = []
    if level is not None:
        m.engage = float(level)
        await memory.kv_set("engagement", f"{m.engage:g}")
        m.boost_to_baseline()
        msgs.append(f"적극성 **{m.engage:g}**" + (" 🔥폭주 구간(GPU 사용 증가, 밀리면 낮추세요)" if m.engage > 2.0 else ""))
    if cooldown is not None:
        bot.cooldown = float(cooldown)
        await memory.kv_set("cooldown", str(cooldown))
        msgs.append(f"쿨다운 **{cooldown}초**")
    if chat_ratio is not None:
        ratio.set_center(float(chat_ratio))
        await memory.kv_set("ratio", f"{ratio.center:g}")
        msgs.append(f"채팅 비율 중심 **{ratio.center:g}** (목표는 {max(0.2, ratio.center - ratio.spread):.2f}~{ratio.center + ratio.spread:.2f} 사이 랜덤)")
    if msgs:
        await interaction.response.send_message("바꿨어요: " + " · ".join(msgs), ephemeral=True)
        return
    await interaction.response.send_message(
        f"**현재 설정**\n- 적극성 **{m.engage:g}** (에너지 바닥 {m.energy_floor * m.engage_amount():.2f}, "
        f"대답 확률 +{max(0.0, 0.2 * (m.engage - 1.0)):.2f}, 쿨다운 x{1 / max(0.5, m.engage):.2f})\n"
        f"- 기본 쿨다운 **{bot.cooldown:g}초** (+랜덤 최대 {int(gcfg['cooldown_jitter'] * 100)}%)\n"
        f"- 채팅 비율 중심 **{ratio.center:g}** (±{ratio.spread:g}), 최근 {int(ratio.window // 60)}분 기준으로 자동 보정", ephemeral=True)


LEVEL_DESC = {1: "순화된 말싸움만", 2: "가벼운 욕까지", 3: "욕설을 거침없이"}


# ---- 싸움 (유저를 주면 즉시 싸움 시작) ----
@bot.tree.command(name="싸움", description="유저를 고르면 즉시 싸움을 겁니다. 수위는 자유롭게 써요. 동작으로 켜기/끄기/중단/필터/상태")
@app_commands.describe(user="싸울 상대 (고르면 봇이 바로 먼저 시비를 걸어요)",
                       level_text="수위를 자유롭게 (예: 거칠게 욕하고 서럽게 따져). 1/2/3 은 프리셋. 유저 없이 쓰면 기본 수위로 저장",
                       action="상태(기본) / 켜기 / 끄기 / 중단(이 채널 싸움 종료) / 필터(출력 필터 전환)",
                       character="싸울 캐릭터 (여러 캐릭터일 때, 생략하면 기본 캐릭터)")
@app_commands.rename(user="유저", level_text="수위", action="동작", character="캐릭터")
@app_commands.autocomplete(character=guild_char_autocomplete)
@app_commands.choices(action=[
    app_commands.Choice(name="상태", value="status"),
    app_commands.Choice(name="켜기", value="on"),
    app_commands.Choice(name="끄기", value="off"),
    app_commands.Choice(name="중단", value="stop"),
    app_commands.Choice(name="필터", value="filter"),
])
async def fight_cmd(
    interaction: discord.Interaction,
    user: Optional[discord.User] = None,
    level_text: Optional[app_commands.Range[str, 1, 300]] = None,
    action: Optional[app_commands.Choice[str]] = None,
    character: Optional[str] = None,
) -> None:
    if character and character in hub.ctx().chars:
        hub.use_char(character)
    fm = engine.fights
    act = action.value if action else ""
    if act in ("on", "off"):
        fm.cfg["enabled"] = act == "on"
        await memory.kv_set("fight_enabled", "1" if fm.cfg["enabled"] else "0")
        if not fm.cfg["enabled"]:
            fm.stop_all()
        await interaction.response.send_message(
            "말싸움 기능을 켰어요." if fm.cfg["enabled"] else "말싸움 기능을 껐어요.", ephemeral=True)
        return
    if act == "filter":                 # 출력 금지어 필터 전환. 앞단 검열이 있으면 꺼두는 게 효율적
        fm.cfg["filter"] = not fm.cfg.get("filter", False)
        await memory.kv_set("fight_filter", "1" if fm.cfg["filter"] else "0")
        await interaction.response.send_message(
            "출력 필터를 **켰어요**. 금지 표현이 나오면 다시 생성해요." if fm.cfg["filter"]
            else "출력 필터를 **껐어요**. 검열 없이 감정 그대로 나가요.", ephemeral=True)
        return
    if act == "stop":
        n = sum(c.engine.fights.stop_all(interaction.channel_id) for c in hub.ctx().chars.values())
        await interaction.response.send_message(f"이 채널의 말싸움 {n}건을 중단했어요.", ephemeral=True)
        return

    if user is not None:                # ---- 즉시 싸움: 기분이 나빠진 채로 봇이 먼저 욕하며 시비를 건다 ----
        channel = interaction.channel
        if channel is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("텍스트 채널에서 실행해주세요.", ephemeral=True)
            return
        if not fm.cfg["enabled"]:
            await interaction.response.send_message("말싸움 기능이 꺼져 있어요. `/싸움 동작:켜기`", ephemeral=True)
            return
        if channel.id in bot.busy:
            await interaction.response.send_message("지금 이 채널에서 대답을 만드는 중이에요. 잠시 뒤에 다시 해주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        f = fm.start(channel.id, user.id, user.display_name)
        if level_text:
            f.level_text = level_text.strip()
        engine.mood.on_fight_start(user.id, engine.label(user.id, user.display_name))
        engine.mood.set_emotion("angry", 8)        # 기분이 즉시 나빠진다
        await run_fight_open(channel, f)
        shown = f.level_text or f"기본 수위({fm.cfg.get('level_text') or fm.cfg['level']})"
        await interaction.followup.send(f"{user.display_name} 님과 싸움을 시작했어요. 수위: {trunc(shown, 80)}", ephemeral=True)
        return

    if level_text:                      # 유저 없이 수위만: 기본 수위로 저장
        t = level_text.strip()
        fm.cfg["level_text"] = "" if t in ("1", "2", "3") else t
        if t in ("1", "2", "3"):
            fm.cfg["level"] = int(t)
        await memory.kv_set("fight_level_text", fm.cfg["level_text"])
        await memory.kv_set("fight_level", str(fm.cfg["level"]))
        await interaction.response.send_message(
            f"기본 수위를 바꿨어요: **{LEVEL_DESC[int(t)] if t in ('1', '2', '3') else trunc(t, 120)}**", ephemeral=True)
        return

    fights = [(c.persona.name, f) for c in hub.ctx().chars.values() for f in c.engine.fights.active.values()]
    body = "\n".join(
        f"<#{f.channel_id}> {cn} ↔ {f.user_name}: 라운드 {f.rounds} · 흐름 {f.advantage}" for cn, f in fights) or "(진행 중인 싸움 없음)"
    cur = fm.cfg.get("level_text") or f"{fm.cfg['level']} ({LEVEL_DESC[int(fm.cfg['level'])]})"
    await interaction.response.send_message(
        f"**말싸움** {'켜짐' if fm.cfg['enabled'] else '꺼짐'} · 기본 수위 {trunc(str(cur), 100)} · "
        f"출력 필터 {'켜짐' if fm.cfg.get('filter') else '꺼짐'}\n{body}\n-# `/싸움 유저:@누구` 로 즉시 싸움을 걸 수 있어요.", ephemeral=True)


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
        # 장시간 침묵 혼잣말 이벤트와 능동 대화(먼저 말걸기)를 함께 켜고 끈다
        bot.idle_enabled = bot.init_enabled = action.value == "on"
        await memory.kv_set("idle_enabled", "1" if bot.idle_enabled else "0")
        await memory.kv_set("init_enabled", "1" if bot.init_enabled else "0")
        await interaction.response.send_message(
            "먼저 말걸기(능동 대화 + 혼잣말 이벤트)를 켰어요." if bot.idle_enabled
            else "먼저 말걸기(능동 대화 + 혼잣말 이벤트)를 껐어요.", ephemeral=True)
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


# ---- 12. 웹 (인터넷 정보) ----
@bot.tree.command(name="웹", description="유행어/최신 정보 가져오기를 켜고 끄거나, 새로고침/현황을 봐요")
@app_commands.describe(action="보기(기본) / 켜기 / 끄기 / 새로고침")
@app_commands.rename(action="동작")
@app_commands.choices(action=[
    app_commands.Choice(name="보기", value="show"),
    app_commands.Choice(name="켜기", value="on"),
    app_commands.Choice(name="끄기", value="off"),
    app_commands.Choice(name="새로고침", value="refresh"),
])
async def web_cmd(interaction: discord.Interaction, action: Optional[app_commands.Choice[str]] = None) -> None:
    w = engine.web
    act = action.value if action else "show"
    if not w.allowed:
        await interaction.response.send_message("웹 검색은 설정에서 꺼져 있어요 (config/settings.yaml 의 web.allowed: true 로 바꾸고 재시작하면 다시 쓸 수 있어요).", ephemeral=True)
        return
    if act in ("on", "off"):
        w.set_enabled(act == "on")
        await memory.kv_set("web_enabled", "1" if w.enabled else "0")
        await interaction.response.send_message(f"웹 정보를 {'켰' if w.enabled else '껐'}어요.", ephemeral=True)
        return
    if act == "refresh":
        await interaction.response.defer(ephemeral=True)
        n = await w.refresh(engine.web_topics())
        await interaction.followup.send(f"새로 받은 항목 {n}개. {w.summary()}\n{w.status()}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"**웹 정보** {'켜짐' if w.enabled else '꺼짐'} · 방식 {w.cfg['provider']} · 캐시: {w.summary()}\n"
        f"사용 확률 {int(w.cfg['chance'] * 100)}% · 실시간 조회 {'허용' if w.cfg.get('realtime') else '안 함'}"
        f" (제한 {w.cfg['timeout_sec']}초, 분당 {w.cfg['per_min']}회)\n{w.status()}", ephemeral=True)


# ---- 12.5 통계 (응답 속도 · 메시지 처리 · 호감도/감정 그래프) ----
def _user_name(guild: discord.Guild | None, cid: str, uid: int) -> str:
    c = hub.ctxs.get(guild.id if guild else DM)
    ch = c.chars.get(cid) if c else None
    m = guild.get_member(uid) if guild else None
    nm = m.display_name if m else (ch.engine.names.get(uid, (None, None))[0] if ch else None)
    return str(nm or uid)


class StatsView(discord.ui.View):
    """관리자 전용 그래프 화면: 캐릭터/유저/기간을 고르고 버튼으로 호감도·감정 그래프와 변화 원인을 본다.
    응답은 전부 ephemeral(본인에게만 보임)이라 관계 정보가 공개 채널에 노출되지 않는다."""

    def __init__(self, gid: int, guild: discord.Guild | None, owner: int) -> None:
        super().__init__(timeout=300)
        self.gid, self.guild, self.owner = gid, guild, owner
        ctx = hub.ctxs.get(gid)
        self.chars = {cid: c.persona.name for cid, c in (ctx.chars.items() if ctx else [])}
        self.cid = next(iter(self.chars), "")
        self.uid: int | None = None
        self.days = 7
        self.add_item(self._char_select())
        self.add_item(self._days_select())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner or interaction.user.id not in ADMIN_IDS:
            await interaction.response.send_message("관리자만 쓸 수 있어요.", ephemeral=True)
            return False
        return True

    def _char_select(self) -> discord.ui.Select:
        opts = [discord.SelectOption(label=n[:90], value=cid, default=(cid == self.cid)) for cid, n in list(self.chars.items())[:25]]
        sel = discord.ui.Select(placeholder="캐릭터", options=opts or [discord.SelectOption(label="(캐릭터 없음)", value="-")], row=0)

        async def cb(i: discord.Interaction) -> None:
            self.cid, self.uid = sel.values[0], None
            await self.refresh_users(i)
        sel.callback = cb
        return sel

    def _days_select(self) -> discord.ui.Select:
        sel = discord.ui.Select(placeholder="기간", options=[
            discord.SelectOption(label=f"최근 {d}일", value=str(d), default=(d == self.days)) for d in (1, 3, 7, 14, 30)], row=2)

        async def cb(i: discord.Interaction) -> None:
            self.days = int(sel.values[0])
            await i.response.defer()
        sel.callback = cb
        return sel

    async def refresh_users(self, i: discord.Interaction) -> None:
        """선택한 캐릭터의 기록이 있는 유저 목록(최대 25)으로 유저 선택 메뉴를 다시 만든다."""
        ctx = hub.ctxs[self.gid]
        ids = await ctx.memory.affinity_users(self.cid)
        c = ctx.chars.get(self.cid)
        ids = list(dict.fromkeys(ids + (list(c.engine.mood.aff) if c else [])))[:25]
        for it in list(self.children):
            if isinstance(it, discord.ui.Select) and it.row == 1:
                self.remove_item(it)
        opts = [discord.SelectOption(label=_user_name(self.guild, self.cid, u)[:90], value=str(u)) for u in ids]
        usel = discord.ui.Select(placeholder="유저" if ids else "기록 있는 유저가 아직 없어요", row=1,
                                 options=opts or [discord.SelectOption(label="(없음)", value="0")])

        async def ucb(ii: discord.Interaction) -> None:
            self.uid = int(usel.values[0]) or None
            await ii.response.defer()
        usel.callback = ucb
        self.add_item(usel)
        for it in self.children:                                  # 캐릭터 선택 표시 갱신
            if isinstance(it, discord.ui.Select) and it.row == 0:
                for o in it.options:
                    o.default = (o.value == self.cid)
        await i.response.edit_message(view=self)

    def _since(self) -> float:
        return time.time() - self.days * 86400

    async def _need_user(self, i: discord.Interaction) -> bool:
        if not self.uid or not self.cid:
            await i.response.send_message("먼저 캐릭터와 유저를 골라 주세요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="호감도 그래프", style=discord.ButtonStyle.primary, row=3)
    async def aff_btn(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await self._need_user(i):
            return
        await i.response.defer(ephemeral=True, thinking=True)
        ctx = hub.ctxs[self.gid]
        hist = await ctx.memory.affinity_history(self.cid, self.uid, self._since())
        who = _user_name(self.guild, self.cid, self.uid)
        png = await asyncio.to_thread(statslib.affinity_graph, hist, f"{self.chars.get(self.cid, self.cid)} -> {who} 호감도", Mood.aff100)
        if png is None:
            await i.followup.send("그래프를 만들 수 없어요 (`pip install matplotlib` 필요).", ephemeral=True)
            return
        c = ctx.chars.get(self.cid)
        now = Mood.aff100(c.engine.mood.affinity(self.uid)) if c else None
        head = f"현재 호감도 **{now}** ({Mood.aff_stage(now)}) · " if now is not None else ""
        await i.followup.send(f"{head}기록 {len(hist)}건", file=discord.File(io.BytesIO(png), "affinity.png"), ephemeral=True)

    @discord.ui.button(label="감정 그래프", style=discord.ButtonStyle.primary, row=3)
    async def emo_btn(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        if not self.cid:
            await i.response.send_message("먼저 캐릭터를 골라 주세요.", ephemeral=True)
            return
        await i.response.defer(ephemeral=True, thinking=True)
        hist = await hub.ctxs[self.gid].memory.emotion_history(self.cid, self._since())
        png = await asyncio.to_thread(statslib.emotion_graph, hist, f"{self.chars.get(self.cid, self.cid)} 감정 변화",
                                      lambda e: EMO[e].label if e in EMO else e)
        if png is None:
            await i.followup.send("그래프를 만들 수 없어요 (`pip install matplotlib` 필요).", ephemeral=True)
            return
        await i.followup.send(f"감정 변화 {len(hist)}건", file=discord.File(io.BytesIO(png), "emotion.png"), ephemeral=True)

    @discord.ui.button(label="변화 원인", style=discord.ButtonStyle.secondary, row=3)
    async def why_btn(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await self._need_user(i):
            return
        hist = await hub.ctxs[self.gid].memory.affinity_history(self.cid, self.uid, self._since(), limit=400)
        rows = []
        for ts, o, n, why, emo in hist[-15:]:
            d = Mood.aff100(n) - Mood.aff100(o)
            rows.append(f"`{kst(ts)}` {Mood.aff100(o)}->{Mood.aff100(n)} ({'+' if d >= 0 else ''}{d}) {why} · {EMO[emo].label if emo in EMO else emo}")
        await i.response.send_message("**최근 호감도 변화 원인**\n" + ("\n".join(rows) or "기록이 없어요")[:1900], ephemeral=True)


@bot.tree.command(name="통계", description="응답 속도/메시지 처리 통계, 호감도·감정 그래프 (관리자, 본인에게만 보임)")
@app_commands.describe(mode="응답속도(P50/P95) · 메시지처리(상태별 건수) · 그래프(호감도/감정 화면) · 토큰(=/사용량)",
                       hours="응답속도/메시지처리: 최근 몇 시간 (기본 24)")
@app_commands.rename(mode="동작", hours="시간")
@app_commands.choices(mode=[
    app_commands.Choice(name="응답속도", value="latency"),
    app_commands.Choice(name="메시지처리", value="queue"),
    app_commands.Choice(name="그래프", value="graph"),
    app_commands.Choice(name="토큰", value="usage"),
])
async def stats_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str],
                    hours: app_commands.Range[int, 1, 720] = 24) -> None:
    act = mode.value
    if act == "usage":
        await usage_cmd.callback(interaction, None)
        return
    if act == "latency":
        rep = await asyncio.to_thread(statslib.latency_report, engine.tracer.files(), float(hours))
        await interaction.response.send_message((statslib.format_latency(rep) + "\n" + engine.corrector.summary())[:1990], ephemeral=True)
        return
    if act == "queue":
        c = await memory.msg_counts(time.time() - hours * 3600)
        total = sum(c.values()) or 1
        lines = [f"**최근 {hours}시간 메시지 처리**"] + [f"{k}: {v}건 ({v * 100 // total}%)" for k, v in sorted(c.items(), key=lambda kv: -kv[1])]
        await interaction.response.send_message("\n".join(lines) if c else "기록이 아직 없어요.", ephemeral=True)
        return
    gid = interaction.guild_id or DM
    view = StatsView(gid, interaction.guild, interaction.user.id)
    await interaction.response.send_message(
        "캐릭터를 고르면 유저 목록이 나와요. 유저와 기간을 고른 뒤 버튼을 눌러 주세요. (나에게만 보여요)", view=view, ephemeral=True)


# ---- 13. 로그 (디버깅용 상세 기록) ----
@bot.tree.command(name="로그", description="받은 메시지/대답 대상/프롬프트/답변 기록을 보거나 내보내요 (디버깅용)")
@app_commands.describe(action="보기(기본, 최근 응답) / 받은메시지 / 상세(기록 id) / 내보내기(파일) / 켜기 / 끄기 / 통계",
                       count="보기 개수 (기본 10, 최대 30)", user="이 유저와 관련된 기록만", trace_id="상세로 볼 기록 id (보기 목록의 [id])",
                       hours="내보내기 범위(시간, 기본 24)")
@app_commands.rename(action="동작", count="개수", user="유저", trace_id="기록id", hours="시간")
@app_commands.choices(action=[
    app_commands.Choice(name="보기", value="show"),
    app_commands.Choice(name="받은메시지", value="recv"),
    app_commands.Choice(name="상세", value="detail"),
    app_commands.Choice(name="내보내기", value="export"),
    app_commands.Choice(name="통계", value="stats"),
    app_commands.Choice(name="켜기", value="on"),
    app_commands.Choice(name="끄기", value="off"),
])
async def log_cmd(
    interaction: discord.Interaction, action: Optional[app_commands.Choice[str]] = None, count: int = 10,
    user: Optional[discord.User] = None, trace_id: Optional[str] = None, hours: int = 24,
) -> None:
    tr = engine.tracer
    act = action.value if action else "show"
    uid = user.id if user else None
    if act in ("on", "off"):
        tr.cfg["enabled"] = act == "on"
        await memory.kv_set("trace_enabled", "1" if tr.enabled else "0")
        await interaction.response.send_message(f"상세 기록을 {'켰' if tr.enabled else '껐'}어요. (저장 위치 `{tr.dir}`)", ephemeral=True)
        return
    if act == "export":
        await interaction.response.defer(ephemeral=True)
        data, n = tr.export(max(1, hours))
        if not n:
            await interaction.followup.send("내보낼 기록이 없어요.", ephemeral=True)
            return
        import io
        fname = f"trace-{datetime.now().strftime('%Y%m%d-%H%M')}.jsonl"
        await interaction.followup.send(f"최근 {hours}시간 기록 {n}줄 (JSONL, 한 줄에 사건 하나). 사용자 메시지가 들어 있으니 공유에 주의하세요.",
                                        file=discord.File(io.BytesIO(data), filename=fname), ephemeral=True)
        return
    if act == "stats":
        rows = tr.read(5000, since_ts=time.time() - 24 * 3600)
        kinds = Counter(r.get("kind") for r in rows)
        outcomes = Counter(r.get("outcome") for r in rows if r.get("kind") in ("reply", "initiate", "proactive"))
        recv = Counter(r.get("outcome") for r in rows if r.get("kind") == "recv")
        secs = [r["total_sec"] for r in rows if r.get("kind") == "reply" and r.get("outcome") == "replied" and "total_sec" in r]
        users = Counter(r.get("user_name") for r in rows if r.get("kind") == "recv").most_common(5)
        files = tr.files()
        size = sum(p.stat().st_size for p in files) / 1e6
        avg = f"{sum(secs) / len(secs):.1f}초 ({len(secs)}건)" if secs else "-"
        await interaction.response.send_message(
            f"**최근 24시간 기록** {'켜짐' if tr.enabled else '꺼짐'} · 파일 {len(files)}개 {size:.1f}MB (보관 {tr.cfg['retention_days']}일)\n"
            f"- 종류: {dict(kinds)}\n- 받은 메시지 처리: {dict(recv)}\n- 응답 결과: {dict(outcomes)}\n"
            f"- 평균 응답 생성 {avg}\n- 많이 말한 사람: {', '.join(f'{n}({c})' for n, c in users) or '-'}",
            ephemeral=True)
        return
    if act == "detail":
        if not trace_id:
            await interaction.response.send_message("`기록id` 에 [대괄호] 안의 id 를 넣어 주세요.", ephemeral=True)
            return
        rows = tr.read(1, trace_id=trace_id.strip("[] "))
        if not rows:
            await interaction.response.send_message("그 id 의 기록을 못 찾았어요.", ephemeral=True)
            return
        import io
        body = json.dumps(rows[0], ensure_ascii=False, indent=2)
        await interaction.response.send_message(
            f"기록 `{trace_id}` 상세 (프롬프트/후보 점수/LLM 원문/후처리 포함)",
            file=discord.File(io.BytesIO(body.encode("utf-8")), filename=f"trace-{trace_id}.json"), ephemeral=True)
        return
    n = max(1, min(30, count))
    kind = "recv" if act == "recv" else None
    rows = tr.read(n * 3 if kind is None else n, kind=kind, user=uid)
    if kind is None:
        rows = [r for r in rows if r.get("kind") in ("reply", "initiate", "proactive", "decision")][:n]
    if not rows:
        await interaction.response.send_message("기록이 없어요. (`/로그 동작:켜기` 로 켰는지 확인하세요)", ephemeral=True)
        return
    text = "\n".join(tracelib.summarize(r) for r in reversed(rows))
    await interaction.response.send_message(text[:1990] + "\n-# `/로그 동작:상세 기록id:...` 로 프롬프트와 후보 점수까지 볼 수 있어요.", ephemeral=True)


def run() -> None:
    bot.run(discord_token(), log_handler=None)


if __name__ == "__main__":
    run()
