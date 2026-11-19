"""캐릭터의 기분/상태/마음 시스템.

- 상태 점수 5개(기분, 에너지, 짜증, 외로움, 흥미)가 시간 경과와 대화 내용에 따라 실시간으로 움직인다.
- 점수 조합과 대화 사건으로 30가지 감정 중 하나가 정해진다.
- 최근 사건에서 생긴 '마음속 심정'(이유)을 기억해서 프롬프트에 넣는다.
- 사람마다 호감도(-1~1)가 따로 있어서, 같은 말이라도 누구에게 하느냐에 따라 말투의 온도가 달라진다.
- 채널 분위기(활발/썰렁/과열...)도 조금씩 기분에 영향을 준다.
- 위 값들로 '말투 조절 항목'(따뜻함, 웃음, 텐션, 격식, 감탄사)을 계산해 매번 프롬프트에 구체적으로 지시한다.
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
HALF_LIFE_MIN = {"mood": 30.0, "energy": 45.0, "irritation": 12.0, "lonely": 40.0, "interest": 25.0}
ENERGY_BY_HOUR = [0.15, 0.10, 0.08, 0.08, 0.10, 0.15, 0.25, 0.40, 0.50, 0.60, 0.65, 0.65,
                  0.60, 0.55, 0.55, 0.60, 0.60, 0.65, 0.70, 0.70, 0.65, 0.60, 0.45, 0.30]
DEFAULT_BASE = {"mood": 0.2, "irritation": 0.1, "lonely": 0.15, "interest": 0.5}
AFF_BASE = 0.1          # 처음 보는 사람에 대한 기본 호감도
THOUGHT_TTL_MIN = 25    # 마음속 심정이 프롬프트에 남는 시간


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
    formal: bool = False                        # 존댓말이 섞여도 자연스러운 감정
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
            (-0.2, 0.45, 0.5, 0.0, 0.0), (1.8, 1.2, 0.5, 0.1), reply=0.6, no_laugh=True, formal=True),
    # ---- 이벤트형 (11) ----
    Emotion("smug", "의기양양", "으스대는 말투. 자신만만하게 한마디 하고 가볍게 비웃듯 말한다. 은근히 자랑이 섞인다.",
            length=(1.0, 1.3, 1.0, 0.6), reply=1.1, special=True),
    Emotion("proud", "뿌듯", "뿌듯하고 우쭐한 말투. 어깨가 올라간 느낌으로 말한다.", special=True),
    Emotion("shy", "쑥스러움", "쑥스러워서 얼버무린다. 말을 끊어 말하고 화제를 돌리려 한다. 말수가 줄어든다.",
            length=(1.5, 1.2, 0.6, 0.2), speed=0.8, formal=True, special=True),
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
            length=(1.2, 1.2, 1.0, 0.4), speed=1.2, formal=True, special=True),
    Emotion("dumbfounded", "어이없음", "어이가 없어서 말문이 막힌다. 황당해하며 '뭐라는 거야' 식으로 짧게 반문한다.",
            length=(1.6, 1.3, 0.6, 0.1), special=True),
]
EMO = {e.id: e for e in EMOTIONS}
assert len(EMOTIONS) == 30


# ---------- 대화 사건 규칙 (앞에 있을수록 우선순위 높음) ----------
@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    effects: dict           # 점수 변화 (-100~100 스케일)
    specials: tuple = ()    # ((감정 id, 가중치), ...)
    minutes: float = 6.0    # 감정 지속 시간
    only_directed: bool = False
    reason: str = ""        # 마음속 심정으로 남길 문장
    aff: float = 0.0        # 그 사람에 대한 호감도 변화


def _r(name, pat, effects, specials=(), minutes=6.0, only_directed=False, reason="", aff=0.0) -> Rule:
    return Rule(name, re.compile(pat, re.I), effects, tuple(specials), minutes, only_directed, reason, aff)


RULES: list[Rule] = [
    _r("insult", r"죽을래|꺼져|닥쳐|바보|멍청|병신|시발|씨발|ㅅㅂ|ㅂㅅ|저능|쓰레기|개못|못해|못하네|못함|못한다|한심",
       {"mood": -12, "irritation": 18}, (("angry", 1), ("annoyed", 2), ("hurt", 2), ("dumbfounded", 1)), 8, True,
       "놀림이나 모욕을 당해서 기분이 상했다", -0.08),
    _r("praise", r"귀엽|예쁘|이쁘|천재|잘한다|잘하네|잘해|최고|멋지|대박|짱이|굿|고마워|감사|똑똑",
       {"mood": 12, "interest": 5}, (("shy", 2), ("proud", 2), ("flutter", 1), ("smug", 2)), 6, True,
       "칭찬을 받아서 쑥스럽고 기분이 좋다", 0.06),
    _r("affection", r"사랑해|좋아해|보고\s*싶|결혼|내 거|내꺼",
       {"mood": 10, "lonely": -15}, (("flustered", 3), ("shy", 3), ("flutter", 2)), 8, True,
       "갑자기 좋아한다는 말을 들어서 당황스럽다", 0.05),
    _r("sadness", r"우울|힘들|지쳤|죽고\s*싶|슬퍼|ㅠㅠ|ㅜㅜ|망했|아파|외로|울고",
       {"mood": -4, "energy": -3}, (("worried", 3), ("caring", 3), ("sad", 1)), 8, False,
       "누가 힘들어 보여서 신경 쓰인다", 0.02),
    _r("fun", r"게임|노래|파티|놀자|같이|이벤트|치킨|피자|맛있|여행|놀러",
       {"interest": 10, "energy": 5, "mood": 3}, (("curious", 2), ("excited", 1), ("playful", 1)), 5, False,
       "재미있어 보이는 화제가 나와서 끌린다"),
    _r("laugh", r"ㅋ{3,}|ㅎ{3,}", {"mood": 3, "energy": 2}, aff=0.003),
    _r("task", r"레시피|번역|코드|작성해|써줘|알려줘|해줘|만들어줘|설명해",
       {"irritation": 6, "energy": -3}, (), 5, True, "귀찮은 일을 시키려 해서 짜증 난다", -0.01),
    _r("greeting", r"안녕|ㅎㅇ|하이|왔어|왔다", {"mood": 4, "lonely": -10}, reason="인사를 받아서 반갑다", aff=0.01),
    _r("danger", r"위험|조심|큰일|사고|다쳐|응급|살려", {"interest": 5}, (("worried", 3), ("anxious", 2)), 6, False,
       "위험하다는 말이 나와서 걱정된다"),
    _r("work", r"공부|과제|시험|일해|개발|코딩|작업|야근", {"interest": 6}, (("focused", 2), ("curious", 1)), 6, False,
       "공부/작업 이야기가 나왔다"),
]

# ---------- 채널 분위기 ----------
INSULT_RE = re.compile(r"죽을래|꺼져|닥쳐|바보|멍청|병신|시발|씨발|ㅅㅂ|ㅂㅅ|한심|쓰레기")
SERIOUS_RE = re.compile(r"진짜\s*심각|문제|고민|걱정|힘들|어떡|도와|부탁|사실은")
ATMOSPHERES = {
    "썰렁한 정적": ("말이 없다. 화제를 던지거나 조용히 있어도 자연스럽다", {"lonely": 3, "interest": -1}),
    "과열된 말싸움": ("서로 날이 서 있다. 편들기보다 한 발 물러서서 한마디 하거나 말리는 쪽이 자연스럽다", {"irritation": 3, "energy": 1}),
    "정신없이 빠른 수다판": ("말이 빠르게 오간다. 아주 짧게 끼어든다", {"energy": 1, "irritation": 1.5}),
    "다 같이 웃고 떠드는 분위기": ("가볍고 신난다. 드립을 받아주고 같이 웃는다", {"mood": 2, "energy": 1}),
    "진지한 분위기": ("진지한 이야기 중이다. 농담을 줄이고 신중하게 말한다", {"interest": 1, "mood": -0.5}),
    "편한 잡담": ("편하게 수다를 떨고 있다. 가볍게 끼어든다", {}),
    "잔잔한 대화": ("조용히 이어지는 대화다. 차분하게 말한다", {}),
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def atmosphere_of(hist: list[dict]) -> tuple[str, str]:
    """최근 5분 대화로 채널 분위기를 판정한다. (이름, 행동 지침)"""
    now = time.time()
    msgs = [h for h in hist if h["role"] == "user" and now - h["ts"] <= 300]
    n = len(msgs)
    last_gap = (now - msgs[-1]["ts"]) if msgs else 9999
    if n <= 1 and last_gap > 600 or n == 0:
        name = "썰렁한 정적"
    else:
        texts = [m["content"] for m in msgs]
        laugh = sum(len(re.findall(r"[ㅋㅎ]", t)) for t in texts) / n
        insult = sum(1 for t in texts if INSULT_RE.search(t)) / n
        serious = sum(1 for t in texts if SERIOUS_RE.search(t))
        if insult >= 0.3 and n >= 3:
            name = "과열된 말싸움"
        elif n >= 12:
            name = "정신없이 빠른 수다판"
        elif laugh >= 4 and n >= 4:
            name = "다 같이 웃고 떠드는 분위기"
        elif serious >= 2:
            name = "진지한 분위기"
        elif n >= 4:
            name = "편한 잡담"
        else:
            name = "잔잔한 대화"
    return name, ATMOSPHERES[name][0]


class Mood:
    def __init__(self, memory: Memory, persona, tz) -> None:
        self.mem = memory
        self.persona = persona
        self.tz = tz
        self.override: Optional[tuple[str, float]] = None     # (감정 id, 만료 시각)
        self.cur_id = "calm"
        self.last_ts = time.time()
        self.recent: deque[tuple[float, str]] = deque(maxlen=8)      # 도배 감지용
        self.thoughts: deque[tuple[float, str]] = deque(maxlen=6)    # (시각, 마음속 심정)
        self.aff: dict[int, float] = {}                              # 유저별 호감도
        self._atm_ts = 0.0
        self._tasks: set[asyncio.Task] = set()
        self.engage = 1.0                 # 적극성 (1.0=기본, 높을수록 대화에 적극적). settings.yaml engagement.level
        self.energy_floor = 0.45          # 적극성이 높을 때 에너지가 이 밑으로 떨어지지 않게 하는 바닥값
        self.s: dict[str, float] = self.baseline()

    # ---------- 기준값 / 저장 ----------
    def baseline(self) -> dict[str, float]:
        cfg = (self.persona.data.get("mood_baseline") or {}) if self.persona else {}
        hour = datetime.now(self.tz).hour
        b = {k: float(cfg.get(k, v)) for k, v in DEFAULT_BASE.items()}
        e = _clamp(self.engage_amount(), 0.0, 1.0)      # 0(기본)~1(매우 적극)
        energy = ENERGY_BY_HOUR[hour] + float(cfg.get("energy_bias", 0.0))
        energy = max(energy, self.energy_floor * e)     # 새벽에도 졸림/귀찮음 감정으로 빠지지 않게 바닥을 깐다
        b["energy"] = _clamp(energy, 0.02, 1.0)
        b["mood"] = _clamp(b["mood"] + 0.15 * e, -1.0, 1.0)
        b["interest"] = _clamp(b["interest"] + 0.12 * e, 0.0, 1.0)
        return b

    def engage_amount(self) -> float:
        """적극성 1.0→0, 1.5 이상→1. 이 값만큼 '게으른 쪽' 보정을 약하게 한다."""
        return _clamp((self.engage - 1.0) / 0.5, 0.0, 1.0)

    def _soft(self, v: float, lazy_side_below: bool) -> float:
        """감정이 주는 '귀찮은 쪽' 페널티(대답 확률 감소, 쿨다운 증가, 말 느려짐)를 적극성만큼 1.0 쪽으로 되돌린다."""
        d = self.engage_amount() * 0.7
        lazy = (v < 1.0) if lazy_side_below else (v > 1.0)
        return 1.0 + (v - 1.0) * (1.0 - d) if lazy else v

    def boost_to_baseline(self) -> None:
        """적극성을 올렸을 때 지금 상태에도 바로 반영: 에너지/기분을 새 기준값 이상으로 끌어올리고 처진 감정 고정을 푼다."""
        base = self.baseline()
        self.s["energy"] = max(self.s["energy"], base["energy"])
        self.s["mood"] = max(self.s["mood"], base["mood"])
        if self.override and self.override[0] in ("sleepy", "tired", "lazy", "listless", "bored", "gloomy"):
            self.override = None
        self._save()

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
            self.aff = {int(k): float(v) for k, v in (d.get("aff") or {}).items()}
        except Exception:  # noqa: BLE001
            log.warning("기분 상태 복원 실패, 기본값 사용")

    def _save(self) -> None:
        data = json.dumps({"s": self.s, "cur": self.cur_id, "override": self.override, "ts": self.last_ts,
                           "aff": {str(k): round(v, 3) for k, v in self.aff.items()}})
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
        self.cur_id = cur if cur and dists[cur] <= dists[best] + 0.008 else best   # 깜빡임 방지
        return EMO[self.cur_id]

    def intensity(self) -> float:
        """기준값에서 얼마나 벗어났는가 (0~1). 이벤트 감정은 최소 0.5."""
        self._tick()
        base = self.baseline()
        dev = math.sqrt(sum((self.s[d] - base[d]) ** 2 for d in DIMS) / len(DIMS))
        val = _clamp(dev * 2.4, 0.0, 1.0)
        return max(val, 0.5) if self.override else val

    # ---------- 호감도 ----------
    def affinity(self, uid: Optional[int]) -> float:
        return self.aff.get(uid, AFF_BASE) if uid is not None else AFF_BASE

    @staticmethod
    def aff_label(a: float) -> tuple[str, str]:
        if a >= 0.6:
            return "아주 친함", "허물없이 편하게 대하고 장난도 많이 친다"
        if a >= 0.3:
            return "친함", "편하고 다정하게 대한다"
        if a >= -0.1:
            return "보통", "평범하게 대한다"
        if a >= -0.4:
            return "서먹하고 불편함", "거리를 두고 조금 퉁명스럽게 대한다"
        return "싫어함", "차갑고 시큰둥하게 대하며 말을 길게 섞지 않는다"

    def _adj_aff(self, uid: Optional[int], delta: float) -> None:
        if uid is None or not delta:
            return
        self.aff[uid] = _clamp(self.affinity(uid) + delta, -1.0, 1.0)

    # ---------- 마음속 심정 ----------
    def add_thought(self, text: str, who: str = "") -> None:
        if not text:
            return
        full = f"{who}: {text}" if who else text
        now = time.time()
        if self.thoughts and self.thoughts[-1][1] == full and now - self.thoughts[-1][0] < 180:
            return
        self.thoughts.append((now, full))

    def _recent_thoughts(self) -> list[str]:
        now = time.time()
        out = []
        for ts, t in reversed(self.thoughts):
            age = (now - ts) / 60
            if age <= THOUGHT_TTL_MIN:
                out.append(f"{t} ({int(age)}분 전)" if age >= 1 else f"{t} (방금)")
            if len(out) >= 3:
                break
        return out

    # ---------- 프롬프트용 ----------
    def scores_100(self) -> dict[str, int]:
        self._tick()
        return {"mood": int(round(self.s["mood"] * 100)),
                **{d: int(round(self.s[d] * 100)) for d in DIMS if d != "mood"}}

    def knobs(self, uid: Optional[int] = None) -> list[str]:
        """점수와 호감도로 계산한 구체적 말투 조절 항목 (감정 라벨보다 세밀하게 실시간 반영)"""
        e = self.current()
        s = self.s
        aff = self.affinity(uid)
        warmth = 0.5 * s["mood"] - 0.7 * s["irritation"] + 0.5 * aff
        out = []
        if warmth >= 0.45:
            out.append("말투: 따뜻하고 다정하다")
        elif warmth >= 0.1:
            out.append("말투: 평범하게 친근하다")
        elif warmth >= -0.2:
            out.append("말투: 조금 무심하고 담백하다")
        else:
            out.append("말투: 차갑고 퉁명스럽다")
        if e.no_laugh:
            out.append("웃음: ㅋㅋ/ㅎㅎ를 쓰지 않는다")
        elif s["mood"] > 0.45 and s["energy"] > 0.45:
            out.append("웃음: ㅋㅋ를 자주 쓴다")
        elif s["mood"] > 0.1:
            out.append("웃음: ㅋㅋ를 가끔 쓴다")
        else:
            out.append("웃음: ㅋㅋ를 거의 쓰지 않는다")
        tension = 0.6 * s["energy"] + 0.4 * s["interest"]
        if tension > 0.75:
            out.append("텐션: 높다 (반응이 빠르고 들떠 있다)")
        elif tension > 0.45:
            out.append("텐션: 보통")
        else:
            out.append("텐션: 낮다 (힘 뺀 느린 말투)")
        out.append("격식: 가끔 존댓말이 섞여도 어색하지 않다" if (e.formal or aff < -0.4)
                   else "격식: 편한 반말")
        out.append("감탄사/느낌표: 한 번까지 허용" if (s["energy"] > 0.7 and s["mood"] > 0.3)
                   else "감탄사/느낌표: 거의 쓰지 않는다")
        return out

    def describe(self, uid: Optional[int] = None, who: str = "") -> str:
        e = self.current()
        it = self.intensity()
        level = "아주 약하게" if it < 0.25 else "약하게" if it < 0.5 else "뚜렷하게" if it < 0.75 else "매우 강하게"
        sc = self.scores_100()
        lines = [
            f"- 기분: {e.label}({level}) · 점수 기분{sc['mood']:+d} 에너지{sc['energy']} 짜증{sc['irritation']} "
            f"외로움{sc['lonely']} 흥미{sc['interest']}",
        ]
        th = self._recent_thoughts()
        if th:
            lines.append("- 마음속(겉으로 말하진 않고 말투에 묻어남): " + " / ".join(th))
        if uid is not None:
            label, guide = self.aff_label(self.affinity(uid))
            lines.append(f"- {who or '이 사람'}에 대한 호감도: {label} → {guide}")
        lines.append("- 말투: " + e.tone + " [" + "; ".join(self.knobs(uid)) + "]")
        lines.append("- 기분/점수를 직접 말로 설명하지 말고 말투와 길이로만 드러낸다.")
        return "\n".join(lines)

    # ---------- 행동에 미치는 영향 ----------
    @property
    def length_bias(self) -> tuple[float, ...]:
        d = self.engage_amount() * 0.7          # 졸림/피곤 같은 감정의 '짧게 말하기' 쏠림을 적극성만큼 완화
        return tuple(1.0 + (x - 1.0) * (1.0 - d) for x in self.current().length)

    @property
    def speed(self) -> float:
        return self._soft(self.current().speed, lazy_side_below=True)

    @property
    def reply_mult(self) -> float:
        v = self._soft(self.current().reply, lazy_side_below=True)
        return v * (1.0 + 0.15 * _clamp(self.engage - 1.0, 0.0, 1.0))      # 적극적일수록 더 자주 끼어든다

    @property
    def cooldown_mult(self) -> float:
        return self._soft(self.current().cooldown, lazy_side_below=False)

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

    def observe(self, text: str, directed: bool, uid: Optional[int] = None, who: str = "") -> None:
        """사람의 메시지를 보고 점수, 감정, 마음속 심정, 호감도를 움직인다."""
        self._tick()
        text = re.sub(r"^\(.*?에 답장\)\s*", "", text)
        scale = 1.0 if directed else 0.25
        changed = False
        first_special: Optional[Rule] = None       # RULES 순서가 우선순위 (모욕 > 칭찬 > ...)
        for rule in RULES:
            if rule.only_directed and not directed:
                continue
            if not rule.pattern.search(text):
                continue
            self._apply(rule.effects, scale)
            self._adj_aff(uid, rule.aff * scale)
            if directed and rule.reason:
                self.add_thought(rule.reason, who)
            changed = True
            if rule.specials and first_special is None:
                first_special = rule
        if directed and first_special and random.random() < 0.85:
            ids, ws = zip(*first_special.specials)
            self._set_override(random.choices(ids, weights=ws, k=1)[0], first_special.minutes)
        if directed:
            self._adj_aff(uid, 0.004)               # 말을 걸어주는 것 자체로 조금씩 친해진다
            changed = True

        # 같은 말을 여러 번 도배하면 짜증
        norm = re.sub(r"\s+", "", text.lower())
        now = time.time()
        self.recent.append((now, norm))
        dup = sum(1 for t, n in self.recent if n == norm and now - t < 120)
        if norm and dup >= 3:
            self._apply({"irritation": 10, "mood": -3}, 1.0 if directed else 0.6)
            self.add_thought("같은 말을 계속 도배해서 짜증 난다", who)
            self._adj_aff(uid, -0.03)
            if directed:
                self._set_override("annoyed", 5)
            changed = True
        if changed:
            self._save()

    def on_injection(self, uid: Optional[int], who: str = "") -> None:
        """명령/프롬프트 주입 시도를 받았을 때"""
        self._tick()
        self._apply({"irritation": 10, "mood": -4, "interest": 3})
        self._adj_aff(uid, -0.05)
        self.add_thought("이상한 명령/지시를 해서 어이가 없고 조금 질린다", who)
        self._set_override(random.choice(["dumbfounded", "dumbfounded", "annoyed", "smug"]), 5)
        self._save()

    def on_reaction(self, uid: Optional[int], sign: int, who: str = "") -> None:
        """누가 내 메시지에 이모지 리액션을 달았을 때 (sign: +1 호의적 / -1 부정적 / 0 중립)"""
        self._tick()
        if sign > 0:
            self._apply({"mood": 3, "lonely": -3})
            self._adj_aff(uid, 0.01)
            self.add_thought("내 말에 호의적인 리액션이 달려서 기분이 좋다", who)
        elif sign < 0:
            self._apply({"mood": -3, "irritation": 4})
            self._adj_aff(uid, -0.015)
            self.add_thought("내 말에 비웃는 듯한 리액션이 달려서 기분이 상한다", who)
        else:
            return
        self._save()

    def on_fight_start(self, uid: Optional[int], who: str = "") -> None:
        """말싸움이 시작될 때: 열이 오르고 기세가 오른다"""
        self._tick()
        self._apply({"irritation": 8, "energy": 6, "interest": 8})
        self.add_thought("누가 시비를 걸어서 말싸움이 붙었다. 질 수 없다", who)
        if not self.override or self.override[0] not in ("angry", "annoyed"):
            self._set_override(random.choice(["annoyed", "angry", "playful"]), 6)
        self._save()

    def on_fight_result(self, result: str, uid: Optional[int], who: str = "") -> None:
        """말싸움이 끝났을 때 결과를 기분/호감도/마음에 반영한다"""
        self._tick()
        if result in ("bot_win", "timeout_win", "retreat"):
            self._apply({"mood": 8, "interest": 5, "irritation": -10})
            self.add_thought("말싸움에서 이겨서 속이 시원하고 으쓱하다", who)
            self._set_override(random.choice(["smug", "smug", "playful"]), 8)
        elif result in ("bot_lose", "timeout_quiet"):
            self._apply({"mood": -10, "irritation": 8, "lonely": 6, "energy": -6})
            self.add_thought("말싸움에서 져서 억울하고 서럽다", who)
            self._set_override(random.choice(["hurt", "sulky"]), 12)
            self._adj_aff(uid, -0.03)
        elif result == "calmed":
            self._apply({"irritation": -25, "mood": -2})
            self.add_thought("상대가 진심으로 속상해 보여서 미안하다", who)
            self._set_override("caring", 8)
            self._adj_aff(uid, 0.05)
        else:                                  # draw
            self._apply({"energy": -8, "irritation": -12})
            self._set_override("tired", 6)
        self._save()

    def on_atmosphere(self, name: str) -> None:
        """채널 분위기가 기분에 아주 조금 스며든다 (60초에 한 번)"""
        now = time.time()
        if now - self._atm_ts < 60:
            return
        self._atm_ts = now
        eff = ATMOSPHERES.get(name, ("", {}))[1]
        if eff:
            self._tick()
            self._apply(eff)
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
        self.add_thought("먼저 말을 걸었는데 아무도 대답해주지 않아서 서운하고 외롭다")
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
            self.add_thought("한참 아무도 없다가 이제야 사람들이 돌아왔다")
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
        self.thoughts.clear()
        self._save()

    def snapshot(self, uid: Optional[int] = None) -> dict:
        e = self.current()
        left = int(max(0, (self.override[1] - time.time()) // 60)) if self.override else 0
        return {"emotion": e, "intensity": self.intensity(), "scores": self.scores_100(),
                "override_min": left, "thoughts": self._recent_thoughts(), "knobs": self.knobs(uid)}
