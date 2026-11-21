"""채팅 비율 제어.

비율 = 최근 N분 동안 (봇이 보낸 메시지(말풍선) 수) ÷ (사람이 보낸 메시지 수).
목표는 기본 1.0~1.5(중심 1.25 ± 0.25) 안에서 랜덤하게 정해지고(몇 분마다 다시 뽑음), 현재 비율이 목표보다 낮으면
대답 확률·말풍선 수·먼저 말걸기를 올리고 쿨다운을 줄이며, 높으면 반대로 줄인다.
LLM 호출 횟수는 늘리지 않고, 한 번의 답을 말풍선 여러 개로 나누는 방식을 우선해서 속도를 지킨다.
"""
from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Adjust:
    reply: float = 1.0        # 대답 확률 배수
    cooldown: float = 1.0     # 쿨다운 배수 (작을수록 빨리 말함)
    bubbles: float = 1.0      # 말풍선 수 가중치 (>1: 여러 개로 나눠 말함, <1: 한 개로 짧게)
    initiate: float = 1.0     # 먼저 말걸기 배수
    ratio: float = 0.0
    target: float = 0.0
    human: int = 0
    bot: int = 0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class RatioControl:
    def __init__(self, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.center = float(cfg.get("target", 1.0))
        self.spread = float(cfg.get("spread", 0.65))
        self.window = float(cfg.get("window_min", 20)) * 60
        self.reroll = float(cfg.get("reroll_min", 8)) * 60
        self.min_human = int(cfg.get("min_human", 3))
        self.allow = float(cfg.get("allow", 2))      # 사람이 말하지 않아도 봇이 먼저 말할 수 있는 여유분(메시지 수)
        self._ev: dict[int, deque[tuple[float, str, int]]] = {}
        self._t: dict[int, tuple[float, float]] = {}

    # ---------- 기록 ----------
    def record(self, channel_id: int, kind: str, n: int = 1) -> None:
        """kind: 'h'(사람 메시지) / 'b'(봇 메시지)"""
        dq = self._ev.setdefault(channel_id, deque(maxlen=400))
        dq.append((time.time(), kind, n))
        self._purge(dq)

    def _purge(self, dq: deque) -> None:
        cut = time.time() - self.window
        while dq and dq[0][0] < cut:
            dq.popleft()

    def counts(self, channel_id: int) -> tuple[int, int]:
        dq = self._ev.get(channel_id)
        if not dq:
            return 0, 0
        self._purge(dq)
        h = sum(n for _, k, n in dq if k == "h")
        b = sum(n for _, k, n in dq if k == "b")
        return h, b

    # ---------- 목표 ----------
    def target(self, channel_id: int) -> float:
        now = time.time()
        t = self._t.get(channel_id)
        if t is None or now > t[0]:
            lo = max(0.2, self.center - self.spread)
            hi = max(lo, self.center + self.spread)
            t = (now + self.reroll * random.uniform(0.5, 1.5), random.uniform(lo, hi))   # (다음에 다시 뽑을 시각, 목표) — 주기도 랜덤
            self._t[channel_id] = t
        return t[1]

    def set_center(self, value: float) -> None:
        self.center = _clamp(float(value), 0.3, 4.0)
        self._t.clear()                       # 새 중심값으로 다시 뽑는다

    # ---------- 보정 ----------
    def deficit(self, channel_id: int) -> float:
        """봇이 목표보다 얼마나 조용한가. (+이면 더 말해야 함, -이면 너무 많이 말함, 범위 -0.6~0.6)"""
        h, b = self.counts(channel_id)
        if not self.enabled or h < self.min_human:
            return 0.0
        tgt = self.target(channel_id)
        return _clamp((tgt - b / h) / max(tgt, 0.2), -0.6, 0.6)

    def over_cap(self, channel_id: int) -> bool:
        """봇 메시지가 (목표 비율 x 사람 메시지 + 여유분) 이상이면 먼저 말하기를 막는다.
        사람이 거의 말하지 않는 구간에서도 봇이 혼자 떠드는 일을 막는 절대 상한이다."""
        if not self.enabled:
            return False
        h, b = self.counts(channel_id)
        return b >= self.target(channel_id) * h + self.allow

    def adjust(self, channel_id: int) -> Adjust:
        h, b = self.counts(channel_id)
        tgt = self.target(channel_id)
        ratio = b / h if h else 0.0
        capped = self.over_cap(channel_id)
        if not self.enabled or h < self.min_human:
            return Adjust(initiate=0.0 if capped else 1.0, ratio=ratio, target=tgt, human=h, bot=b)
        d = self.deficit(channel_id)
        if d >= 0:      # 봇이 조용함 → 더 말한다
            return Adjust(reply=1 + 0.8 * d, cooldown=1 - 0.8 * d, bubbles=1 + 1.2 * d,
                          initiate=0.0 if capped else 1 + 1.5 * d, ratio=ratio, target=tgt, human=h, bot=b)
        # 봇이 너무 많이 말함 → 줄인다
        return Adjust(reply=max(0.4, 1 + 1.0 * d), cooldown=1 - 1.2 * d, bubbles=max(0.3, 1 + 1.5 * d),
                      initiate=0.0 if capped else max(0.1, 1 + 2.0 * d), ratio=ratio, target=tgt, human=h, bot=b)
