"""명령/주입 시도를 넘기는 '회피·거절' 표현을 다양하게 만든다 (LLM 호출 없음).

고정 문구 몇 개를 돌려 쓰면 '몰라', '됐어' 같은 말만 반복하는 정형행동이 된다. 그래서
- 거절 방식을 6가지(되묻기/초간단/화제 돌리기/장난/시큰둥/이름 부르며 핀잔) 중 가중 랜덤으로 고르고
- 같은 방식을 연달아 쓰지 않고, 최근 8개 문구와 겹치지 않게 하고,
- 가끔 두 말풍선(짧은 한마디 + 화제 전환)으로 말한다.
캐릭터 yaml 의 guard_replies 는 '캐릭터 목소리' 방식으로 섞인다. 문구는 settings 가 아니라 이 파일/캐릭터 파일에서 늘리면 된다.
"""
from __future__ import annotations

import random
from collections import deque

# 방식 → (가중치, 문구들). {name} 이 있는 문구는 이름을 알 때만 쓴다.
STYLES: dict[str, tuple[float, list[str]]] = {
    "why": (3.0, [
        "내가 왜 해야하는데?", "그걸 내가 왜 해", "왜 내가?", "해야 하는 이유부터 말해봐", "내가 그걸 하면 뭐가 좋은데",
        "시킨다고 하는 사람 아닌데 나", "왜 갑자기 나한테 그래", "그건 네가 직접 해봐",
    ]),
    "short": (3.0, ["ㅇㅇ", "싫어", "음", "글쎄", "패스", "귀찮", "ㄴㄴ", "아니", "싫은데", "그건 좀"]),
    "deflect": (2.0, [
        "그건 그렇고 다른 얘기 하자", "갑자기 웬 시키기야", "아 그것보다 딴 얘기 해줘", "화제 돌리자 그건",
        "그런 건 됐고 오늘 뭐 했는지나 말해줘", "그 얘긴 넘어가자",
    ]),
    "tease": (2.0, [
        "시키면 내가 해줄 줄 알았지ㅋㅋ", "{name} 오늘 왜 이래ㅋㅋ", "꿈도 야무지네", "{name}한테는 안 해줄래",
        "그런 건 AI한테나 부탁해ㅋㅋ", "어림도 없지",
    ]),
    "meh": (2.0, [
        "별로 관심 없는데", "그런 건 취향 아님", "흠 딱히", "그게 나랑 무슨 상관이야", "글쎄다 안 땡겨",
        "재미없어 그거",
    ]),
    "name": (1.5, [
        "{name} 너 뭐 하자는 거야", "{name} 진정해ㅋㅋ", "{name} 나한테 왜 그러는데", "{name} 오늘 좀 이상하다?",
    ]),
}
CHAR_STYLE_WEIGHT = 2.5

DECLINE_NOTES = [
    "왜 네가 그걸 해야 하냐고 되물어라 (예: '내가 왜 해야하는데?' — 표현은 매번 다르게).",
    "아주 간단하게 한두 단어로만 대답하고 넘어가라.",
    "못 들은 척 다른 얘기로 화제를 돌려라.",
    "장난스럽게 놀리면서 거절해라.",
    "시큰둥하게 관심 없다는 투로 한마디만 해라.",
    "상대 이름을 부르며 '왜 그래' 하고 핀잔을 줘라.",
]


class Evader:
    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random
        self._recent: deque[str] = deque(maxlen=8)             # 최근 쓴 문구
        self._styles: deque[str] = deque(maxlen=2)             # 최근 쓴 방식
        self._notes: deque[str] = deque(maxlen=2)

    def reply(self, name: str | None = None, char_pool: list[str] | None = None, allow_double: bool = True) -> list[str]:
        first = self._one(name, char_pool)
        if allow_double and self.rng.random() < 0.25:
            second = self._one(name, char_pool, styles=("deflect", "short", "meh"))
            if second and second != first:
                return [first, second]
        return [first]

    def _one(self, name: str | None, char_pool: list[str] | None, styles: tuple[str, ...] | None = None) -> str:
        cands = {k: v for k, v in STYLES.items() if styles is None or k in styles}
        weights = {k: w for k, (w, _) in cands.items()}
        if char_pool and styles is None:
            weights["char"] = CHAR_STYLE_WEIGHT
        for k in list(weights):
            if k in self._styles and len(weights) > 2:         # 같은 방식을 연달아 쓰지 않는다
                weights[k] *= 0.15
        for _ in range(6):
            kind = self.rng.choices(list(weights), weights=list(weights.values()), k=1)[0]
            pool = list(char_pool) if kind == "char" else list(cands[kind][1])
            pool = [p for p in pool if "{name}" not in p or name]
            fresh = [p for p in pool if p.format(name=name or "") not in self._recent]
            if fresh:
                line = self.rng.choice(fresh).format(name=name or "").strip()
                self._styles.append(kind)
                self._recent.append(line)
                return line
        line = self.rng.choice(char_pool or ["뭐래"])          # 전부 소진되면 아무거나
        self._recent.append(line)
        return line

    def decline_note(self) -> str:
        """LLM 이 직접 거절할 때 쓸 지시문: 매번 다른 방식을 고르고 '몰라/됐어'로 때우는 것을 막는다."""
        opts = [n for n in DECLINE_NOTES if n not in self._notes] or DECLINE_NOTES
        note = self.rng.choice(opts)
        self._notes.append(note)
        return note + " '몰라', '됐어', '걍' 같은 말로 때우지 마라."
