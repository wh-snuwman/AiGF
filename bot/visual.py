"""캐릭터 이미지 프롬프트 조합기 (이미지 생성 모델용 태그 문자열). LLM 이나 GPU 를 쓰지 않는다.

구성: 공통 품질/스타일(config/visual_common.yaml) + 캐릭터 외형 + 분위기 + 감정 표정(30종) + 장소/시간대 + 의상 + 장면.
외형은 캐릭터 yaml 의 `visual:` 한 곳에만 적고 나머지는 공통값을 재사용한다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import load_yaml


def _tag_join(*parts: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        for t in str(part or "").split(","):
            t = t.strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return ", ".join(out)


def time_slot(hour: int) -> str:
    return "morning" if 5 <= hour < 11 else "day" if 11 <= hour < 17 else "evening" if 17 <= hour < 21 else "night"


def build(char: dict[str, Any], emotion: str = "calm", place: str | None = None, scene: str | None = None,
          outfit: str | None = None, hour: int | None = None, common: dict | None = None) -> tuple[str, str]:
    """(positive, negative) 프롬프트. 캐릭터 yaml 에 visual 이 없으면 profile 한 줄을 외형 대신 쓴다."""
    c = common if common is not None else load_yaml("visual_common.yaml")
    v = char.get("visual") or {}
    appearance = v.get("appearance") or str(char.get("profile", "")).replace("\n", " ")[:120]
    exp = (c.get("expressions") or {}).get(emotion, "")
    places = {**(c.get("places") or {}), **(v.get("places") or {})}        # 캐릭터 전용 장소는 그 캐릭터만
    outfits = v.get("outfits") or {}
    dress = outfits.get(outfit or "default") or (outfit if outfit and outfit not in outfits else "") or outfits.get("default", "")
    scenes = v.get("scenes") or {}
    act = scenes.get(scene or "", scene or "")
    bg = places.get(place or "", "")
    tm = (c.get("time_tags") or {}).get(time_slot(hour)) if hour is not None else ""
    pos = _tag_join(c.get("quality", ""), appearance, dress, v.get("vibe", ""), exp, act, bg, tm)
    return pos, c.get("negative", "")
