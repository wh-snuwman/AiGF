"""대답 대상 선정(점수)과 대화 인원에 따른 속도 배율."""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass


@dataclass
class Candidate:
    uid: int
    text: str
    age: float                    # 메시지가 올라온 지 몇 초
    priority: bool = False        # 멘션 / 봇 메시지에 대한 답장
    in_fight: bool = False
    directed: bool = False        # 봇 이름을 불렀는가
    human_reply: bool = False     # 다른 사람 메시지에 대한 답장인가
    has_image: bool = False
    affinity: float = 0.1
    since_replied: float = 9999   # 마지막으로 이 사람에게 대답한 뒤 몇 초
    is_last_target: bool = False  # 직전에 대답한 상대인가


def score(c: Candidate) -> float:
    s = 1.0
    if c.priority:
        s += 6.0
    if c.in_fight:
        s += 6.0
    if c.directed:
        s += 2.5
    if re.search(r"[?？]", c.text):
        s += 0.8
    s += 1.5 * max(0.0, c.affinity)                       # 친한 사람을 조금 더 챙긴다
    s += min(2.0, c.since_replied / 120.0)                # 오래 대답 못 받은 사람
    s += max(0.0, 1.0 - c.age / 60.0)                     # 최신 메시지
    if c.has_image:
        s += 1.0
    if c.is_last_target and not (c.priority or c.in_fight):
        s *= 0.6                                          # 같은 사람만 계속 상대하지 않는다
    if len(c.text.strip()) <= 3:
        s *= 0.6
    if c.human_reply and not (c.priority or c.directed):
        s *= 0.7                                          # 다른 사람에게 하는 말에는 덜 끼어든다
    return max(0.05, s)


def pick(cands: list[Candidate]) -> int:
    """점수^1.5 로 가중 랜덤. 인덱스를 돌려준다."""
    weights = [score(c) ** 1.5 for c in cands]
    return random.choices(range(len(cands)), weights=weights, k=1)[0]


def crowd(events: list[tuple[float, int]], window_sec: float, now: float | None = None) -> tuple[int, int]:
    """최근 window_sec 동안의 (말한 사람 수, 메시지 수)"""
    now = now or time.time()
    recent = [(t, u) for t, u in events if now - t <= window_sec]
    return len({u for _, u in recent}), len(recent)


def crowd_factor(users: int, msgs: int, boost: float, min_ratio: float) -> float:
    """쿨다운에 곱할 배율 (1.0 = 그대로, 작을수록 빨리 말한다). 사람이 많고 메시지가 많을수록 작아진다."""
    f = 1.0 / (1.0 + boost * max(0, users - 1) + 0.04 * max(0, msgs - 4))
    return max(min_ratio, f)


def chance_boost(users: int, per_user: float = 0.05, cap: float = 0.25) -> float:
    return min(cap, per_user * max(0, users - 1))
