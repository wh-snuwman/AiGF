"""SQLite 저장소: 채널별 대화 기록, 유저별 장기 기억, 토큰 사용량, 봇 상태(kv)."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,          -- 봇 발화는 0
    author TEXT NOT NULL,
    role TEXT NOT NULL,                -- user / assistant
    content TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    fact TEXT NOT NULL,
    ts REAL NOT NULL,
    UNIQUE(user_id, fact)
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    name TEXT,
    dm_channel_id INTEGER,
    last_user_ts REAL,
    last_bot_ts REAL,
    since_extract INTEGER NOT NULL DEFAULT 0,
    proactive_day TEXT,
    proactive_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,                -- reply / memory / proactive
    character TEXT,
    model TEXT,
    user_id INTEGER NOT NULL DEFAULT 0, -- 답변 대상 유저
    user_name TEXT,
    channel_id INTEGER,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    snippet TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage(user_id, id);

CREATE TABLE IF NOT EXISTS user_names (
    user_id INTEGER PRIMARY KEY,
    real_name TEXT NOT NULL,           -- 닉네임 대신 쓸 진짜 이름
    call_name TEXT                     -- 부를 때 쓸 호칭 (예: 민수야). 비우면 real_name
);

CREATE TABLE IF NOT EXISTS user_views (
    user_id INTEGER PRIMARY KEY,
    view TEXT NOT NULL                 -- 캐릭터가 그 사람에 대해 가진 인식/이미지 (관리자가 수정)
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- 호감도 변경 이력 (그래프/원인 조회용). aff 값은 내부값(-1~1)
CREATE TABLE IF NOT EXISTS affinity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    character TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    old REAL NOT NULL,
    new REAL NOT NULL,
    reason TEXT,
    emotion TEXT
);
CREATE INDEX IF NOT EXISTS idx_aff_log ON affinity_log(character, user_id, ts);

-- 감정 변화 이력 (캐릭터별)
CREATE TABLE IF NOT EXISTS emotion_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    character TEXT NOT NULL,
    emotion TEXT NOT NULL,
    intensity REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_emo_log ON emotion_log(character, ts);

-- 메시지 처리 상태 (재시작 복구 / 중복 제거). status: RECEIVED QUEUED PROCESSING RESPONDED SKIPPED FAILED
CREATE TABLE IF NOT EXISTS msg_status (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL,
    updated REAL NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_status ON msg_status(status, updated);
"""


class Memory:
    def __init__(self, db_path: str, max_facts_per_user: int = 200) -> None:
        self.path = Path(db_path)
        self.max_facts = max_facts_per_user
        self.db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self._migrate_old()
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def _migrate_old(self) -> None:
        """초기 버전(1:1 DM 전용) 스키마가 있으면 대화 기록 테이블만 버리고 새로 만든다."""
        assert self.db
        cur = await self.db.execute("PRAGMA table_info(messages)")
        cols = {r[1] for r in await cur.fetchall()}
        if cols and "channel_id" not in cols:
            await self.db.execute("DROP TABLE messages")
            await self.db.execute("DROP TABLE IF EXISTS users")

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    # ---------- kv (봇 상태) ----------
    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        assert self.db
        cur = await self.db.execute("SELECT value FROM kv WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

    async def kv_set(self, key: str, value: str) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.db.commit()

    # ---------- 실명 등록 ----------
    async def set_real_name(self, user_id: int, real_name: str, call_name: str | None) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO user_names(user_id, real_name, call_name) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET real_name=excluded.real_name, call_name=excluded.call_name",
            (user_id, real_name, call_name),
        )
        await self.db.commit()

    async def del_real_name(self, user_id: int) -> bool:
        assert self.db
        cur = await self.db.execute("DELETE FROM user_names WHERE user_id=?", (user_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def all_real_names(self) -> dict[int, tuple[str, str | None]]:
        assert self.db
        cur = await self.db.execute("SELECT user_id, real_name, call_name FROM user_names")
        return {u: (r, c) for u, r, c in await cur.fetchall()}

    # ---------- 캐릭터가 가진 '인식' ----------
    async def set_view(self, user_id: int, view: str) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO user_views(user_id, view) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET view=excluded.view", (user_id, view))
        await self.db.commit()

    async def del_view(self, user_id: int) -> bool:
        assert self.db
        cur = await self.db.execute("DELETE FROM user_views WHERE user_id=?", (user_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def all_views(self) -> dict[int, str]:
        assert self.db
        cur = await self.db.execute("SELECT user_id, view FROM user_views")
        return {u: v for u, v in await cur.fetchall()}

    async def last_message_ts(self, channel_id: int) -> float | None:
        assert self.db
        cur = await self.db.execute("SELECT MAX(ts) FROM messages WHERE channel_id=?", (channel_id,))
        return (await cur.fetchone())[0]

    # ---------- 대화 기록 ----------
    async def add_message(
        self,
        channel_id: int,
        user_id: int,
        author: str,
        role: str,
        content: str,
        dm_user_id: int | None = None,
    ) -> None:
        assert self.db
        now = time.time()
        await self.db.execute(
            "INSERT INTO messages(channel_id, user_id, author, role, content, ts) VALUES (?,?,?,?,?,?)",
            (channel_id, user_id, author, role, content, now),
        )
        if role == "user":
            await self.db.execute(
                "INSERT INTO users(user_id, name, last_user_ts) VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, last_user_ts=excluded.last_user_ts",
                (user_id, author, now),
            )
        elif dm_user_id:
            await self.db.execute(
                "INSERT INTO users(user_id, last_bot_ts) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_bot_ts=excluded.last_bot_ts",
                (dm_user_id, now),
            )
        await self.db.commit()

    async def recent(self, channel_id: int, limit: int) -> list[dict[str, Any]]:
        assert self.db
        cur = await self.db.execute(
            "SELECT role, user_id, author, content, ts FROM messages WHERE channel_id=? ORDER BY id DESC LIMIT ?",
            (channel_id, limit),
        )
        rows = await cur.fetchall()
        return [
            {"role": r, "user_id": u, "author": a, "content": c, "ts": t}
            for r, u, a, c, t in reversed(rows)
        ]

    async def clear_all_history(self) -> None:
        assert self.db
        await self.db.execute("DELETE FROM messages")
        await self.db.commit()

    async def message_counts(self) -> tuple[int, int]:
        """(전체 메시지 수, 채널 수)"""
        assert self.db
        cur = await self.db.execute("SELECT COUNT(*), COUNT(DISTINCT channel_id) FROM messages")
        row = await cur.fetchone()
        return row[0], row[1]

    # ---------- 장기 기억 ----------
    async def get_facts(self, user_id: int, limit: int | None = None) -> list[str]:
        assert self.db
        sql = "SELECT fact FROM facts WHERE user_id=? ORDER BY ts DESC"
        params: tuple = (user_id,)
        if limit:
            sql += " LIMIT ?"
            params = (user_id, limit)
        cur = await self.db.execute(sql, params)
        return [r[0] for r in await cur.fetchall()]

    async def add_facts(self, user_id: int, facts: list[str]) -> None:
        assert self.db
        now = time.time()
        for f in facts:
            f = f.strip()
            if f:
                await self.db.execute(
                    "INSERT INTO facts(user_id, fact, ts) VALUES (?,?,?) "
                    "ON CONFLICT(user_id, fact) DO UPDATE SET ts=excluded.ts",
                    (user_id, f, now),
                )
        await self.db.execute(
            "DELETE FROM facts WHERE user_id=? AND id NOT IN "
            "(SELECT id FROM facts WHERE user_id=? ORDER BY ts DESC LIMIT ?)",
            (user_id, user_id, self.max_facts),
        )
        await self.db.commit()

    async def bump_extract_counter(self, user_id: int) -> int:
        assert self.db
        await self.db.execute(
            "INSERT INTO users(user_id, since_extract) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET since_extract=since_extract+1",
            (user_id,),
        )
        await self.db.commit()
        cur = await self.db.execute("SELECT since_extract FROM users WHERE user_id=?", (user_id,))
        return (await cur.fetchone())[0]

    async def reset_extract_counter(self, user_id: int) -> None:
        assert self.db
        await self.db.execute("UPDATE users SET since_extract=0 WHERE user_id=?", (user_id,))
        await self.db.commit()

    async def reset_user(self, user_id: int) -> None:
        """그 유저의 기억과 대화 기록을 삭제 (사용량 기록은 유지)."""
        assert self.db
        await self.db.execute("DELETE FROM facts WHERE user_id=?", (user_id,))
        await self.db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
        await self.db.execute("UPDATE users SET since_extract=0 WHERE user_id=?", (user_id,))
        await self.db.commit()

    # ---------- DM / 먼저 말걸기 ----------
    async def set_dm_channel(self, user_id: int, channel_id: int) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO users(user_id, dm_channel_id) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET dm_channel_id=excluded.dm_channel_id",
            (user_id, channel_id),
        )
        await self.db.commit()

    async def proactive_candidates(self) -> list[dict[str, Any]]:
        assert self.db
        cur = await self.db.execute(
            "SELECT user_id, name, dm_channel_id, last_user_ts, last_bot_ts, proactive_day, proactive_count "
            "FROM users WHERE dm_channel_id IS NOT NULL AND last_user_ts IS NOT NULL"
        )
        keys = ["user_id", "name", "dm_channel_id", "last_user_ts", "last_bot_ts", "proactive_day", "proactive_count"]
        return [dict(zip(keys, r)) for r in await cur.fetchall()]

    async def mark_proactive(self, user_id: int, day: str, count: int) -> None:
        assert self.db
        await self.db.execute(
            "UPDATE users SET proactive_day=?, proactive_count=? WHERE user_id=?",
            (day, count, user_id),
        )
        await self.db.commit()

    # ---------- 호감도/감정 이력 ----------
    async def log_affinity(self, character: str, user_id: int, old: float, new: float, reason: str, emotion: str) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO affinity_log(ts, character, user_id, old, new, reason, emotion) VALUES (?,?,?,?,?,?,?)",
            (time.time(), character, user_id, old, new, reason[:80], emotion))
        await self.db.execute("DELETE FROM affinity_log WHERE ts < ?", (time.time() - 90 * 86400,))     # 90일 보존
        await self.db.commit()

    async def affinity_history(self, character: str, user_id: int, since: float = 0.0, limit: int = 2000) -> list[tuple]:
        """[(ts, old, new, reason, emotion)] 오래된 순"""
        assert self.db
        cur = await self.db.execute(
            "SELECT ts, old, new, reason, emotion FROM affinity_log WHERE character=? AND user_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",
            (character, user_id, since, limit))
        return list(reversed(await cur.fetchall()))

    async def log_emotion(self, character: str, emotion: str, intensity: float) -> None:
        assert self.db
        await self.db.execute("INSERT INTO emotion_log(ts, character, emotion, intensity) VALUES (?,?,?,?)",
                              (time.time(), character, emotion, round(intensity, 3)))
        await self.db.execute("DELETE FROM emotion_log WHERE ts < ?", (time.time() - 30 * 86400,))      # 30일 보존
        await self.db.commit()

    async def emotion_history(self, character: str, since: float = 0.0, limit: int = 3000) -> list[tuple]:
        """[(ts, emotion, intensity)] 오래된 순"""
        assert self.db
        cur = await self.db.execute(
            "SELECT ts, emotion, intensity FROM emotion_log WHERE character=? AND ts>=? ORDER BY ts DESC LIMIT ?",
            (character, since, limit))
        return list(reversed(await cur.fetchall()))

    async def affinity_users(self, character: str) -> list[int]:
        assert self.db
        cur = await self.db.execute("SELECT DISTINCT user_id FROM affinity_log WHERE character=?", (character,))
        return [r[0] for r in await cur.fetchall()]

    # ---------- 메시지 상태 (재시작 복구 / 중복 제거) ----------
    async def msg_seen(self, message_id: int, channel_id: int, user_id: int) -> bool:
        """처음 보는 메시지면 RECEIVED 로 등록하고 False, 이미 있으면 True (중복)."""
        assert self.db
        now = time.time()
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO msg_status(message_id, channel_id, user_id, status, ts, updated) VALUES (?,?,?,?,?,?)",
            (message_id, channel_id, user_id, "RECEIVED", now, now))
        await self.db.commit()
        return cur.rowcount == 0

    async def msg_set(self, message_id: int, status: str, note: str = "", attempt: bool = False) -> None:
        assert self.db
        await self.db.execute(
            "UPDATE msg_status SET status=?, updated=?, note=?, attempts=attempts+? WHERE message_id=?",
            (status, time.time(), note[:80], 1 if attempt else 0, message_id))
        await self.db.commit()

    async def msg_unfinished(self, max_age_sec: float = 900, max_attempts: int = 3) -> list[tuple[int, int, int, int]]:
        """재시작 뒤 이어서 처리할 메시지: [(message_id, channel_id, user_id, attempts)] 오래된 순"""
        assert self.db
        cur = await self.db.execute(
            "SELECT message_id, channel_id, user_id, attempts FROM msg_status "
            "WHERE status IN ('QUEUED','PROCESSING') AND ts>=? AND attempts<? ORDER BY ts",
            (time.time() - max_age_sec, max_attempts))
        return list(await cur.fetchall())

    async def msg_counts(self, since: float = 0.0) -> dict[str, int]:
        assert self.db
        cur = await self.db.execute("SELECT status, COUNT(*) FROM msg_status WHERE ts>=? GROUP BY status", (since,))
        return {r[0]: r[1] for r in await cur.fetchall()}

    async def msg_cleanup(self, days: int = 7) -> None:
        assert self.db
        await self.db.execute("DELETE FROM msg_status WHERE ts < ?", (time.time() - days * 86400,))
        await self.db.commit()

    # ---------- 토큰 사용량 ----------
    async def add_usage(
        self,
        kind: str,
        character: str,
        model: str,
        user_id: int,
        user_name: str,
        channel_id: int | None,
        prompt_tokens: int,
        completion_tokens: int,
        snippet: str = "",
    ) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO usage(ts, kind, character, model, user_id, user_name, channel_id, "
            "prompt_tokens, completion_tokens, snippet) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), kind, character, model, user_id, user_name, channel_id,
             prompt_tokens, completion_tokens, snippet[:300]),
        )
        await self.db.commit()

    async def usage_total(self) -> dict[str, Any]:
        assert self.db

        async def agg(where: str = "", params: tuple = ()) -> tuple[int, int, int]:
            cur = await self.db.execute(
                f"SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) "
                f"FROM usage {where}", params)
            return await cur.fetchone()

        now = time.time()
        out: dict[str, Any] = {
            "all": await agg(),
            "day": await agg("WHERE ts>?", (now - 86400,)),
            "week": await agg("WHERE ts>?", (now - 7 * 86400,)),
        }
        cur = await self.db.execute(
            "SELECT kind, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens) FROM usage GROUP BY kind")
        out["by_kind"] = await cur.fetchall()
        cur = await self.db.execute(
            "SELECT character, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens) FROM usage "
            "GROUP BY character ORDER BY SUM(prompt_tokens+completion_tokens) DESC")
        out["by_character"] = await cur.fetchall()
        cur = await self.db.execute("SELECT MIN(ts) FROM usage")
        out["since"] = (await cur.fetchone())[0]
        return out

    async def usage_by_user(self, limit: int = 15) -> list[tuple]:
        assert self.db
        cur = await self.db.execute(
            "SELECT user_id, MAX(user_name), COUNT(*), SUM(prompt_tokens), SUM(completion_tokens) "
            "FROM usage WHERE user_id!=0 GROUP BY user_id "
            "ORDER BY SUM(prompt_tokens+completion_tokens) DESC LIMIT ?", (limit,))
        return await cur.fetchall()

    async def usage_user_detail(self, user_id: int, limit: int = 10) -> dict[str, Any]:
        assert self.db
        cur = await self.db.execute(
            "SELECT MAX(user_name), COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), "
            "MIN(ts), MAX(ts) FROM usage WHERE user_id=?", (user_id,))
        summary = await cur.fetchone()
        cur = await self.db.execute(
            "SELECT ts, kind, character, channel_id, prompt_tokens, completion_tokens, snippet "
            "FROM usage WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return {"summary": summary, "recent": await cur.fetchall()}
