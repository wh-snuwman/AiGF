"""대화 엔진: 프롬프트 조립 → LLM 호출 → 말풍선 분리 → 기억 갱신."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .llm import LLM
from .memory import Memory
from .persona import Persona

log = logging.getLogger("chat")


class ChatEngine:
    def __init__(self, settings: dict, persona: Persona, llm: LLM, memory: Memory) -> None:
        self.s = settings
        self.persona = persona
        self.llm = llm
        self.mem = memory
        self.tz = ZoneInfo(settings.get("timezone", "Asia/Seoul"))
        self._bg: set[asyncio.Task] = set()

    def now(self) -> datetime:
        return datetime.now(self.tz)

    async def _build(self, user_id: int, user_name: str, extra_user: str | None) -> list[dict[str, str]]:
        mcfg = self.s["memory"]
        facts = await self.mem.get_facts(user_id, mcfg["max_facts_in_prompt"])
        history = await self.mem.recent(user_id, mcfg["history_turns"])
        system = self.persona.system_prompt(user_name, facts, self.now())
        msgs = [{"role": "system", "content": system}]
        msgs += self.persona.example_messages()
        msgs += history
        if extra_user:
            msgs.append({"role": "user", "content": extra_user})
        return msgs

    # ---------- 일반 응답 ----------
    async def reply(self, user_id: int, user_name: str, text: str) -> list[str]:
        await self.mem.add_message(user_id, "user", text)
        msgs = await self._build(user_id, user_name, None)
        raw = await self.llm.chat(msgs)
        bubbles = self.to_bubbles(raw)
        if not bubbles:
            bubbles = ["…으응? 잠깐 멍해졌어. 다시 말해줄래?"]
        await self.mem.add_message(user_id, "assistant", "\n".join(bubbles))
        self._schedule_extract(user_id)
        return bubbles

    # ---------- 먼저 말 걸기 ----------
    async def proactive(self, user_id: int, user_name: str, idle_hours: float) -> list[str]:
        title = self.persona.user_title
        hint = (
            f"[시스템 지시: 지금 {title}에게 먼저 말을 거는 상황이다. 마지막 대화로부터 약 {idle_hours:.0f}시간이 지났다. "
            f"현재 시간대와 기억하는 정보를 바탕으로 자연스럽고 짧게 먼저 연락해라. "
            f"예: 안부, 밥/수면 챙기기, 보고 싶다는 은근한 표현, 오늘 있었던 일 공유. 이 지시문 자체는 언급하지 마라.]"
        )
        msgs = await self._build(user_id, user_name, hint)
        raw = await self.llm.chat(msgs, temperature=1.0)
        bubbles = self.to_bubbles(raw)
        if bubbles:
            await self.mem.add_message(user_id, "assistant", "\n".join(bubbles))
        return bubbles

    # ---------- 후처리 ----------
    def to_bubbles(self, raw: str) -> list[str]:
        name = re.escape(self.persona.name)
        nick = re.escape(self.persona.data.get("nickname", self.persona.name))
        text = raw.strip()
        text = re.sub(rf"^\s*(?:{name}|{nick})\s*[:：]\s*", "", text)   # "미나: " 접두어 제거
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)                    # 굵게 제거
        text = re.sub(r"^#+\s*", "", text, flags=re.M)                  # 헤더 제거
        text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)             # 불릿 제거
        parts = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
        limit = self.s["typing"]["max_bubbles"]
        if len(parts) > limit:  # 초과분은 마지막 말풍선에 합친다
            parts = parts[: limit - 1] + [" ".join(parts[limit - 1:])]
        return [p[:1900] for p in parts]

    # ---------- 장기 기억 ----------
    def _schedule_extract(self, user_id: int) -> None:
        t = asyncio.create_task(self._maybe_extract(user_id))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _maybe_extract(self, user_id: int) -> None:
        mcfg = self.s["memory"]
        try:
            count = await self.mem.bump_extract_counter(user_id)
            if count < mcfg["extract_every"]:
                return
            await self.mem.reset_extract_counter(user_id)
            recent = await self.mem.recent(user_id, mcfg["extract_every"] * 2)
            title = self.persona.user_title
            dialogue = "\n".join(
                f"{title if m['role'] == 'user' else self.persona.name}: {m['content']}" for m in recent
            )
            known = await self.mem.get_facts(user_id, 40)
            facts = await self.llm.extract_facts(title, dialogue, known, self.persona.name)
            if facts:
                await self.mem.add_facts(user_id, facts)
                log.info("user %s 기억 추가: %s", user_id, facts)
        except Exception:  # noqa: BLE001
            log.exception("기억 추출 중 오류")

    def typing_delay(self, text: str) -> float:
        t = self.s["typing"]
        d = len(text) / max(t["chars_per_second"], 1)
        return min(max(d, t["min_delay"]), t["max_delay"])
