"""캐릭터별 글 스타일: 말줄임표, 느낌표, 물결, 웃음(ㅋㅋ), 머뭇거림을 캐릭터 성격에 맞게 허용하고 코드가 입혀 준다.

기본 규칙은 디스코드 채팅처럼 담백하게(기호 제거)지만, 소심한 캐릭터는 말줄임표를, 순진하고 들뜬 캐릭터는 느낌표를 쓰는 게 자연스럽다.
캐릭터 yaml 의 `style:` 에 적는다 (없으면 기본 = 담백):

style:
  ellipsis: true          # 말줄임표(...) 허용 (모델이 쓴 것을 지우지 않는다)
  ellipsis_rate: 0.6      # 코드가 말끝에 말줄임표를 붙일 확률 (감정이 소심/불안일수록 더 자주)
  exclaim: true           # 느낌표 허용 (최대 두 개)
  exclaim_rate: 0.5       # 코드가 말끝에 느낌표를 붙일 확률
  tilde: false            # 물결(~) 허용
  laugh: none             # none 이면 ㅋㅋ 를 쓰지 않는다 (normal 이 기본)
  hesitate: ["저기...", "아..."]   # 말 앞에 가끔 붙이는 머뭇거림
  hesitate_rate: 0.4
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

TIMID = {"shy", "anxious", "flustered", "worried", "sad", "hurt", "lonely", "gloomy", "flutter"}   # 말을 흐리기 쉬운 감정
EXCITED = {"joy", "excited", "playful", "proud", "curious", "content"}                            # 들뜬 감정


@dataclass
class Style:
    ellipsis: bool = False
    ellipsis_rate: float = 0.0
    exclaim: bool = False
    exclaim_rate: float = 0.0
    tilde: bool = False
    laugh: str = "normal"
    hesitate: list[str] = field(default_factory=list)
    hesitate_rate: float = 0.0

    @property
    def plain(self) -> bool:
        return not (self.ellipsis or self.exclaim or self.tilde or self.hesitate or self.laugh == "none")


def from_data(raw: Any) -> Style:
    d = raw if isinstance(raw, dict) else {}

    def rate(k: str) -> float:
        try:
            return max(0.0, min(1.0, float(d.get(k, 0.0))))
        except (TypeError, ValueError):
            return 0.0
    return Style(ellipsis=bool(d.get("ellipsis")), ellipsis_rate=rate("ellipsis_rate"), exclaim=bool(d.get("exclaim")),
                 exclaim_rate=rate("exclaim_rate"), tilde=bool(d.get("tilde")),
                 laugh="none" if str(d.get("laugh", "normal")) == "none" else "normal",
                 hesitate=[str(x).strip() for x in (d.get("hesitate") or []) if str(x).strip()][:6], hesitate_rate=rate("hesitate_rate"))


def symbol_rule(s: Style) -> str:
    """공통 시스템 프롬프트의 기호 규칙 한 줄 (캐릭터 스타일에 맞게)."""
    banned = ["쉼표", "마침표", "따옴표", "괄호"] + ([] if s.tilde else ["물결"]) + ([] if s.exclaim else ["느낌표"])
    out = f"{'·'.join(banned)} 금지(물음표만 가끔)."
    if s.ellipsis:
        out += " 말을 흐릴 때는 말줄임표(...)를 써도 된다."
    if s.exclaim:
        out += " 신날 때 느낌표는 한 개까지만 쓴다."
    if s.tilde:
        out += " 물결(~)은 가끔만."
    out += " ㅇㅇ 정도만 쓰고 ㅋㅋ는 쓰지 않는다." if s.laugh == "none" else " ㅋㅋ ㅇㅇ 정도만."
    return out + " 유치한 드립·말장난 금지."


def strip_laugh(text: str) -> str:
    """ㅋ 연속(ㅋㅋ, ㅋㅋㅋㅋ)을 지운다 (laugh: none 인 캐릭터)."""
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"ㅋ{1,}", "", text)).strip()


_END_SKIP = re.compile(r"([?？!！~]|\.\.\.|ㅋ|ㅎ|ㅠ|ㅜ|:\w+:|>)\s*$")


def flavor(text: str, s: Style, emotion: str = "", is_first: bool = True, is_last: bool = True,
           rng: random.Random | None = None) -> str:
    """스타일에 맞게 머뭇거림(앞)과 말줄임표/느낌표(끝)를 확률적으로 입힌다. 이미 있으면 덧붙이지 않는다."""
    if not text or s.plain:
        return text
    r = rng or random
    out = text
    timid = emotion in TIMID
    if s.hesitate and is_first and r.random() < min(1.0, s.hesitate_rate * (1.6 if timid else 1.0)) \
            and not re.match(r"^(저기|아아|아|어|그|음|앗)(?![가-힣])", out):
        out = f"{r.choice(s.hesitate)} {out}"
    if is_last and not _END_SKIP.search(out):
        if s.ellipsis and r.random() < min(1.0, s.ellipsis_rate * (1.5 if timid else 1.0)):
            out = out.rstrip(" ,") + "..."
        elif s.exclaim and r.random() < min(1.0, s.exclaim_rate * (1.4 if emotion in EXCITED else 1.0)) and not out.endswith("..."):
            out = out.rstrip(" ,") + "!"
    return out
