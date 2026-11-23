"""캐릭터 가상 생활 스케줄: 시간표(구조화 데이터)로 '지금 무슨 활동을 어디서 하는지'를 계산한다.

- LLM 을 부르지 않는다. 시각만으로 현재 블록을 계산하고, 대화에 필요한 한 줄만 프롬프트에 넣는다.
- 캐릭터 yaml 의 `schedule:` 을 쓴다. 없으면 기본 시간표(DEFAULT)를 쓴다.
- 장소는 공통 장소(집/학교/회사/편의점/거리/공원)와 캐릭터 전용 장소(yaml `places:`)를 구분한다.
  세계관에 안 맞는 장소는 자동으로 추가하지 않는다 (캐릭터가 적은 장소만 쓴다).

yaml 예:
schedule:
  places: {shrine: 신사, workshop: 개인 작업실}      # 캐릭터 전용 장소
  blocks:
    - at: "02:00-10:00"      # 자정을 넘겨도 된다 ("23:00-07:00")
      do: 자는 중
      place: home
      sleep: true            # 잠든 상태 (채팅 빈도 크게 감소, 능동 말걸기 안 함)
      energy: -0.3           # 기분 에너지 보정 (-0.5~0.5)
      chat: 0.2              # 대답/먼저 말하기 빈도 배수
      length: 0.6            # 답변 길이 배수
      days: all              # all / weekday / weekend
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

COMMON_PLACES = {"home": "집", "school": "학교", "office": "회사", "store": "편의점", "street": "거리", "park": "공원"}

# 캐릭터 설정이 없을 때 쓰는 기본 시간표 (평범한 올빼미 학생/직장인 사이의 무난한 하루)
DEFAULT_BLOCKS = [
    {"at": "03:00-09:00", "do": "자는 중", "place": "home", "sleep": True, "energy": -0.3, "chat": 0.15, "length": 0.6},
    {"at": "09:00-11:00", "do": "막 일어나 준비하는 중", "place": "home", "energy": -0.15, "chat": 0.6, "length": 0.8},
    {"at": "11:00-14:00", "do": "바깥에서 활동하는 중", "place": "street", "energy": 0.05, "chat": 0.9, "length": 1.0},
    {"at": "14:00-18:00", "do": "자기 할 일을 하는 중", "place": "home", "energy": 0.0, "chat": 1.0, "length": 1.0},
    {"at": "18:00-22:00", "do": "저녁 먹고 쉬는 중", "place": "home", "energy": 0.1, "chat": 1.15, "length": 1.1},
    {"at": "22:00-03:00", "do": "늦은 밤 자기 시간", "place": "home", "energy": 0.05, "chat": 0.9, "length": 1.0},
]


@dataclass
class Block:
    start: int                      # 분 (0~1439)
    end: int
    do: str = ""
    place: str = "home"
    sleep: bool = False
    energy: float = 0.0
    chat: float = 1.0
    length: float = 1.0
    days: str = "all"

    def covers(self, minute: int) -> bool:
        if self.start <= self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end      # 자정을 넘는 구간


def _minutes(hhmm: str) -> int:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(hhmm))
    if not m:
        raise ValueError(f"시간 형식 오류: {hhmm!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 24 and 0 <= mi < 60):
        raise ValueError(f"시간 범위 오류: {hhmm!r}")
    return (h % 24) * 60 + mi


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class Schedule:
    def __init__(self, cfg: dict | None = None, tz: Any = None) -> None:
        cfg = cfg or {}
        self.tz = tz
        self.places: dict[str, str] = {**COMMON_PLACES}
        self.own_places: dict[str, str] = {str(k): str(v) for k, v in (cfg.get("places") or {}).items()}
        self.places.update(self.own_places)
        self.blocks: list[Block] = []
        for raw in (cfg.get("blocks") or DEFAULT_BLOCKS):
            try:
                a, b = str(raw["at"]).split("-")
                self.blocks.append(Block(
                    start=_minutes(a), end=_minutes(b), do=str(raw.get("do", "")), place=str(raw.get("place", "home")),
                    sleep=bool(raw.get("sleep", False)), energy=_clamp(float(raw.get("energy", 0.0)), -0.5, 0.5),
                    chat=_clamp(float(raw.get("chat", 1.0)), 0.05, 2.0), length=_clamp(float(raw.get("length", 1.0)), 0.3, 1.8),
                    days=str(raw.get("days", "all"))))
            except (KeyError, ValueError, TypeError):
                continue                                          # 잘못된 블록 하나가 전체를 막지 않게 건너뛴다
        if not self.blocks:
            self.blocks = Schedule(None, tz).blocks

    def _now(self, now: datetime | None) -> datetime:
        return now or (datetime.now(self.tz) if self.tz else datetime.now())

    def current(self, now: datetime | None = None) -> Block:
        n = self._now(now)
        minute = n.hour * 60 + n.minute
        weekend = n.weekday() >= 5
        fits = [b for b in self.blocks if b.covers(minute) and (b.days == "all" or (b.days == "weekend") == weekend)]
        # 요일 지정 블록이 'all' 블록보다 우선한다
        fits.sort(key=lambda b: b.days == "all")
        return fits[0] if fits else self.blocks[0]

    def place_name(self, block: Block) -> str:
        return self.places.get(block.place, block.place)

    def asleep(self, now: datetime | None = None) -> bool:
        return self.current(now).sleep

    def line(self, now: datetime | None = None) -> str:
        """프롬프트에 넣을 한 줄 (아주 짧게)"""
        b = self.current(now)
        return f"활동:{self.place_name(b)}에서 {b.do}" if b.do else ""

    def effects(self, now: datetime | None = None) -> tuple[float, float, float]:
        """(에너지 보정, 대답/먼저말하기 빈도 배수, 답변 길이 배수)"""
        b = self.current(now)
        return b.energy, b.chat, b.length
