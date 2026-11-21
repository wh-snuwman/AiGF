"""이모지: 서버 커스텀 이모지 사전, `:이름:` 변환, 리액션 선택."""
from __future__ import annotations

import random
import re
from typing import Any, Optional

# 감정 → 리액션 후보 (유니코드)
EMOTION_REACT: dict[str, list[str]] = {
    "joy": ["😆", "😂"], "excited": ["🔥", "😆"], "playful": ["😏", "😂"], "smug": ["😎", "😏"],
    "proud": ["😎"], "content": ["🙂", "👍"], "calm": ["👍", "🙂"], "relaxed": ["😌"],
    "sleepy": ["😴"], "tired": ["😮‍💨", "😪"], "bored": ["😑", "🥱"], "lazy": ["😑"],
    "annoyed": ["😒", "🙄"], "angry": ["😠", "💢"], "sulky": ["😤"], "hurt": ["🥺"], "sad": ["😢"],
    "gloomy": ["😞"], "lonely": ["🥺"], "worried": ["😟"], "anxious": ["😰"], "caring": ["🫶", "💕"],
    "curious": ["👀"], "flustered": ["😳"], "shy": ["😳"], "flutter": ["💓"], "dumbfounded": ["😑", "🤨"],
    "focused": ["🤔"], "cold": ["😐"], "listless": ["😶"],
}
# 감정 → 커스텀 이모지 이름에서 찾을 키워드
EMOTION_KEYWORDS: dict[str, list[str]] = {
    "laugh": ["lol", "laugh", "lmao", "kek", "haha", "ㅋ", "웃", "joy", "funny"],
    "sad": ["sad", "cry", "ㅠ", "울", "tear"],
    "love": ["love", "heart", "하트", "사랑"],
    "angry": ["angry", "mad", "화", "rage"],
    "think": ["think", "hmm", "생각", "흠"],
    "ok": ["ok", "good", "굿", "thumb", "nice", "ㅇㅋ"],
    "shock": ["shock", "omg", "헉", "놀"],
}
EMO_TO_CAT = {
    "joy": "laugh", "excited": "laugh", "playful": "laugh", "smug": "laugh", "content": "ok", "calm": "ok",
    "sad": "sad", "hurt": "sad", "gloomy": "sad", "lonely": "sad", "angry": "angry", "annoyed": "angry",
    "sulky": "angry", "caring": "love", "flutter": "love", "curious": "think", "focused": "think",
    "worried": "shock", "anxious": "shock", "flustered": "shock", "dumbfounded": "shock",
}
SHORTCODES = {
    ":smile:": "😄", ":joy:": "😂", ":sob:": "😭", ":heart:": "❤️", ":thinking:": "🤔", ":sleeping:": "😴",
    ":rage:": "😡", ":eyes:": "👀", ":fire:": "🔥", ":pleading_face:": "🥺", ":flushed:": "😳", ":thumbsup:": "👍",
}
LAUGH_ONLY = re.compile(r"^[ㅋㅎ\s!?.~ㅠㅜ]{1,12}$")
TOKEN = re.compile(r":([A-Za-z0-9_가-힣]{2,32}):")


class Emojis:
    def __init__(self) -> None:
        self.custom: dict[str, tuple[str, int, bool]] = {}     # 이름 -> (이름, id, animated)

    def refresh(self, guilds: Any) -> None:
        self.custom.clear()
        for g in guilds:
            for e in getattr(g, "emojis", []):
                if getattr(e, "available", True):
                    self.custom.setdefault(e.name, (e.name, e.id, bool(e.animated)))

    # ---------- 프롬프트 ----------
    def prompt_block(self, limit: int = 40) -> str:
        if not self.custom:
            return ""
        names = " ".join(f":{n}:" for n in list(self.custom)[:limit])
        return f"서버이모지(:이름: 형식, 아주 드물게만·대부분의 답엔 쓰지 않음): {names}"

    # ---------- 출력 변환 ----------
    def render(self, text: str) -> str:
        """`:이름:` 을 실제 커스텀 이모지로, 흔한 숏코드는 유니코드로 바꾸고, 모르는 토큰은 지운다."""
        for k, v in SHORTCODES.items():
            text = text.replace(k, v)

        def sub(m: re.Match) -> str:
            name = m.group(1)
            if name in self.custom:
                n, i, anim = self.custom[name]
                return f"<{'a' if anim else ''}:{n}:{i}>"
            return ""        # 존재하지 않는 이모지 토큰은 지운다 (깨진 글자 방지)

        out = TOKEN.sub(sub, text)
        return re.sub(r"[ \t]{2,}", " ", out).strip() or text

    def text_token(self, cat: str) -> Optional[str]:
        """글에 섞을 서버 커스텀 이모지 `:이름:` (감정 카테고리에 맞는 이름 키워드로 고른다). 없으면 None."""
        keys = EMOTION_KEYWORDS.get(cat, [])
        hits = [n for n in self.custom if any(k in n.lower() for k in keys)]
        return f":{random.choice(hits)}:" if hits else None

    # ---------- 리액션 선택 ----------
    def is_trivial(self, text: str) -> bool:
        """ㅋㅋ/ㅎㅎ/ㅠㅠ 같은 짧은 리액션뿐인 말 → LLM 없이 이모지만 달아도 자연스럽다."""
        body = re.sub(r"^\(.*?에 답장\)\s*", "", text).strip()
        return bool(LAUGH_ONLY.match(body))

    def pick_reaction(self, text: str, emotion_id: str, has_image: bool = False) -> Optional[Any]:
        """리액션으로 달 이모지 하나. 커스텀이면 (이름, id, animated) 튜플, 유니코드면 문자열."""
        body = re.sub(r"^\(.*?에 답장\)\s*", "", text)
        if has_image:
            return random.choice(["👀", "😮", "🤔"])
        cat = EMO_TO_CAT.get(emotion_id)
        if re.search(r"ㅋ{2,}|ㅎ{2,}", body):
            cat, pool = "laugh", ["😂", "🤣", "😆"]
        elif re.search(r"ㅠ|ㅜ|슬퍼|힘들", body):
            cat, pool = "sad", ["🥲", "🫂", "😢"]
        elif "?" in body or "？" in body:
            cat, pool = "think", ["🤔", "👀"]
        else:
            pool = EMOTION_REACT.get(emotion_id) or ["👍"]
        if cat and self.custom and random.random() < 0.4:
            keys = EMOTION_KEYWORDS.get(cat, [])
            hits = [v for n, v in self.custom.items() if any(k in n.lower() for k in keys)]
            if hits:
                return random.choice(hits)
        return random.choice(pool)

    @staticmethod
    def classify_reaction(emoji: str) -> int:
        """상대가 단 리액션의 호감도: +1 긍정 / -1 부정 / 0 중립"""
        pos = "❤️😍🥰👍😂🤣😆😊🔥👏🎉💕🫶😎✨💯"
        neg = "😡😠👎💩🤡🙄😒🤮💢🖕"
        if any(c in emoji for c in pos):
            return 1
        if any(c in emoji for c in neg):
            return -1
        return 0
