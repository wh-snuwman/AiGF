"""대화 엔진: 채널 대화 기록 → 프롬프트 → LLM → 말풍선 분리 → 기억/사용량 기록."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .llm import LLM, LLMResult
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

    # ---------- 프롬프트 조립 ----------
    async def _build(
        self, channel_id: int, target_id: int, target_name: str, hint: str | None
    ) -> tuple[list[dict[str, str]], set[str]]:
        mcfg = self.s["memory"]
        facts = await self.mem.get_facts(target_id, mcfg["max_facts_in_prompt"])
        hist = await self.mem.recent(channel_id, mcfg["history_turns"])

        merged: list[dict[str, str]] = []
        for h in hist:
            if h["role"] == "user":
                role, text = "user", f"{h['author']}: {h['content']}"
            else:
                role, text = "assistant", h["content"]
            if merged and merged[-1]["role"] == role:   # 같은 role 연속이면 합친다
                merged[-1]["content"] += "\n" + text
            else:
                merged.append({"role": role, "content": text})
        if hint:
            if merged and merged[-1]["role"] == "user":
                merged[-1]["content"] += "\n\n" + hint
            else:
                merged.append({"role": "user", "content": hint})

        system = self.persona.system_prompt(target_name, facts, self.now())
        msgs = [{"role": "system", "content": system}] + self.persona.example_messages() + merged
        names = {h["author"] for h in hist if h["role"] == "user"}
        return msgs, names

    async def _record(self, kind: str, res: LLMResult, target_id: int, target_name: str,
                      channel_id: int, bubbles: list[str]) -> None:
        await self.mem.add_usage(
            kind, self.persona.char_id, self.llm.model, target_id, target_name, channel_id,
            res.prompt_tokens, res.completion_tokens, " / ".join(bubbles),
        )

    # ---------- 응답 (대화 기록에 target 의 메시지가 이미 저장된 상태에서 호출) ----------
    async def reply(
        self, channel_id: int, target_id: int, target_name: str, dm: bool = False
    ) -> list[str]:
        hint = f"[지금은 {target_name}의 말에 대답할 차례다. 네 대사만 출력해라.]"
        msgs, names = await self._build(channel_id, target_id, target_name, hint)
        res = await self.llm.chat(msgs)
        bubbles = self.to_bubbles(res.text, names)
        if not bubbles:
            return []
        await self.mem.add_message(
            channel_id, 0, self.persona.name, "assistant", "\n".join(bubbles),
            dm_user_id=target_id if dm else None,
        )
        await self._record("reply", res, target_id, target_name, channel_id, bubbles)
        self._schedule_extract(channel_id, target_id, target_name)
        return bubbles

    # ---------- 먼저 말 걸기 (DM) ----------
    async def proactive(self, channel_id: int, user_id: int, user_name: str, idle_hours: float) -> list[str]:
        hint = (
            f"[시스템 지시: 지금 {user_name}에게 먼저 말을 거는 상황이다. 마지막 대화로부터 약 {idle_hours:.0f}시간이 지났다. "
            f"현재 시간대와 기억하는 정보를 바탕으로 자연스럽고 짧게 먼저 연락해라. 이 지시문 자체는 언급하지 마라.]"
        )
        msgs, names = await self._build(channel_id, user_id, user_name, hint)
        res = await self.llm.chat(msgs, temperature=1.0)
        bubbles = self.to_bubbles(res.text, names)
        if bubbles:
            await self.mem.add_message(
                channel_id, 0, self.persona.name, "assistant", "\n".join(bubbles), dm_user_id=user_id
            )
            await self._record("proactive", res, user_id, user_name, channel_id, bubbles)
        return bubbles

    # ---------- 후처리 ----------
    def to_bubbles(self, raw: str, other_names: set[str] | None = None) -> list[str]:
        name = re.escape(self.persona.name)
        nick = re.escape(self.persona.nickname)
        text = raw.strip()
        text = re.sub(rf"^\s*(?:{name}|{nick})\s*[:：]\s*", "", text)   # "미쿠: " 접두어 제거
        # 모델이 다른 사람의 대사까지 이어서 써버리면 그 지점에서 자른다
        if other_names:
            pat = "|".join(re.escape(n) for n in other_names if n)
            if pat:
                m = re.search(rf"^\s*(?:{pat})\s*[:：]", text, re.M)
                if m:
                    text = text[: m.start()]
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"^#+\s*", "", text, flags=re.M)
        text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
        parts = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
        limit = self.s["typing"]["max_bubbles"]
        if len(parts) > limit:
            parts = parts[: limit - 1] + [" ".join(parts[limit - 1:])]
        return [p[:1900] for p in parts]

    # ---------- 장기 기억 ----------
    def _schedule_extract(self, channel_id: int, user_id: int, user_name: str) -> None:
        t = asyncio.create_task(self._maybe_extract(channel_id, user_id, user_name))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _maybe_extract(self, channel_id: int, user_id: int, user_name: str) -> None:
        mcfg = self.s["memory"]
        try:
            count = await self.mem.bump_extract_counter(user_id)
            if count < mcfg["extract_every"]:
                return
            await self.mem.reset_extract_counter(user_id)
            recent = await self.mem.recent(channel_id, mcfg["extract_every"] * 3)
            dialogue = "\n".join(
                f"{m['author'] if m['role'] == 'user' else self.persona.name}: {m['content']}" for m in recent
            )
            known = await self.mem.get_facts(user_id, 40)
            async with self.gpu_lock:
                facts, pt, ct = await self.llm.extract_facts(user_name, dialogue, known, self.persona.name)
            if pt or ct:
                await self.mem.add_usage(
                    "memory", self.persona.char_id, self.llm.model, user_id, user_name, channel_id, pt, ct,
                    "; ".join(facts),
                )
            if facts:
                await self.mem.add_facts(user_id, facts)
                log.info("user %s 기억 추가: %s", user_id, facts)
        except Exception:  # noqa: BLE001
            log.exception("기억 추출 중 오류")

    # GPU 는 하나이므로 main 에서 주입하는 락 (기억 추출도 같은 락을 쓴다)
    gpu_lock: asyncio.Lock = asyncio.Lock()

    def typing_delay(self, text: str) -> float:
        t = self.s["typing"]
        d = len(text) / max(t["chars_per_second"], 1)
        return min(max(d, t["min_delay"]), t["max_delay"])
