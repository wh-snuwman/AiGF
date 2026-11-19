"""대화 엔진: 채널 대화 기록 + 참여자/상황 분석 → 프롬프트 → LLM → 말풍선 분리 → 기억/사용량 기록."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .llm import LLM, LLMResult
from .memory import Memory
from .persona import Persona

log = logging.getLogger("chat")


def clean_name(name: str) -> str:
    """'SNUWMAN [AnA],' 같은 디스코드 표시 이름에서 부르기 좋은 이름만 남긴다."""
    s = re.sub(r"[\[\(\{【《<][^\]\)\}】》>]*[\]\)\}】》>]", "", name)
    s = re.sub(r"[^\w\s\-.]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ._-")
    return s or name.strip()


# ---------- 답변 길이: 상황에 따라 랜덤하게 ----------
@dataclass(frozen=True)
class LengthMode:
    label: str
    desc: str
    num_predict: int
    max_bubbles: int


MODES = [
    LengthMode("초단문", "한두 단어나 짧은 리액션만 (예: ㅋㅋ, 오, 헐, 아 맞네, 그건 좀)", 60, 1),
    LengthMode("한 줄", "짧은 한 문장", 110, 1),
    LengthMode("보통", "1~2문장", 200, 2),
    LengthMode("길게", "2~4문장. 말풍선을 2~3개로 나눠서 이야기를 풀어서", 340, 3),
]


def pick_length(text: str, activity: int, priority: bool, free: bool = False) -> LengthMode:
    """대상 말의 길이/질문 여부, 채팅방 활발함, 멘션 여부로 가중치를 바꿔 랜덤 선택."""
    body = re.sub(r"^\(.*?에 답장\)\s*", "", text)
    n = len(body.strip())
    if free:
        w = [0.3, 3.0, 4.0, 1.5]            # 먼저 말을 꺼낼 땐 어느 정도 말을 한다
    elif n <= 6:
        w = [5.0, 4.0, 1.0, 0.2]
    elif n <= 25:
        w = [3.0, 4.0, 2.0, 0.6]
    elif n <= 60:
        w = [1.5, 3.5, 3.0, 1.2]
    else:
        w = [1.0, 2.5, 3.5, 2.5]
    if not free and re.search(r"[?？]|뭐|어떻|왜|어때|알려", body):   # 질문이면 조금 더 길게
        w = [w[0] * 0.5, w[1], w[2] * 1.4, w[3] * 1.5]
    if activity >= 10:                       # 채팅이 빠르면 짧게
        w = [w[0] * 1.6, w[1] * 1.2, w[2] * 0.6, w[3] * 0.25]
    elif activity <= 2 and not free:         # 한가하면 조금 길게
        w = [w[0] * 0.7, w[1], w[2] * 1.3, w[3] * 1.5]
    if priority:                             # 직접 부르면 한 단어로 때우지 않는다
        w = [w[0] * 0.25, w[1], w[2] * 1.3, w[3]]
    return random.choices(MODES, weights=w, k=1)[0]


class ChatEngine:
    def __init__(self, settings: dict, persona: Persona, llm: LLM, memory: Memory) -> None:
        self.s = settings
        self.persona = persona
        self.llm = llm
        self.mem = memory
        self.tz = ZoneInfo(settings.get("timezone", "Asia/Seoul"))
        self.names: dict[int, tuple[str, str | None]] = {}   # user_id -> (실명, 호칭)
        self.views: dict[int, str] = {}                      # user_id -> 캐릭터가 가진 인식
        self._bg: set[asyncio.Task] = set()

    def now(self) -> datetime:
        return datetime.now(self.tz)

    # ---------- 이름 ----------
    async def load_names(self) -> None:
        self.names = await self.mem.all_real_names()
        self.views = await self.mem.all_views()

    def label(self, user_id: int, author: str) -> str:
        """프롬프트에서 쓸 이름: 등록된 실명 > 정리한 닉네임"""
        if user_id in self.names:
            return self.names[user_id][0]
        return clean_name(author)

    def call_name(self, user_id: int, author: str) -> str:
        real, call = self.names.get(user_id, (None, None))
        return call or real or clean_name(author)

    # ---------- 상황 분석 ----------
    async def _scene(self, hist: list[dict]) -> tuple[str, int]:
        users = [h for h in hist if h["role"] == "user"]
        now_ts = time.time()
        recent = sum(1 for h in hist if now_ts - h["ts"] < 300)
        if not users:
            return "", recent
        counts: Counter[int] = Counter(h["user_id"] for h in users)
        authors = {h["user_id"]: h["author"] for h in users}

        lines = ["참여자 (최근 발언 많은 순):"]
        for uid, n in counts.most_common(5):
            lab = self.label(uid, authors[uid])
            call = self.call_name(uid, authors[uid])
            facts = await self.mem.get_facts(uid, 3)
            extra = f", 부를 때: {call}" if call != lab else ""
            tail = f" / 기억: {'; '.join(facts)}" if facts else ""
            view = f" / 네가 보는 인식: {self.views[uid]}" if uid in self.views else ""
            lines.append(f"- {lab} ({n}회 발언{extra}){view}{tail}")

        flow: list[str] = []
        for h in users[-6:]:
            nm = self.label(h["user_id"], h["author"])
            if not flow or flow[-1] != nm:
                flow.append(nm)
        lines.append("최근 발언 흐름: " + " → ".join(flow))

        if recent >= 10:
            mood = "대화가 매우 활발함 (짧게 끼어들기)"
        elif recent >= 4:
            mood = "대화가 이어지는 중"
        else:
            mood = "대화가 뜸한 편"
        lines.append(f"분위기: 최근 5분간 메시지 {recent}개 — {mood}")
        return "\n".join(lines), recent

    async def _context(self, channel_id: int, target_id: int | None):
        mcfg = self.s["memory"]
        hist = await self.mem.recent(channel_id, mcfg["history_turns"])
        facts = await self.mem.get_facts(target_id, mcfg["max_facts_in_prompt"]) if target_id else []

        merged: list[dict[str, str]] = []
        prev_ts: float | None = None
        for h in hist:
            if h["role"] == "user":
                role, text = "user", f"{self.label(h['user_id'], h['author'])}: {h['content']}"
                if prev_ts and h["ts"] - prev_ts >= 600:
                    text = f"(약 {int((h['ts'] - prev_ts) // 60)}분 지남)\n" + text
            else:
                role, text = "assistant", h["content"]
            prev_ts = h["ts"]
            if merged and merged[-1]["role"] == role:
                merged[-1]["content"] += "\n" + text
            else:
                merged.append({"role": role, "content": text})

        own = [ln for h in hist if h["role"] == "assistant" for ln in h["content"].split("\n") if ln.strip()]
        scene, activity = await self._scene(hist)
        known: set[str] = set()
        for h in hist:
            if h["role"] == "user":
                known |= {h["author"], clean_name(h["author"]), self.label(h["user_id"], h["author"]),
                          self.call_name(h["user_id"], h["author"])}
        return hist, merged, own, scene, activity, facts, known

    def _assemble(self, target_label: str, facts: list[str], scene: str,
                  merged: list[dict[str, str]], hint: str, view: str = "") -> list[dict[str, str]]:
        if merged and merged[-1]["role"] == "user":
            merged[-1]["content"] += "\n\n" + hint
        else:
            merged.append({"role": "user", "content": hint})
        system = self.persona.system_prompt(target_label, facts, self.now(), scene, view)
        return [{"role": "system", "content": system}] + self.persona.example_messages() + merged

    @staticmethod
    def _own_block(own_recent: list[str]) -> str:
        own = "\n".join(f"  · {t}" for t in own_recent[-8:]) or "  (없음)"
        return f"- 최근에 네가 한 말이다. 이와 비슷한 표현, 시작 단어, 문장 구조, 농담은 쓰지 마라:\n{own}"

    async def _record(self, kind: str, res: LLMResult, target_id: int, target_name: str,
                      channel_id: int, snippet: str) -> None:
        await self.mem.add_usage(
            kind, self.persona.char_id, self.llm.model, target_id, target_name, channel_id,
            res.prompt_tokens, res.completion_tokens, snippet,
        )

    # ---------- 응답 (대화 기록에 target 메시지가 이미 저장된 상태에서 호출) ----------
    async def reply(
        self, channel_id: int, target_id: int, target_name: str, target_text: str,
        dm: bool = False, can_skip: bool = False, priority: bool = False, extra_note: str | None = None,
    ) -> list[str]:
        hist, merged, own, scene, activity, facts, known = await self._context(channel_id, target_id)
        who = self.label(target_id, target_name)
        call = self.call_name(target_id, target_name)
        mode = pick_length(target_text, activity, priority or dm)

        parts = [
            "[상황 메모 — 이 메모 자체는 절대 언급하지 마라]",
            f"- 대답할 대상: {who}이(가) 한 말 → \"{target_text}\"  (부를 때 이름: {call})",
            "- 상황 파악: (1) 이 말이 누구에게 하는 말인지 (2) 농담/진지/하소연/질문 중 무엇인지 (3) 지금 분위기 "
            "(4) 이미 다른 사람이 답한 이야기인지 판단하고 그에 맞게 반응해라. 이미 정리된 얘기는 반복하지 마라.",
            "- 이름: 사람을 부를 때는 위 [현재 대화 상황]에 적힌 진짜 이름/호칭을 쓰되 매번 부르지는 마라. "
            "이전에 나온 이름, 사건, 말을 기억해서 맥락 있게 언급해라.",
            f"- 이번 답변 길이: [{mode.label}] {mode.desc}. 이 길이를 지켜라.",
            self._own_block(own),
        ]
        if extra_note:
            parts.append(f"- 추가 상황: {extra_note}")
        if can_skip:
            parts.append("- 이 말이 너와 상관없고 끼어들 이유도 없으면 다른 말 없이 SKIP 이라고만 출력해라.")
        msgs = self._assemble(who, facts, scene, merged, "\n".join(parts), self.views.get(target_id, ""))

        res = await self.llm.chat(msgs, num_predict=mode.num_predict)
        if can_skip and re.match(r"^\W*\[?SKIP\]?\W*$", res.text.strip(), re.I):
            await self._record("reply", res, target_id, target_name, channel_id, "(SKIP: 대답하지 않음)")
            return []
        bubbles = self.to_bubbles(res.text, known, mode.max_bubbles)
        if not bubbles:
            return []
        await self.mem.add_message(
            channel_id, 0, self.persona.name, "assistant", "\n".join(bubbles),
            dm_user_id=target_id if dm else None,
        )
        await self._record("reply", res, target_id, target_name, channel_id, f"[{mode.label}] " + " / ".join(bubbles))
        self._schedule_extract(channel_id, target_id, target_name)
        return bubbles

    # ---------- 채널이 조용할 때 먼저 말 걸기 ----------
    async def initiate(self, channel_id: int, instruction: str, idle_minutes: int, unanswered: int) -> list[str]:
        hist, merged, own, scene, activity, facts, known = await self._context(channel_id, None)
        mode = pick_length("", activity, False, free=True)
        parts = [
            "[상황 메모 — 이 메모 자체는 절대 언급하지 마라]",
            f"- 이 채널은 약 {idle_minutes}분째 아무도 말하지 않고 있다. 네가 먼저 말을 꺼내는 상황이다.",
            f"- 이번에 할 행동: {instruction}",
        ]
        if unanswered:
            parts.append(f"- 지금까지 네가 먼저 말을 꺼냈는데 아무도 답하지 않은 횟수: {unanswered}번")
        parts += [
            "- 사람 이름을 부를 때는 [현재 대화 상황]에 적힌 진짜 이름/호칭을 쓴다. 특정인을 부르지 않아도 된다.",
            f"- 이번 길이: [{mode.label}] {mode.desc}. 이 길이를 지켜라.",
            self._own_block(own),
        ]
        msgs = self._assemble("", [], scene, merged, "\n".join(parts))
        res = await self.llm.chat(msgs, num_predict=mode.num_predict, temperature=1.05)
        bubbles = self.to_bubbles(res.text, known, mode.max_bubbles)
        if bubbles:
            await self.mem.add_message(channel_id, 0, self.persona.name, "assistant", "\n".join(bubbles))
            await self._record("idle", res, 0, "(혼잣말)", channel_id, f"[{mode.label}] " + " / ".join(bubbles))
        return bubbles

    # ---------- 먼저 말 걸기 (DM) ----------
    async def proactive(self, channel_id: int, user_id: int, user_name: str, idle_hours: float) -> list[str]:
        hist, merged, own, scene, activity, facts, known = await self._context(channel_id, user_id)
        who = self.label(user_id, user_name)
        mode = pick_length("", activity, False, free=True)
        hint = "\n".join([
            "[상황 메모 — 이 메모 자체는 절대 언급하지 마라]",
            f"- {who}에게 먼저 말을 거는 상황이다. 마지막 대화로부터 약 {idle_hours:.0f}시간이 지났다. "
            "현재 시간대와 기억하는 정보를 바탕으로 자연스럽게 먼저 연락해라.",
            f"- 길이: [{mode.label}] {mode.desc}",
            self._own_block(own),
        ])
        msgs = self._assemble(who, facts, scene, merged, hint, self.views.get(user_id, ""))
        res = await self.llm.chat(msgs, temperature=1.0, num_predict=mode.num_predict)
        bubbles = self.to_bubbles(res.text, known, mode.max_bubbles)
        if bubbles:
            await self.mem.add_message(
                channel_id, 0, self.persona.name, "assistant", "\n".join(bubbles), dm_user_id=user_id)
            await self._record("proactive", res, user_id, user_name, channel_id, " / ".join(bubbles))
        return bubbles

    # ---------- 후처리 ----------
    def to_bubbles(self, raw: str, other_names: set[str] | None = None, limit: int | None = None) -> list[str]:
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
        limit = min(limit or 99, self.s["typing"]["max_bubbles"])
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
                f"{self.label(m['user_id'], m['author']) if m['role'] == 'user' else self.persona.name}: {m['content']}"
                for m in recent
            )
            known = await self.mem.get_facts(user_id, 40)
            async with self.gpu_lock:
                facts, pt, ct = await self.llm.extract_facts(
                    self.label(user_id, user_name), dialogue, known, self.persona.name)
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
