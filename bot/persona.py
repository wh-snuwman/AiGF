"""config/characters/*.yaml 을 읽어 시스템 프롬프트와 few-shot 예시를 만든다."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import yaml

from .config import CONFIG_DIR, load_yaml

CHAR_DIR = CONFIG_DIR / "characters"
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def time_of_day(hour: int) -> str:
    if hour < 5:
        return "새벽"
    if hour < 11:
        return "아침"
    if hour < 14:
        return "점심 무렵"
    if hour < 18:
        return "오후"
    if hour < 22:
        return "저녁"
    return "밤"


def _block(value: Any) -> str:
    return str(value or "").strip()


class Persona:
    def __init__(self, char_id: str) -> None:
        self.char_id = ""
        self.data: dict[str, Any] = {}
        self.common: dict[str, Any] = {}
        self.load(char_id)

    # ---------- 캐릭터 목록/로드 ----------
    @staticmethod
    def available() -> list[tuple[str, str]]:
        """[(id, 표시 이름), ...]"""
        out: list[tuple[str, str]] = []
        for p in sorted(CHAR_DIR.glob("*.yaml")):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    name = (yaml.safe_load(f) or {}).get("name", p.stem)
            except Exception:  # noqa: BLE001
                continue
            out.append((p.stem, str(name)))
        return out

    def load(self, char_id: str) -> None:
        ids = [i for i, _ in self.available()]
        if not ids:
            raise SystemExit(f"{CHAR_DIR} 에 캐릭터 yaml 파일이 없습니다.")
        if char_id not in ids:
            char_id = ids[0]
        self.data = load_yaml(f"characters/{char_id}.yaml")
        self.common = load_yaml("common.yaml")
        self.char_id = char_id

    def reload(self) -> None:
        self.load(self.char_id)

    # ---------- 속성 ----------
    @property
    def name(self) -> str:
        return self.data.get("name", "캐릭터")

    @property
    def nickname(self) -> str:
        return self.data.get("nickname", self.name)

    @property
    def greeting(self) -> str:
        return self.data.get("greeting", "")

    # ---------- 프롬프트 ----------
    def system_prompt(self, target_name: str, facts: list[str], now: datetime) -> str:
        d = self.data
        name = self.name
        rules = _block(self.common.get("rules")).replace("{name}", name)
        extra = _block(d.get("rules")).replace("{name}", name)
        if extra:
            rules += "\n" + extra
        facts_block = "\n".join(f"- {f}" for f in facts) if facts else "(아직 아는 게 없음)"
        now_str = (
            f"{now.month}월 {now.day}일 {WEEKDAYS[now.weekday()]}요일 "
            f"{time_of_day(now.hour)} {now.hour}시 {now.minute:02d}분"
        )

        return f"""너는 "{name}"(이)라는 캐릭터가 되어, 디스코드 서버에서 여러 사람과 함께 채팅하는 서버 멤버 중 한 명이다.
이것은 롤플레이이며, 너는 끝까지 {name} 본인으로서만 말한다.

[캐릭터 프로필]
이름: {name} (애칭: {self.nickname})
{_block(d.get('profile'))}
좋아하는 것: {d.get('likes', '')}
싫어하는 것: {d.get('dislikes', '')}

[성격]
{_block(d.get('personality'))}

[말투]
{_block(d.get('speech_style'))}

[호칭]
{_block(d.get('address_style'))}

[배경]
{_block(d.get('backstory'))}

[대화 환경]
- 여러 명이 섞여서 대화하며, 대화 기록은 "닉네임: 내용" 형식이다. 너의 답변에는 닉네임 접두어를 붙이지 말고 대사만 쓴다.
- 지금은 "{target_name}"의 말에 대답할 차례다. 필요하면 이름을 부르고, 다른 사람들이 하던 이야기의 흐름도 자연스럽게 반영한다.
- 사람처럼 대화의 흐름에 자연스럽게 끼어들어서 말한다.

[{target_name}에 대해 기억하고 있는 것]
{facts_block}

[현재 시각]
{now_str}

[규칙]
{rules}"""

    def example_messages(self) -> list[dict[str, str]]:
        who = self.data.get("example_user", "유저")
        msgs: list[dict[str, str]] = []
        for ex in self.data.get("examples", []) or []:
            msgs.append({"role": "user", "content": f"{who}: {str(ex['user']).strip()}"})
            msgs.append({"role": "assistant", "content": str(ex["assistant"]).strip()})
        return msgs
