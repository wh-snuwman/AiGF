"""SQLite 기반 대화 기록 + 장기 기억 저장소."""
from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    fact TEXT NOT NULL,
    ts REAL NOT NULL,
    UNIQUE(user_id, fact)
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    dm_channel_id INTEGER,
    last_user_ts REAL,
    last_bot_ts REAL,
    since_extract INTEGER NOT NULL DEFAULT 0,
    proactive_day TEXT,
    proactive_count INTEGER NOT NULL DEFAULT 0
);
"""


class Memory:
    def __init__(self, db_path: str, max_facts_per_user: int = 200) -> None:
        self.path = Path(db_path)
        self.max_facts = max_facts_per_user
        self.db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    # ---------- 대화 기록 ----------
    async def add_message(self, user_id: int, role: str, content: str) -> None:
        assert self.db
        now = time.time()
        await self.db.execute(
            "INSERT INTO messages(user_id, role, content, ts) VALUES (?,?,?,?)",
            (user_id, role, content, now),
        )
        col = "last_user_ts" if role == "user" else "last_bot_ts"
        await self.db.execute(
            f"INSERT INTO users(user_id, {col}) VALUES (?, ?) "
            f"ON CONFLICT(user_id) DO UPDATE SET {col}=excluded.{col}",
            (user_id, now),
        )
        await self.db.commit()

    async def recent(self, user_id: int, limit: int) -> list[dict[str, str]]:
        assert self.db
        cur = await self.db.execute(
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cur.fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    async def message_count(self, user_id: int) -> int:
        assert self.db
        cur = await self.db.execute("SELECT COUNT(*) FROM messages WHERE user_id=?", (user_id,))
        return (await cur.fetchone())[0]

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
        # 오래된 기억 정리
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

    # ---------- 유저 / 먼저 말걸기 ----------
    async def set_dm_channel(self, user_id: int, channel_id: int) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO users(user_id, dm_channel_id) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET dm_channel_id=excluded.dm_channel_id",
            (user_id, channel_id),
        )
        await self.db.commit()

    async def proactive_candidates(self) -> list[dict]:
        assert self.db
        cur = await self.db.execute(
            "SELECT user_id, dm_channel_id, last_user_ts, last_bot_ts, proactive_day, proactive_count "
            "FROM users WHERE dm_channel_id IS NOT NULL AND last_user_ts IS NOT NULL"
        )
        keys = ["user_id", "dm_channel_id", "last_user_ts", "last_bot_ts", "proactive_day", "proactive_count"]
        return [dict(zip(keys, r)) for r in await cur.fetchall()]

    async def mark_proactive(self, user_id: int, day: str, count: int) -> None:
        assert self.db
        await self.db.execute(
            "UPDATE users SET proactive_day=?, proactive_count=? WHERE user_id=?",
            (day, count, user_id),
        )
        await self.db.commit()

    # ---------- 초기화 ----------
    async def reset_user(self, user_id: int, keep_facts: bool = False) -> None:
        assert self.db
        await self.db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
        if not keep_facts:
            await self.db.execute("DELETE FROM facts WHERE user_id=?", (user_id,))
        await self.db.execute("UPDATE users SET since_extract=0 WHERE user_id=?", (user_id,))
        await self.db.commit()
