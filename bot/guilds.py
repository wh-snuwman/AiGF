"""서버(길드)별 데이터 분리 + 서버마다 여러 캐릭터.

서버마다 독립된 저장소와 상태를 가진다. 서로 섞이지 않는다.
  data/guilds/<서버id>/memory.db    대화 기록, 유저 기억(facts), 사용량, 봇 상태(kv: 캐릭터 목록/채널/쿨다운/적극성/기분/호감도/웹 캐시...)
  data/guilds/<서버id>/logs/        상세 기록(trace JSONL)
  config/people/<서버id>.txt        그 서버의 사람 이름/인식 (처음 만들 때 config/people.txt 를 복사해서 시작)
  data/guilds/dm/                   DM 전용 (서버 id 대신 'dm')
서버마다 캐릭터, 감정, 채널, 쿨다운, 적극성, 채팅 비율, 싸움 설정, 말 반복 감시(다양성), 웹 캐시가 따로 동작한다.
(LLM 모델과 GPU 락, 서버 이모지 목록처럼 서버와 무관한 것만 공유한다.)

한 서버에는 캐릭터가 여러 명 있을 수 있다 (kv 'characters' = 캐릭터 id 목록, 첫 번째가 기본 캐릭터).
캐릭터마다 persona/engine(기분, 호감도, 싸움, 반복 감시, 웹 캐시)이 따로이고, 대화 기록/유저 기억/채팅 비율/채널은 서버가 공유한다.

코드에서는 '현재 서버'와 '현재 캐릭터'를 contextvar 로 들고 다니고, main.py 의 engine/memory/persona/ratio 는 현재 서버(의 현재 캐릭터)의
객체로 연결되는 프록시다. 이벤트/명령/루프가 시작될 때 hub.use(서버id) 로 맞추고, 말할 캐릭터는 hub.use_char(id) 로 고른다.
"""
from __future__ import annotations

import contextvars
import copy
import json
import logging
import shutil
import zlib
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
_char: contextvars.ContextVar[str | None] = contextvars.ContextVar("char_id", default=None)
DM = 0                                         # 서버가 아닌 곳(DM)의 가상 서버 id


def char_uid(char_id: str) -> int:
    """캐릭터가 '말하는 상대'로 등장할 때 쓰는 가상의 유저 id (음수라서 실제 디스코드 유저와 겹치지 않는다)."""
    return -(zlib.crc32(char_id.encode("utf-8")) % 900_000_000) - 1


@dataclass
class GuildState:
    paused: bool = False
    idle_enabled: bool = True
    init_enabled: bool = True
    cooldown: float = 18.0
    channels: set[int] = field(default_factory=set)


@dataclass
class CharCtx:
    cid: str
    persona: Persona
    engine: ChatEngine


@dataclass
class GuildCtx:
    gid: int
    name: str
    dir: Path
    memory: Memory
    ratio: RatioControl
    state: GuildState
    chars: dict[str, CharCtx] = field(default_factory=dict)
    primary: str = ""

    # ---- 현재 캐릭터 ----
    def char(self, cid: str | None = None) -> CharCtx:
        c = self.chars.get(cid or _char.get() or "") or self.chars.get(self.primary)
        if c is None:
            c = next(iter(self.chars.values()))
        return c

    @property
    def persona(self) -> Persona:
        return self.char().persona

    @property
    def engine(self) -> ChatEngine:
        return self.char().engine

    def names(self) -> dict[str, str]:
        return {cid: c.persona.name for cid, c in self.chars.items()}


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
        self.on_create: Callable[[GuildCtx], None] | None = None   # 서버 저장소/캐릭터가 만들어질 때 (이모지 등 연결)
        self.legacy_path: Path | None = None             # 서버별 분리 이전의 단일 DB (처음 한 번만 서버별로 나눠 가진다)
        self.legacy_open = False
        self._locks: dict[int, Any] = {}

    # ---------- 현재 서버 / 캐릭터 ----------
    def use(self, gid: int | None) -> None:
        """현재 서버를 정한다 (현재 캐릭터는 그 서버의 기본 캐릭터로 되돌린다)."""
        _current.set(gid or DM)
        _char.set(None)

    def use_char(self, cid: str | None) -> None:
        _char.set(cid)

    @staticmethod
    def current_id() -> int:
        return _current.get()

    @staticmethod
    def current_char_id() -> str | None:
        return _char.get()

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
        ratio = RatioControl(s.get("ratio", {}))
        ctx = GuildCtx(gid, name, path.parent, mem, ratio,
                       GuildState(idle_enabled=bool(self.defaults["idle"]), init_enabled=bool(self.defaults["init"]),
                                  cooldown=float(self.defaults["cooldown"])))
        ctx._settings = s                                             # type: ignore[attr-defined]
        for cid in await self._saved_chars(mem, s):
            await self._add_char(ctx, cid, persist=False)
        ctx.primary = next(iter(ctx.chars))
        self.ctxs[gid] = ctx
        await self._load_state(ctx, fresh)
        self.refresh_peers(ctx)
        if self.on_create:
            self.on_create(ctx)
        log.info("서버 저장소 준비: %s (%s) → %s · 캐릭터 %s", name, dir_name(gid), path,
                 ", ".join(c.persona.name for c in ctx.chars.values()))
        return ctx

    async def _saved_chars(self, mem: Memory, s: dict) -> list[str]:
        """서버에 저장된 캐릭터 목록 (없으면 예전 'character' 설정 → settings 의 기본 캐릭터 순)."""
        available = [i for i, _ in Persona.available()]
        ids: list[str] = []
        raw = await mem.kv_get("characters")
        if raw:
            try:
                ids = [str(x) for x in json.loads(raw)]
            except ValueError:
                ids = []
        if not ids:
            ids = [await mem.kv_get("character") or s.get("character") or ""]
        out: list[str] = []
        for i in ids:
            if i in available and i not in out:
                out.append(i)
        return out or available[:1]

    async def _add_char(self, ctx: GuildCtx, cid: str, persist: bool = True) -> CharCtx:
        s = copy.deepcopy(getattr(ctx, "_settings", self.settings))
        persona = Persona(cid)
        engine = ChatEngine(s, persona, self.llm, ctx.memory)
        engine.people.path = self.people_path(ctx.gid)
        await engine.load_names()
        c = CharCtx(persona.char_id, persona, engine)
        ctx.chars[c.cid] = c
        if persist:
            await self._save_chars(ctx)
            self.refresh_peers(ctx)
            self._apply_shared(ctx, c)
            if self.on_create:
                self.on_create(ctx)
        return c

    async def _save_chars(self, ctx: GuildCtx) -> None:
        await ctx.memory.kv_set("characters", json.dumps(list(ctx.chars)))
        await ctx.memory.kv_set("character", ctx.primary)            # 예전 버전과의 호환

    async def add_character(self, ctx: GuildCtx, cid: str) -> CharCtx:
        if cid in ctx.chars:
            return ctx.chars[cid]
        return await self._add_char(ctx, cid)

    async def remove_character(self, ctx: GuildCtx, cid: str) -> bool:
        """캐릭터를 뺀다 (기분/호감도는 DB 에 남아서 다시 추가하면 이어진다). 마지막 한 명은 뺄 수 없다."""
        if cid not in ctx.chars or len(ctx.chars) <= 1:
            return False
        c = ctx.chars[cid]
        c.engine.mood._save()
        c.engine.fights.stop_all()
        del ctx.chars[cid]
        if ctx.primary == cid:
            ctx.primary = next(iter(ctx.chars))
        await self._save_chars(ctx)
        self.refresh_peers(ctx)
        return True

    async def replace_character(self, ctx: GuildCtx, old: str, new: str) -> CharCtx:
        """기본 캐릭터 교체 (예전 /캐릭터스왑). 같은 자리(순서)에 새 캐릭터를 넣는다."""
        if new in ctx.chars and new != old:
            raise ValueError("이미 이 서버에 있는 캐릭터예요")
        order = list(ctx.chars)
        if old in ctx.chars:
            ctx.chars[old].engine.mood._save()
            ctx.chars[old].engine.fights.stop_all()
        c = await self._add_char(ctx, new, persist=False)
        if old in ctx.chars and old != new:
            del ctx.chars[old]
        # 순서 유지
        idx = order.index(old) if old in order else 0
        order = [x for x in order if x != old]
        order.insert(idx, c.cid)
        ctx.chars = {cid: ctx.chars[cid] for cid in order if cid in ctx.chars}
        if ctx.primary == old or ctx.primary not in ctx.chars:
            ctx.primary = c.cid
        await self._save_chars(ctx)
        self.refresh_peers(ctx)
        self._apply_shared(ctx, c)
        if self.on_create:
            self.on_create(ctx)
        return c

    def refresh_peers(self, ctx: GuildCtx) -> None:
        """각 캐릭터에게 '같은 방에 있는 다른 캐릭터' 이름을 알려 준다 (프롬프트/이름 필터에서 사용)."""
        for cid, c in ctx.chars.items():
            c.engine.peers = [o.persona.name for oid, o in ctx.chars.items() if oid != cid]

    # ---------- 서버 공통 설정 동기화 ----------
    SHARED_FIGHT = ("enabled", "filter", "level", "level_text")

    def _apply_shared(self, ctx: GuildCtx, target: CharCtx, src: CharCtx | None = None) -> None:
        """서버 공통 설정(적극성, 싸움 기본값, 상세 기록/웹 켜기·끄기)을 src(없으면 기본 캐릭터)에서 target 으로 복사한다."""
        src = src or ctx.chars.get(ctx.primary)
        if src is None or src is target:
            return
        target.engine.mood.engage = src.engine.mood.engage
        for k in self.SHARED_FIGHT:
            if k in src.engine.fights.cfg:
                target.engine.fights.cfg[k] = src.engine.fights.cfg[k]
        target.engine.tracer.cfg["enabled"] = src.engine.tracer.cfg.get("enabled", True)
        target.engine.web.cfg["enabled"] = src.engine.web.cfg.get("enabled", True)

    def sync_peers(self, ctx: GuildCtx | None = None) -> None:
        """명령이 현재 캐릭터에 바꾼 서버 공통 설정을 같은 서버의 다른 캐릭터에도 맞춘다."""
        ctx = ctx or self.ctx()
        src = ctx.char()
        for c in ctx.chars.values():
            self._apply_shared(ctx, c, src)

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
        """서버별 kv 에서 상태를 복원한다. 서버 공통 설정은 모든 캐릭터에 적용한다."""
        m, st = ctx.memory, ctx.state
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
        rt = await m.kv_get("ratio")
        if rt:
            ctx.ratio.set_center(float(rt))
        ie2 = await m.kv_get("init_enabled")
        if ie2 is not None:
            st.init_enabled = ie2 == "1"
        te = await m.kv_get("trace_enabled")
        eg = await m.kv_get("engagement")
        flt = await m.kv_get("fight_level_text")
        ff = await m.kv_get("fight_filter")
        fl = await m.kv_get("fight_level")
        fe = await m.kv_get("fight_enabled")
        for c in ctx.chars.values():
            e = c.engine
            if te is not None:
                e.tracer.cfg["enabled"] = te == "1"
            if eg:
                e.mood.engage = max(0.5, min(4.0, float(eg)))
            if flt is not None:
                e.fights.cfg["level_text"] = flt
            if ff is not None:
                e.fights.cfg["filter"] = ff == "1"
            if fl in ("1", "2", "3"):
                e.fights.cfg["level"] = int(fl)
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
    """main.py 의 engine / memory / persona / ratio 자리에 놓는 객체: 접근할 때마다 '현재 서버(의 현재 캐릭터)'의 진짜 객체로 연결된다."""

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
