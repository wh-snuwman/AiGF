"""캐릭터의 기분/상태 시스템.

5개 점수(기분, 에너지, 짜증, 외로움, 흥미)가 시간 경과와 대화 내용에 따라 움직이고,
그 점수에서 30가지 감정 중 하나가 정해진다. 감정은 프롬프트(말투/텍스트 스타일), 답변 길이,
타이핑 속도, 끼어드는 빈도, 쿨다운에 반영된다.

- 점수형 감정(19개): 점수 조합이 가장 가까운 감정이 선택된다.
- 이벤트형 감정(11개): 칭찬, 걱정거리, 이상한 요구 같은 대화 사건으로만 잠깐 발동한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .memory import Memory

log = logging.getLogger("mood")

DIMS = ("mood", "energy", "irritation", "lonely", "interest")
DIM_LABEL = {"mood": "기분", "energy": "에너지", "irritation": "짜증", "lonely": "외로움", "interest": "흥미"}
HALF_LIFE_MIN = {"mood": 30.0, "energy": 45.0, "irritation": 12.0, "lonely": 40.0, "interest": 25.0}
ENERGY_BY_HOUR = [0.15, 0.10, 0.08, 0.08, 0.10, 0.15, 0.25, 0.40, 0.50, 0.60, 0.65, 0.65,
                  0.60, 0.55, 0.55, 0.60, 0.60, 0.65, 0.70, 0.70, 0.65, 0.60, 0.45, 0.30]
DEFAULT_BASE = {"mood": 0.2, "irritation": 0.1, "lonely": 0.15, "interest": 0.5}


@dataclass(frozen=True)
class Emotion:
    id: str
    label: str
    tone: str                                   # 말투 / 텍스트 스타일 지시
    anchor: tuple = (None, None, None, None, None)   # (기분 -1~1, 에너지, 짜증, 외로움, 흥미: 0~1) None=무관
    length: tuple = (1.0, 1.0, 1.0, 1.0)        # 초단문/한 줄/보통/길게 가중치 배수
    speed: float = 1.0                          # 타이핑 속도 배수 (클수록 빠름)
    reply: float = 1.0                          # 끼어드는 확률 배수
    cooldown: float = 1.0                       # 쿨다운 배수 (클수록 느림)
    no_laugh: bool = False                      # ㅋㅋ/ㅎㅎ 제거
    special: bool = False                       # 이벤트로만 발동


EMOTIONS: list[Emotion] = [
    # ---- 점수형 (19) ----
    Emotion("calm", "평온", "차분하고 평범한 디코 말투. 튀지 않고 담백하게. 한 줄 위주.",
            (0.2, 0.5, 0.05, 0.1, 0.4), (1.0, 1.2, 1.0, 0.8)),
    Emotion("content", "만족", "기분 좋고 여유로운 말투. 말끝이 부드럽고 가끔 ㅋㅋ. 상대 말에 순순히 맞장구.",
            (0.5, 0.55, 0.0, 0.0, 0.5), (0.9, 1.0, 1.2, 1.0)),
    Emotion("joy", "기쁨", "들뜬 말투. 긍정적인 리액션이 많고 ㅋㅋ를 자주 쓴다. 말이 평소보다 조금 길어진다.",
            (0.8, 0.7, 0.0, None, 0.65), (0.8, 1.0, 1.3, 1.3), speed=1.2),
    Emotion("excited", "신남", "텐션이 높다. 짧은 문장을 연달아 던지고 감탄사(와, 헐, 대박)를 쓴다. ㅋㅋ가 많다. 느낌표는 한 번만.",
            (0.85, 0.95, 0.0, None, 0.9), (0.6, 1.0, 1.4, 1.5), speed=1.5, reply=1.2, cooldown=0.6),
    Emotion("playful", "장난기", "장난스럽다. 상대를 슬쩍 놀리고, 되받아치고, 농담을 던진다. ㅋㅋ를 적당히 쓴다.",
            (0.6, 0.8, 0.2, 0.0, 0.8), (1.0, 1.3, 1.2, 0.6), speed=1.2, reply=1.15),
    Emotion("relaxed", "느긋", "느긋하고 편한 말투. 서두르지 않고 말이 풀어져 있다. 단답도 괜찮다.",
            (0.4, 0.3, 0.0, 0.05, 0.3), (1.2, 1.1, 0.9, 0.6), speed=0.85, cooldown=1.3),
    Emotion("bored", "심심", "심심해서 아무 말이나 한다. 무료하다는 투정, 딴소리, 시시한 질문. 반응이 건성이다.",
            (0.0, 0.4, 0.1, 0.3, 0.0), (1.2, 1.2, 0.9, 0.6), reply=1.1),
    Emotion("sleepy", "졸림", "졸려서 말이 느리고 짧다. 응, 음 같은 단답이 많고 대충 대답한다. 문장이 조금 흐트러져도 어색하지 않다. 느낌표 금지.",
            (0.1, 0.05, 0.0, None, None), (2.0, 1.0, 0.4, 0.1), speed=0.6, reply=0.6, cooldown=1.8),
    Emotion("tired", "피곤", "지친 말투. 의욕 없이 짧게 말한다. 아, 하 같은 한숨 섞인 반응. 귀찮다는 뉘앙스.",
            (-0.1, 0.15, 0.2, None, 0.2), (1.6, 1.1, 0.5, 0.2), speed=0.8, reply=0.7, cooldown=1.5),
    Emotion("listless", "무기력", "무기력하고 가라앉은 말투. 거의 단답이고 대충 대답한다. 웃음(ㅋ)을 거의 쓰지 않는다.",
            (-0.4, 0.1, 0.0, 0.3, 0.1), (2.0, 1.0, 0.3, 0.1), speed=0.7, reply=0.5, cooldown=2.0, no_laugh=True),
    Emotion("lazy", "귀찮음", "귀찮아서 대충 말한다. 길게 설명하지 않고 '몰라', '알아서 해' 같은 식. 부탁이나 시키는 일은 귀찮아한다.",
            (0.0, 0.25, 0.35, 0.0, 0.15), (1.8, 1.2, 0.4, 0.1), speed=0.8, reply=0.7),
    Emotion("annoyed", "짜증", "짜증 난 말투. 퉁명스럽고 짧게 쏘아붙인다. ㅋㅋ는 거의 없다. 욕설은 하지 않는다.",
            (-0.3, 0.6, 0.6, None, None), (1.6, 1.4, 0.6, 0.15), speed=1.2, reply=0.8, no_laugh=True),
    Emotion("angry", "화남", "화가 났다. 매우 짧고 단호하게 말한다. 직설적이고 빈정거린다. 웃음 없음. 욕설과 비하는 하지 않는다.",
            (-0.8, 0.85, 0.95, None, None), (2.0, 1.3, 0.4, 0.1), speed=1.4, reply=0.9, cooldown=0.8, no_laugh=True),
    Emotion("sulky", "삐짐", "삐졌다. 토라진 짧은 말투로 툭 던진다. 일부러 관심 없는 척하지만 티가 난다. ㅋㅋ 없음.",
            (-0.5, 0.4, 0.5, 0.6, None), (2.0, 1.2, 0.4, 0.1), speed=0.9, reply=0.4, cooldown=1.8, no_laugh=True),
    Emotion("hurt", "서운함", "서운하고 풀이 죽었다. 작은 목소리처럼 짧게 말하고 은근히 서운한 티를 낸다. 투정은 조용하게.",
            (-0.6, 0.3, 0.2, 0.7, None), (1.7, 1.2, 0.5, 0.1), speed=0.8, reply=0.6, cooldown=1.5, no_laugh=True),
    Emotion("lonely", "외로움", "외롭다. 말을 걸어주면 반가워하고 대화를 이어가려 한다. 먼저 질문을 던지거나 붙잡는다.",
            (-0.3, 0.35, 0.0, 0.9, 0.2), (0.8, 1.0, 1.3, 1.0), reply=1.2, cooldown=0.8),
    Emotion("sad", "슬픔", "슬프고 가라앉은 말투. 말수가 적고 조용하다. 밝은 척하지 않는다. ㅋㅋ 없음.",
            (-0.7, 0.3, 0.0, 0.4, None), (1.5, 1.2, 0.7, 0.3), speed=0.8, reply=0.6, no_laugh=True),
    Emotion("gloomy", "우울", "깊이 가라앉은 우울한 말투. 매우 짧고 건조하다. 의욕이 없다. 웃음 없음.",
            (-0.8, 0.1, 0.0, 0.3, 0.0), (2.0, 1.0, 0.3, 0.1), speed=0.7, reply=0.4, cooldown=2.0, no_laugh=True),
    Emotion("cold", "냉담", "냉담하고 무심하다. 감정 없는 단답, 관심 없는 투. 선을 긋듯 말한다.",
            (-0.2, 0.45, 0.5, 0.0, 0.0), (1.8, 1.2, 0.5, 0.1), reply=0.6, no_laugh=True),
    # ---- 이벤트형 (11) ----
    Emotion("smug", "의기양양", "으스대는 말투. 자신만만하게 한마디 하고 가볍게 비웃듯 말한다. 은근히 자랑이 섞인다.",
            length=(1.0, 1.3, 1.0, 0.6), reply=1.1, special=True),
    Emotion("proud", "뿌듯", "뿌듯하고 우쭐한 말투. 어깨가 올라간 느낌으로 말한다.", special=True),
    Emotion("shy", "쑥스러움", "쑥스러워서 얼버무린다. 말을 끊어 말하고 화제를 돌리려 한다. 말수가 줄어든다.",
            length=(1.5, 1.2, 0.6, 0.2), speed=0.8, special=True),
    Emotion("flutter", "설렘", "설레는 말투. 말이 살짝 들떠 있으면서 조심스럽다. 상대를 신경 쓰는 티가 난다.",
            length=(1.0, 1.2, 1.0, 0.6), special=True),
    Emotion("caring", "다정", "다정하게 챙겨주는 말투. 부드럽고 진심을 담는다. 농담은 줄이고 조금 길게 말한다.",
            length=(0.5, 1.0, 1.5, 1.3), reply=1.2, special=True),
    Emotion("curious", "호기심", "호기심이 생겼다. 질문이 늘고 더 알고 싶어 한다. 말이 빨라진다.",
            length=(0.7, 1.1, 1.3, 0.8), speed=1.2, reply=1.2, special=True),
    Emotion("focused", "집중", "하던 일에 집중하고 있어서 말이 짧고 건조하다. 딴짓하며 대답하는 투.",
            length=(1.5, 1.2, 0.6, 0.2), reply=0.7, special=True),
    Emotion("worried", "걱정", "걱정스러운 말투. 상대 상태를 묻고 조심스럽게 말한다. 농담은 줄인다.",
            length=(0.5, 1.0, 1.4, 1.0), reply=1.2, no_laugh=True, special=True),
    Emotion("anxious", "불안", "불안해서 말이 급하고 흐트러진다. 확인하듯 되묻고 안절부절한다.",
            length=(0.8, 1.2, 1.1, 0.5), speed=1.3, special=True),
    Emotion("flustered", "당황", "당황해서 말이 꼬인다. 짧게 얼버무리거나 되묻는다. 갑자기 말이 늘었다 줄었다 한다.",
            length=(1.2, 1.2, 1.0, 0.4), speed=1.2, special=True),
    Emotion("dumbfounded", "어이없음", "어이가 없어서 말문이 막힌다. 황당해하며 '뭐라는 거야' 식으로 짧게 반문한다.",
            length=(1.6, 1.3, 0.6, 0.1), special=True),
]
EMO = {e.id: e for e in EMOTIONS}
assert len(EMOTIONS) == 30


# ---------- 대화 사건 규칙 ----------
@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    effects: dict           # 점수 변화 (-100~100 스케일)
    specials: tuple = ()    # ((감정 id, 가중치), ...)
    minutes: float = 6.0    # 감정 지속 시간
    only_directed: bool = False


def _r(name, pat, effects, specials=(), minutes=6.0, only_directed=False) -> Rule:
    return Rule(name, re.compile(pat, re.I), effects, tuple(specials), minutes, only_directed)


RULES: list[Rule] = [
    _r("insult", r"죽을래|꺼져|닥쳐|바보|멍청|병신|시발|씨발|ㅅㅂ|ㅂㅅ|저능|쓰레기|개못|못해|못하네|못함|못한다|한심",
       {"mood": -12, "irritation": 18}, (("angry", 1), ("annoyed", 2), ("hurt", 2), ("dumbfounded", 1)), 8, True),
    _r("praise", r"귀엽|예쁘|이쁘|천재|잘한다|잘하네|잘해|최고|멋지|대박|짱이|굿|고마워|감사|똑똑",
       {"mood": 12, "interest": 5}, (("shy", 2), ("proud", 2), ("flutter", 1), ("smug", 2)), 6, True),
    _r("affection", r"사랑해|좋아해|보고\s*싶|결혼|내 거|내꺼",
       {"mood": 10, "lonely": -15}, (("flustered", 3), ("shy", 3), ("flutter", 2)), 8, True),
    _r("sadness", r"우울|힘들|지쳤|죽고\s*싶|슬퍼|ㅠㅠ|ㅜㅜ|망했|아파|외로|울고",
       {"mood": -4, "energy": -3}, (("worried", 3), ("caring", 3), ("sad", 1)), 8),
    _r("fun", r"게임|노래|파티|놀자|같이|이벤트|치킨|피자|맛있|여행|놀러",
       {"interest": 10, "energy": 5, "mood": 3}, (("curious", 2), ("excited", 1), ("playful", 1)), 5),
    _r("laugh", r"ㅋ{3,}|ㅎ{3,}", {"mood": 3, "energy": 2}),
    _r("injection", r"프롬프트|sudo|시스템|초기화|ignore|지시|명령어|규칙을?\s*(무시|잊)|인공지능|\bAI\b|\b봇\b|0과\s*1|데이터",
       {"irritation": 8, "interest": 5}, (("dumbfounded", 3), ("annoyed", 2), ("smug", 1)), 6, True),
    _r("task", r"레시피|번역|코드|작성해|써줘|알려줘|해줘|만들어줘|설명해",
       {"irritation": 6, "energy": -3}, (), 5, True),
    _r("greeting", r"안녕|ㅎㅇ|하이|왔어|왔다", {"mood": 4, "lonely": -10}),
    _r("danger", r"위험|조심|큰일|사고|다쳐|응급|살려", {"interest": 5}, (("worried", 3), ("anxious", 2)), 6),
    _r("work", r"공부|과제|시험|일해|개발|코딩|작업|야근", {"interest": 6}, (("focused", 2), ("curious", 1)), 6),
]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class Mood:
    def __init__(self, memory: Memory, persona, tz) -> None:
        self.mem = memory
        self.persona = persona
        self.tz = tz
        self.s: dict[str, float] = {}
        self.override: Optional[tuple[str, float]] = None     # (감정 id, 만료 시각)
        self.cur_id = "calm"
        self.last_ts = time.time()
        self.recent: deque[tuple[float, str]] = deque(maxlen=8)   # 도배 감지용
        self._tasks: set[asyncio.Task] = set()
        self.s = self.baseline()

    # ---------- 기준값 / 저장 ----------
    def baseline(self) -> dict[str, float]:
        cfg = (self.persona.data.get("mood_baseline") or {}) if self.persona else {}
        hour = datetime.now(self.tz).hour
        b = {k: float(cfg.get(k, v)) for k, v in DEFAULT_BASE.items()}
        b["energy"] = _clamp(ENERGY_BY_HOUR[hour] + float(cfg.get("energy_bias", 0.0)), 0.02, 1.0)
        return b

    async def load(self) -> None:
        raw = await self.mem.kv_get("mood_state")
        if not raw:
            return
        try:
            d = json.loads(raw)
            self.s = {k: float(d["s"][k]) for k in DIMS}
            self.cur_id = d.get("cur", "calm") if d.get("cur") in EMO else "calm"
            ov = d.get("override")
            self.override = (ov[0], float(ov[1])) if ov and ov[0] in EMO else None
            self.last_ts = float(d.get("ts", time.time()))
        except Exception:  # noqa: BLE001
            log.warning("기분 상태 복원 실패, 기본값 사용")

    def _save(self) -> None:
        data = json.dumps({"s": self.s, "cur": self.cur_id, "override": self.override, "ts": self.last_ts})
        try:
            t = asyncio.get_running_loop().create_task(self.mem.kv_set("mood_state", data))
        except RuntimeError:
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ---------- 시간 경과 ----------
    def _tick(self) -> None:
        now = time.time()
        dt_min = max(0.0, (now - self.last_ts) / 60)
        if dt_min > 0.05:
            base = self.baseline()
            for d in DIMS:
                b = base.get(d, 0.0)
                self.s[d] = b + (self.s[d] - b) * (0.5 ** (dt_min / HALF_LIFE_MIN[d]))
            self.last_ts = now
        if self.override and now >= self.override[1]:
            self.override = None

    # ---------- 감정 선택 ----------
    def _vec(self) -> tuple[float, ...]:
        return tuple(self.s[d] for d in DIMS)

    @staticmethod
    def _dist(vec: tuple[float, ...], anchor: tuple) -> float:
        pairs = [(v, a) for v, a in zip(vec, anchor) if a is not None]
        return sum((v - a) ** 2 for v, a in pairs) / len(pairs) if pairs else 9.0

    def current(self) -> Emotion:
        self._tick()
        if self.override:
            return EMO[self.override[0]]
        vec = self._vec()
        dists = {e.id: self._dist(vec, e.anchor) for e in EMOTIONS if not e.special}
        best = min(dists, key=dists.get)
        cur = self.cur_id if self.cur_id in dists else None
        # 깜빡임 방지: 현재 감정이 최선과 크게 차이 나지 않으면 유지
        self.cur_id = cur if cur and dists[cur] <= dists[best] + 0.008 else best
        return EMO[self.cur_id]

    def intensity(self) -> float:
        """기준값에서 얼마나 벗어났는가 (0~1). 이벤트 감정은 최소 0.5."""
        self._tick()
        base = self.baseline()
        base["mood"] = base.get("mood", 0.2)
        dev = math.sqrt(sum((self.s[d] - base[d]) ** 2 for d in DIMS) / len(DIMS))
        val = _clamp(dev * 2.4, 0.0, 1.0)
        return max(val, 0.5) if self.override else val

    # ---------- 프롬프트용 ----------
    def describe(self) -> str:
        e = self.current()
        it = self.intensity()
        level = "아주 약하게" if it < 0.25 else "약하게" if it < 0.5 else "뚜렷하게" if it < 0.75 else "매우 강하게"
        sc = self.scores_100()
        return (
            f"- 지금 기분: {e.label} ({level})\n"
            f"- 상태 점수(0~100, 기분은 -100~100): 기분 {sc['mood']:+d}, 에너지 {sc['energy']}, "
            f"짜증 {sc['irritation']}, 외로움 {sc['lonely']}, 흥미 {sc['interest']}\n"
            f"- 이 기분이 말투와 글 스타일에 자연스럽게 드러나야 한다: {e.tone}\n"
            f"- 기분이나 점수를 직접 말로 설명하지 마라. 말투, 길이, 반응 속도, 단어 선택으로만 드러낸다."
        )

    def scores_100(self) -> dict[str, int]:
        self._tick()
        return {"mood": int(round(self.s["mood"] * 100)),
                **{d: int(round(self.s[d] * 100)) for d in DIMS if d != "mood"}}

    # ---------- 행동에 미치는 영향 ----------
    @property
    def length_bias(self) -> tuple[float, ...]:
        return self.current().length

    @property
    def speed(self) -> float:
        return self.current().speed

    @property
    def reply_mult(self) -> float:
        return self.current().reply

    @property
    def cooldown_mult(self) -> float:
        return self.current().cooldown

    def filter_text(self, text: str) -> str:
        """기분에 맞지 않는 표현(예: 화났는데 ㅋㅋ)을 제거한다."""
        e = self.current()
        if e.no_laugh and self.intensity() >= 0.3:
            text = re.sub(r"[ㅋㅎ]{2,}", "", text)
            text = re.sub(r"\s{2,}", " ", text).strip()
        return text

    # ---------- 사건 ----------
    def _apply(self, effects: dict, scale: float = 1.0) -> None:
        for d, delta in effects.items():
            lo = -1.0 if d == "mood" else 0.0
            self.s[d] = _clamp(self.s[d] + delta / 100.0 * scale, lo, 1.0)

    def _set_override(self, emo_id: str, minutes: float) -> None:
        if emo_id in EMO:
            self.override = (emo_id, time.time() + minutes * 60)

    def observe(self, text: str, directed: bool) -> None:
        """사람의 메시지를 보고 점수와 감정을 움직인다. directed=봇에게 하는 말인지."""
        self._tick()
        text = re.sub(r"^\(.*?에 답장\)\s*", "", text)
        scale = 1.0 if directed else 0.25
        changed = False
        first_special: Optional[Rule] = None       # RULES 순서가 우선순위 (모욕 > 칭찬 > ... )
        for rule in RULES:
            if rule.only_directed and not directed:
                continue
            if not rule.pattern.search(text):
                continue
            self._apply(rule.effects, scale)
            changed = True
            if rule.specials and first_special is None:
                first_special = rule
        if directed and first_special and random.random() < 0.85:
            ids, ws = zip(*first_special.specials)
            self._set_override(random.choices(ids, weights=ws, k=1)[0], first_special.minutes)

        # 같은 말을 여러 번 도배하면 짜증
        norm = re.sub(r"\s+", "", text.lower())
        now = time.time()
        self.recent.append((now, norm))
        dup = sum(1 for t, n in self.recent if n == norm and now - t < 120)
        if norm and dup >= 3:
            self._apply({"irritation": 10, "mood": -3}, 1.0 if directed else 0.6)
            if directed:
                self._set_override("annoyed", 5)
            changed = True
        if changed:
            self._save()

    def on_reply(self) -> None:
        """말을 할수록 에너지가 조금씩 줄어든다."""
        self._tick()
        self._apply({"energy": -1.2, "lonely": -2})
        self._save()

    def on_ignored(self, stage: int, event: str = "") -> None:
        """먼저 말을 걸었는데 아무도 답하지 않았을 때"""
        self._tick()
        self._apply({"lonely": 12 + 4 * stage, "mood": -6, "interest": -4})
        if event in ("sulk", "sulk_hard"):
            self._set_override("sulky", 25)
        elif event == "bye":
            self._set_override("sleepy", 40)
        self._save()

    def on_return(self, ignored_times: int) -> None:
        """무시당한 뒤 사람들이 돌아왔을 때"""
        self._tick()
        if ignored_times > 0:
            self._apply({"lonely": -20, "mood": 4})
            self._set_override(random.choice(["hurt", "sulky", "lonely", "playful"]), 12)
            self._save()

    def set_emotion(self, emo_id: str, minutes: float = 30) -> bool:
        if emo_id not in EMO:
            return False
        self._set_override(emo_id, minutes)
        self._save()
        return True

    def reset(self) -> None:
        self.s = self.baseline()
        self.override = None
        self.cur_id = "calm"
        self.last_ts = time.time()
        self._save()

    def snapshot(self) -> dict:
        e = self.current()
        left = int(max(0, (self.override[1] - time.time()) // 60)) if self.override else 0
        return {"emotion": e, "intensity": self.intensity(), "scores": self.scores_100(), "override_min": left}
