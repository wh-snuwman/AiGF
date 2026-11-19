"""persona.yaml 을 읽어 시스템 프롬프트와 few-shot 예시를 만든다."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import load_yaml

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


class Persona:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        self.data = load_yaml("persona.yaml")

    @property
    def name(self) -> str:
        return self.data.get("name", "캐릭터")

    @property
    def user_title(self) -> str:
        return self.data.get("user_title", "너")

    @property
    def greeting(self) -> str:
        return self.data.get("greeting", "안녕!")

    def system_prompt(self, user_name: str, facts: list[str], now: datetime) -> str:
        d = self.data
        name = self.name
        rules = str(d.get("rules", "")).replace("{name}", name)
        facts_block = "\n".join(f"- {f}" for f in facts) if facts else "(아직 알고 있는 정보가 없음)"
        now_str = f"{now.month}월 {now.day}일 {WEEKDAYS[now.weekday()]}요일 {time_of_day(now.hour)} {now.hour}시 {now.minute:02d}분"

        return f"""너는 "{name}"(이)라는 캐릭터가 되어 디스코드에서 {self.user_title}과 1:1로 채팅하고 있다.
이것은 롤플레이이며, 너는 끝까지 {name} 본인으로서만 말한다.

[캐릭터 프로필]
이름: {name} (애칭: {d.get('nickname', name)})
나이/소속: {d.get('age', '')} / {d.get('school', '')} / {d.get('club', '')}
외형: {d.get('appearance', '')}
좋아하는 것: {d.get('likes', '')}
싫어하는 것: {d.get('dislikes', '')}

[성격]
{str(d.get('personality', '')).strip()}

[말투]
{str(d.get('speech_style', '')).strip()}

[배경]
{str(d.get('backstory', '')).strip()}

[대화 상대]
상대는 {self.user_title}이며 디스코드 닉네임은 "{user_name}"이다. 호칭은 기본적으로 "{self.user_title}"을 쓴다.
네가 기억하고 있는 상대에 대한 정보:
{facts_block}

[현재 시각]
{now_str}

[규칙]
{rules.strip()}"""

    def example_messages(self) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = []
        for ex in self.data.get("examples", []) or []:
            msgs.append({"role": "user", "content": str(ex["user"]).strip()})
            msgs.append({"role": "assistant", "content": str(ex["assistant"]).strip()})
        return msgs
