"""대화 엔진: 채널 대화 기록 + 참여자/상황 분석 → 프롬프트 → LLM → 말풍선 분리 → 기억/사용량 기록."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from .llm import LLM, LLMResult
from .memory import Memory
from .persona import Persona

log = logging.getLogger("chat")


def clean_name(name: str) -> str:
    """'SNUWMAN [AnA],' 같은 디스코드 표시 이름에서 부르기 좋은 이름만 남긴다."""
    s = re.sub(r"[\[\(\{【《<][^\]\)\}】》>]*[\]\)\}】》>]", "", name)
    s = re.sub(r"[^\w\s\-.]", "", s)           # 이모지/특수문자 제거 (한글, 영문, 숫자는 \w 에 포함)
    s = re.sub(r"\s+", " ", s).strip(" ._-")
    return s or name.strip()


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

    # ---------- 상황 분석 ----------
    async def _scene(self, hist: list[dict]) -> str:
        """참여자, 각자에 대한 기억, 최근 발언 흐름, 분위기를 요약해서 프롬프트에 넣는다."""
        users = [h for h in hist if h["role"] == "user"]
        if not users:
            return ""
        counts: Counter[int] = Counter(h["user_id"] for h in users)
        names = {h["user_id"]: clean_name(h["author"]) for h in users}

        lines = ["참여자 (최근 발언 많은 순):"]
        for uid, n in counts.most_common(5):
            facts = await self.mem.get_facts(uid, 3)
            tail = f" / 기억: {'; '.join(facts)}" if facts else ""
            lines.append(f"- {names[uid]} ({n}회 발언){tail}")

        flow: list[str] = []
        for h in users[-6:]:
            nm = names[h["user_id"]]
            if not flow or flow[-1] != nm:
                flow.append(nm)
        lines.append("최근 발언 흐름: " + " → ".join(flow))

        now_ts = time.time()
        recent = sum(1 for h in hist if now_ts - h["ts"] < 300)
        if recent >= 10:
            mood = "대화가 매우 활발함 (짧게 끼어들기)"
        elif recent >= 4:
            mood = "대화가 이어지는 중"
        else:
            mood = "대화가 뜸한 편 (조금 더 먼저 화제를 던져도 좋음)"
        lines.append(f"분위기: 최근 5분간 메시지 {recent}개 — {mood}")
        return "\n".join(lines)

    def _hint(self, target_name: str, target_text: str, own_recent: list[str], can_skip: bool) -> str:
        own = "\n".join(f"  · {t}" for t in own_recent[-8:]) or "  (없음)"
        parts = [
            "[상황 메모 — 이 메모 자체는 절대 언급하지 마라]",
            f"- 대답할 대상: {target_name}이(가) 한 말 → \"{target_text}\"",
            "- 상황 파악: (1) 이 말이 누구에게 하는 말인지 (2) 농담/진지/하소연/질문 중 무엇인지 (3) 지금 분위기 "
            "(4) 이미 다른 사람이 답한 이야기인지 판단하고 그에 맞게 반응해라. 이미 정리된 얘기는 반복하지 마라.",
            "- 이름: 위 [현재 대화 상황]의 이름(닉네임)으로 부르되 매번 부르지는 마라. "
            "이전에 나온 이름, 사건, 말을 기억해서 맥락 있게 언급해라.",
            "- 길이와 말투를 매번 다르게. 한 줄 또는 짧은 리액션이 기본이다.",
            f"- 최근에 네가 한 말이다. 이와 비슷한 표현, 시작 단어, 문장 구조, 농담은 쓰지 마라:\n{own}",
        ]
        if can_skip:
            parts.append(
                "- 이 말이 너와 상관없고 끼어들 이유도 없으면 다른 말 없이 SKIP 이라고만 출력해라."
            )
        return "\n".join(parts)

    # ---------- 프롬프트 조립 ----------
    async def _build(
        self, channel_id: int, target_id: int, target_name: str, target_text: str, can_skip: bool
    ) -> tuple[list[dict[str, str]], set[str]]:
        mcfg = self.s["memory"]
        facts = await self.mem.get_facts(target_id, mcfg["max_facts_in_prompt"])
        hist = await self.mem.recent(channel_id, mcfg["history_turns"])
        tname = clean_name(target_name)

        merged: list[dict[str, str]] = []
        prev_ts: float | None = None
        for h in hist:
            if h["role"] == "user":
                role, text = "user", f"{clean_name(h['author'])}: {h['content']}"
                if prev_ts and h["ts"] - prev_ts >= 600:
                    text = f"(약 {int((h['ts'] - prev_ts) // 60)}분 지남)\n" + text
            else:
                role, text = "assistant", h["content"]
            prev_ts = h["ts"]
            if merged and merged[-1]["role"] == role:
                merged[-1]["content"] += "\n" + text
            else:
                merged.append({"role": role, "content": text})

        own_recent = [ln for h in hist if h["role"] == "assistant" for ln in h["content"].split("\n") if ln.strip()]
        hint = self._hint(tname, target_text, own_recent, can_skip)
        if merged and merged[-1]["role"] == "user":
            merged[-1]["content"] += "\n\n" + hint
        else:
            merged.append({"role": "user", "content": hint})

        scene = await self._scene(hist)
        system = self.persona.system_prompt(tname, facts, self.now(), scene)
        msgs = [{"role": "system", "content": system}] + self.persona.example_messages() + merged
        known = {h["author"] for h in hist if h["role"] == "user"} | {clean_name(h["author"]) for h in hist}
        return msgs, known

    async def _record(self, kind: str, res: LLMResult, target_id: int, target_name: str,
                      channel_id: int, snippet: str) -> None:
        await self.mem.add_usage(
            kind, self.persona.char_id, self.llm.model, target_id, target_name, channel_id,
            res.prompt_tokens, res.completion_tokens, snippet,
        )

    # ---------- 응답 (대화 기록에 target 메시지가 이미 저장된 상태에서 호출) ----------
    async def reply(
        self, channel_id: int, target_id: int, target_name: str, target_text: str,
        dm: bool = False, can_skip: bool = False,
    ) -> list[str]:
        msgs, known = await self._build(channel_id, target_id, target_name, target_text, can_skip)
        res = await self.llm.chat(msgs)
        if can_skip and re.match(r"^\W*\[?SKIP\]?\W*$", res.text.strip(), re.I):
            await self._record("reply", res, target_id, target_name, channel_id, "(SKIP: 대답하지 않음)")
            return []
        bubbles = self.to_bubbles(res.text, known)
        if not bubbles:
            return []
        await self.mem.add_message(
            channel_id, 0, self.persona.name, "assistant", "\n".join(bubbles),
            dm_user_id=target_id if dm else None,
        )
        await self._record("reply", res, target_id, target_name, channel_id, " / ".join(bubbles))
        self._schedule_extract(channel_id, target_id, target_name)
        return bubbles

    # ---------- 먼저 말 걸기 (DM) ----------
    async def proactive(self, channel_id: int, user_id: int, user_name: str, idle_hours: float) -> list[str]:
        note = (
            f"{user_name}에게 먼저 말을 거는 상황이다. 마지막 대화로부터 약 {idle_hours:.0f}시간이 지났다. "
            "현재 시간대와 기억하는 정보를 바탕으로 자연스럽고 짧게 먼저 연락해라."
        )
        msgs, known = await self._build(channel_id, user_id, user_name, note, False)
        res = await self.llm.chat(msgs, temperature=1.0)
        bubbles = self.to_bubbles(res.text, known)
        if bubbles:
            await self.mem.add_message(
                channel_id, 0, self.persona.name, "assistant", "\n".join(bubbles), dm_user_id=user_id
            )
            await self._record("proactive", res, user_id, user_name, channel_id, " / ".join(bubbles))
        return bubbles

    # ---------- 후처리 ----------
    def to_bubbles(self, raw: str, other_names: set[str] | None = None) -> list[str]:
        name = re.escape(self.persona.name)
        nick = re.escape(self.persona.nickname)
        text = raw.strip()
        text = re.sub(rf"^\s*(?:{name}|{nick})\s*[:：]\s*", "", text)
        if other_names:
            pat = "|".join(re.escape(n) for n in sorted(other_names, key=len, reverse=True) if n)
            if pat:
                m = re.search(rf"^\s*(?:{pat})\s*[:：]", text, re.M)
                if m:
                    text = text[: m.start()]
        text = re.sub(r"^\s*\(?약 \d+분 지남\)?\s*", "", text)
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
                f"{clean_name(m['author']) if m['role'] == 'user' else self.persona.name}: {m['content']}"
                for m in recent
            )
            known = await self.mem.get_facts(user_id, 40)
            async with self.gpu_lock:
                facts, pt, ct = await self.llm.extract_facts(
                    clean_name(user_name), dialogue, known, self.persona.name)
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

    # GPU 는 하나이므로 main 에서 같이 쓰는 락 (기억 추출도 같은 락)
    gpu_lock: asyncio.Lock = asyncio.Lock()

    def typing_delay(self, text: str) -> float:
        t = self.s["typing"]
        d = len(text) / max(t["chars_per_second"], 1)
        return min(max(d, t["min_delay"]), t["max_delay"])
