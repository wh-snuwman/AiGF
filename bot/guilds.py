"""서버(길드)별 데이터 분리.

서버마다 독립된 저장소와 상태를 가진다. 서로 섞이지 않는다.
  data/guilds/<서버id>/memory.db    대화 기록, 유저 기억(facts), 사용량, 봇 상태(kv: 캐릭터/채널/쿨다운/적극성/기분/호감도/웹 캐시...)
  data/guilds/<서버id>/logs/        상세 기록(trace JSONL)
  config/people/<서버id>.txt        그 서버의 사람 이름/인식 (처음 만들 때 config/people.txt 를 복사해서 시작)
  data/guilds/dm/                   DM 전용 (서버 id 대신 'dm')
서버마다 캐릭터, 감정, 채널, 쿨다운, 적극성, 채팅 비율, 싸움 설정, 말 반복 감시(다양성), 웹 캐시가 따로 동작한다.
(LLM 모델과 GPU 락, 서버 이모지 목록처럼 서버와 무관한 것만 공유한다.)

코드에서는 '현재 서버'를 contextvar 로 들고 다니고, main.py 의 engine/memory/persona/ratio 는 현재 서버의 객체로
연결되는 프록시다. 이벤트/명령/루프가 시작될 때 hub.use(서버id) 로 맞춘다.
"""
from __future__ import annotations

import contextvars
import copy
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .chat import ChatEngine
from .config import CONFIG_DIR, ROOT
from .memory import Memory
from .persona import Persona
from .ratio import RatioControl

log = logging.getLogger("guilds")

_current: contextvars.ContextVar[int] = contextvars.ContextVar("guild_id", default=0)
DM = 0                                         # 서버가 아닌 곳(DM)의 가상 서버 id


@dataclass
class GuildState:
    paused: bool = False
    idle_enabled: bool = True
    init_enabled: bool = True
    cooldown: float = 18.0
    channels: set[int] = field(default_factory=set)


@dataclass
class GuildCtx:
    gid: int
    name: str
    dir: Path
    memory: Memory
    persona: Persona
    engine: ChatEngine
    ratio: RatioControl
    state: GuildState


def dir_name(gid: int) -> str:
    return "dm" if gid == DM else str(gid)


class Hub:
    def __init__(self, settings: dict, llm, defaults: dict | None = None, data_dir: Path | None = None,
                 people_dir: Path | None = None) -> None:
        self.settings = settings
        self.llm = llm
        self.defaults = {"idle": True, "init": True, "cooldown": 18.0, "channel_ids": [], **(defaults or {})}
        self.root = data_dir or (ROOT / "data" / "guilds")
        self.people_dir = people_dir or (CONFIG_DIR / "people")
        self.ctxs: dict[int, GuildCtx] = {}
        self.chan_guild: dict[int, int] = {}             # 채널 id → 서버 id (루프가 채널에서 서버를 찾을 때)
        self.owner_of: Callable[[int], int | None] = lambda cid: None   # 채널 id → 서버 id (디스코드 캐시), main 이 설정
        self.is_dm: Callable[[int], bool] = lambda cid: False      # 채널 id 가 DM 채널인가 (디스코드 캐시), main 이 설정
        self.on_create: Callable[[GuildCtx], None] | None = None
        self.legacy_path: Path | None = None             # 서버별 분리 이전의 단일 DB (처음 한 번만 서버별로 나눠 가진다)
        self.legacy_open = False
        self._locks: dict[int, Any] = {}

    # ---------- 현재 서버 ----------
    def use(self, gid: int | None) -> None:
        _current.set(gid or DM)

    @staticmethod
    def current_id() -> int:
        return _current.get()

    def ctx(self, gid: int | None = None) -> GuildCtx:
        g = _current.get() if gid is None else gid
        c = self.ctxs.get(g)
        if c is None:                                    # 아직 준비되지 않은 서버면 DM → 첫 번째 서버 순으로 대체
            c = self.ctxs.get(DM) or next(iter(self.ctxs.values()), None)
        if c is None:
            raise RuntimeError("서버 저장소가 아직 준비되지 않았어요")
        return c

    def gids(self) -> list[int]:
        return list(self.ctxs)

    def guild_of(self, channel_id: int) -> int:
        return self.chan_guild.get(channel_id, DM)

    def all_channels(self) -> list[tuple[int, int]]:
        return [(g, c) for g, ctx in self.ctxs.items() for c in sorted(ctx.state.channels)]

    # ---------- 만들기 ----------
    def db_path(self, gid: int) -> Path:
        return self.root / dir_name(gid) / "memory.db"

    def people_path(self, gid: int) -> Path:
        """그 서버의 people 파일. 없으면 공용 config/people.txt 를 복사해서 시작한다."""
        p = self.people_dir / f"{dir_name(gid)}.txt"
        base = CONFIG_DIR / "people.txt"
        if not p.exists() and base.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(base, p)
        return p

    async def ensure(self, gid: int | None, name: str = "") -> GuildCtx:
        gid = gid or DM
        c = self.ctxs.get(gid)
        if c is not None:
            return c
        import asyncio
        lock = self._locks.setdefault(gid, asyncio.Lock())
        async with lock:
            if gid in self.ctxs:
                return self.ctxs[gid]
            return await self._create(gid, name or ("DM" if gid == DM else str(gid)))

    async def _create(self, gid: int, name: str) -> GuildCtx:
        path = self.db_path(gid)
        fresh = not path.exists()
        if fresh and self.legacy_open and self.legacy_path and self.legacy_path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.legacy_path, path)                  # 이전 단일 DB 를 시작점으로 (아래에서 이 서버 것만 남김)
            await self._prune_legacy(path, gid)
        s = copy.deepcopy(self.settings)                              # 서버마다 설정 사본 (명령으로 바꾼 값이 다른 서버에 번지지 않게)
        s.setdefault("trace", {})["dir"] = str((path.parent / "logs").resolve())
        mem = Memory(str(path), s["memory"]["max_facts_per_user"])
        await mem.init()
        persona = Persona(s.get("character", ""))
        engine = ChatEngine(s, persona, self.llm, mem)
        engine.people.path = self.people_path(gid)
        ratio = RatioControl(s.get("ratio", {}))
        ctx = GuildCtx(gid, name, path.parent, mem, persona, engine, ratio,
                       GuildState(idle_enabled=bool(self.defaults["idle"]), init_enabled=bool(self.defaults["init"]),
                                  cooldown=float(self.defaults["cooldown"])))
        self.ctxs[gid] = ctx
        await self._load_state(ctx, fresh)
        await engine.load_names()
        if self.on_create:
            self.on_create(ctx)
        log.info("서버 저장소 준비: %s (%s) → %s", name, dir_name(gid), path)
        return ctx

    async def _prune_legacy(self, path: Path, gid: int) -> None:
        """복사한 이전 DB 에서 다른 서버 채널의 기록(메시지/사용량)을 지운다. 유저 기억/설정은 그대로 이어받는다."""
        import aiosqlite
        async with aiosqlite.connect(path) as db:
            rows = await db.execute_fetchall("SELECT DISTINCT channel_id FROM messages")
            dm_rows = await db.execute_fetchall("SELECT dm_channel_id FROM users WHERE dm_channel_id IS NOT NULL")
            dm_chans = {r[0] for r in dm_rows}                      # DM 채널은 users 표에 기록돼 있다
            drop = []
            for (cid,) in rows:
                if gid == DM:                                         # DM 저장소: DM 채널 기록만 (소속 불명 채널은 제외, 백업에는 남음)
                    if cid not in dm_chans and not self.is_dm(cid):
                        drop.append(cid)
                elif self.owner_of(cid) != gid:
                    drop.append(cid)
            for i in range(0, len(drop), 500):
                part = drop[i:i + 500]
                q = ",".join("?" * len(part))
                await db.execute(f"DELETE FROM messages WHERE channel_id IN ({q})", part)
                await db.execute(f"DELETE FROM usage WHERE channel_id IN ({q})", part)
            await db.commit()
            log.info("이전 DB 분리: %s 에서 다른 곳 채널 %d개의 기록 제외", dir_name(gid), len(drop))

    async def _load_state(self, ctx: GuildCtx, fresh: bool) -> None:
        """서버별 kv 에서 상태를 복원한다. (예전 bot.load_state)"""
        m, e, st = ctx.memory, ctx.engine, ctx.state
        st.paused = (await m.kv_get("paused", "0")) == "1"
        ie = await m.kv_get("idle_enabled")
        if ie is not None:
            st.idle_enabled = ie == "1"
        cd = await m.kv_get("cooldown")
        if cd:
            st.cooldown = float(cd)
        ch = await m.kv_get("channels")
        if ch:
            chans = {int(c) for c in json.loads(ch)}
        else:
            chans = {int(c) for c in self.defaults["channel_ids"]}
        if ctx.gid == DM:
            chans = set()
        else:                                                       # 이 서버에 속한 채널만 (다른 서버 채널 id 가 섞여 있어도 걸러낸다)
            chans = {c for c in chans if self.owner_of(c) in (ctx.gid, None)}
        st.channels = chans
        for c in chans:
            self.chan_guild[c] = ctx.gid
        char = await m.kv_get("character")
        if char:
            ctx.persona.load(char)
        te = await m.kv_get("trace_enabled")
        if te is not None:
            e.tracer.cfg["enabled"] = te == "1"
        eg = await m.kv_get("engagement")
        if eg:
            e.mood.engage = max(0.5, min(4.0, float(eg)))
        flt = await m.kv_get("fight_level_text")
        if flt is not None:
            e.fights.cfg["level_text"] = flt
        rt = await m.kv_get("ratio")
        if rt:
            ctx.ratio.set_center(float(rt))
        ie2 = await m.kv_get("init_enabled")
        if ie2 is not None:
            st.init_enabled = ie2 == "1"
        ff = await m.kv_get("fight_filter")
        if ff is not None:
            e.fights.cfg["filter"] = ff == "1"
        fl = await m.kv_get("fight_level")
        if fl in ("1", "2", "3"):
            e.fights.cfg["level"] = int(fl)
        fe = await m.kv_get("fight_enabled")
        if fe is not None:
            e.fights.cfg["enabled"] = fe == "1"

    async def finish_legacy(self) -> None:
        """처음 시작할 때 모든 서버 저장소를 만든 뒤, 이전 단일 DB 를 백업 이름으로 옮겨 다시 쓰이지 않게 한다."""
        self.legacy_open = False
        p = self.legacy_path
        if p and p.exists():
            bak = p.with_name(p.name + ".legacy-backup")
            try:
                p.replace(bak)
                log.info("이전 단일 DB 는 서버별로 나눈 뒤 백업으로 보관: %s", bak)
            except OSError:
                log.exception("이전 DB 백업 이동 실패")

    async def save_channels(self, gid: int | None = None) -> None:
        ctx = self.ctx(gid)
        await ctx.memory.kv_set("channels", json.dumps(sorted(ctx.state.channels)))
        for c in ctx.state.channels:
            self.chan_guild[c] = ctx.gid

    async def close(self) -> None:
        for c in self.ctxs.values():
            await c.memory.close()


class Proxy:
    """main.py 의 engine / memory / persona / ratio 자리에 놓는 객체: 접근할 때마다 '현재 서버'의 진짜 객체로 연결된다."""

    def __init__(self, hub: Hub, attr: str) -> None:
        object.__setattr__(self, "_hub", hub)
        object.__setattr__(self, "_attr", attr)

    def _target(self):
        return getattr(object.__getattribute__(self, "_hub").ctx(), object.__getattribute__(self, "_attr"))

    def __getattr__(self, k: str):
        return getattr(self._target(), k)

    def __setattr__(self, k: str, v) -> None:
        setattr(self._target(), k, v)

    def __repr__(self) -> str:
        return f"<Proxy {object.__getattribute__(self, '_attr')}>"
