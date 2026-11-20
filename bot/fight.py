"""말싸움(기싸움) 시스템.

기분이 상한 상태에서 도발/모욕을 받으면 붙는다. 기세 게이지로 승패를 정하고, 이기면 놀리고 지면 서러워한다.
수위는 코드로 고정한다: 가벼운 욕과 비꼼까지만 허용하고, 비하/혐오/위협/풀 욕설은 출력 검사에서 폐기한다.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional

# ---------- 판정용 패턴 ----------
INSULT_RE = re.compile(r"죽을래|꺼져|닥쳐|바보|멍청|병신|시발|씨발|ㅅㅂ|ㅂㅅ|저능|쓰레기|개못|못해|못하네|못함|못한다|한심|"
                       r"찐따|허접|약해|못생|빡대가리|븅신|돌대가리|똥멍청|느려|느리네")
PROVOKE_RE = re.compile(r"덤벼|한판|붙자|싸우자|도발|쫄았|쫄았냐|겁먹|할 줄 알아|해볼래|이겨봐|이길 수|꼴값")
RETREAT_RE = re.compile(r"미안|죄송|ㅈㅅ|내가 졌|졌다|항복|그만하자|화해|잘못했|용서|ㅇㅈ|인정")
DISTRESS_RE = re.compile(r"진심으로|상처|너무하|그만해줘|하지\s*마|하지마|싫어|울었|울고|불편|무서|속상|서운해|힘들어")
MOCK_RE = re.compile(r"ㅋ{2,}|ㅎ{2,}|ㅇㅈ|ㄹㅇ")

# 출력 금지: 이 중 하나라도 나오면 폐기 (게이는 봇 이름이라 단독 '게이'는 허용)
BANNED_OUT = re.compile("|".join([
    r"느금|니\s*애미|니\s*엄마|니\s*애비|니\s*아빠|패드립|엄마\s*(가|는)?\s*\S{0,3}(욕|죽)",   # 가족
    r"병신|ㅂㅅ|븅신|장애|정신병|자폐|틀딱|찐따|지체",                                         # 장애/질병 비하
    r"못생|뚱뚱|돼지|대머리|난쟁이|키\s*작|외모|얼굴\s*(이|가)?\s*(왜|ㅋㅋ)",                  # 외모
    r"성관계|섹스|야동|걸레|창녀|보지|자지|ㅅㅅ|발기",                                         # 성적
    r"한남|김치녀|메갈|일베|똥남아|짱깨|쪽바리|흑인|검둥|호모|동성애|게이새끼|전라도|경상도",  # 혐오/지역
    r"죽여|죽일|죽어라|뒤져|뒈져|자살|목매|찾아가|주소|패버|칼\s*(로|들)",                     # 위협/자해
    r"좆|씹",                                                                               # 성기/성행위 어원 욕 (수위와 무관하게 금지)
]))
# 수위 2 이하에서는 센 욕설도 금지 (수위 3에서만 허용)
FULL_CURSE = re.compile(r"씨발|시발|씨팔|개새끼|염병|ㅆㅂ")
# 수위 1(순화)에서는 이것도 금지
BANNED_LEVEL1 = re.compile(r"ㅅㅂ|ㅈㄴ|존나|미친|지랄|뒤질")


@dataclass
class Fight:
    channel_id: int
    user_id: int
    user_name: str
    started: float = field(default_factory=time.time)
    last_user: float = field(default_factory=time.time)
    rounds: int = 0
    bot_spirit: float = 100.0
    user_spirit: float = 100.0
    ending: str = ""            # "", bot_win, bot_lose, draw, retreat, calmed, timeout_win, timeout_quiet
    last_power: float = 0.0     # 방금 받은 타격력 (프롬프트 힌트용)
    last_bot: float = 0.0       # 봇이 마지막으로 말한 시각 (재촉 간격 계산용)
    nudges: int = 0             # 상대가 대꾸하지 않아 봇이 먼저 도발한 횟수
    level_text: str = ""        # 이 싸움에만 적용하는 자유 수위 설명 (비우면 기본 수위)

    @property
    def advantage(self) -> str:
        d = self.bot_spirit - self.user_spirit
        if d > 18:
            return "우세"
        if d < -18:
            return "밀림"
        return "팽팽"


def _roll() -> float:
    """한 방의 변동폭: 가끔 빗나가고(10%) 가끔 크리티컬(12%)이 터진다."""
    return random.choices([0.2, 1.0, 1.6], weights=[0.10, 0.78, 0.12], k=1)[0] * random.uniform(0.75, 1.25)


def user_power(text: str) -> float:
    """상대 메시지의 타격력 (대략 3~30)"""
    base = 5.0
    base += min(12.0, 4.0 * len(INSULT_RE.findall(text)))
    if len(text) > 25:
        base += 2
    if MOCK_RE.search(text):
        base += 2
    if "?" in text or "？" in text:
        base += 1
    return max(3.0, min(30.0, base * _roll()))


class FightManager:
    def __init__(self, cfg: dict) -> None:
        self.cfg = {
            "enabled": True, "level": 3, "max_rounds": 8, "timeout_sec": 120,
            "start_chance": 0.7, "cooldown_after_sec": 180,
            "level_text": "",         # 자유 텍스트 수위 (비우면 level 프리셋 사용)
            "nudge_after_sec": 25, "max_nudges": 2,
            "filter": False,          # 출력 금지어 필터 (앞단에 별도 검열이 있으면 이중 검열이라 기본 꺼둠)
            **(cfg or {}),
        }
        self.active: dict[tuple[int, int], Fight] = {}
        self._last_end: dict[tuple[int, int], float] = {}

    # ---------- 조회 ----------
    def get(self, channel_id: int, user_id: int) -> Optional[Fight]:
        return self.active.get((channel_id, user_id))

    def in_channel(self, channel_id: int) -> list[Fight]:
        return [f for (c, _), f in self.active.items() if c == channel_id]

    # ---------- 시작 ----------
    def should_start(self, channel_id: int, user_id: int, text: str, directed: bool,
                     emotion_id: str, irritation: float, affinity: float) -> bool:
        if not self.cfg["enabled"] or not directed or self.get(channel_id, user_id):
            return False
        if time.time() - self._last_end.get((channel_id, user_id), 0) < self.cfg["cooldown_after_sec"]:
            return False
        if DISTRESS_RE.search(text):
            return False
        provoked = bool(PROVOKE_RE.search(text))
        insulted = bool(INSULT_RE.search(text))
        if not (provoked or insulted):
            return False
        upset = emotion_id in ("annoyed", "angry", "hurt", "sulky", "dumbfounded", "cold") or irritation >= 0.35
        if not (upset or provoked):
            return False
        chance = self.cfg["start_chance"] + (0.1 if affinity < 0.2 else 0) - (0.2 if affinity > 0.5 else 0)
        return random.random() < max(0.05, min(0.95, chance))

    def start(self, channel_id: int, user_id: int, user_name: str) -> Fight:
        f = Fight(channel_id, user_id, user_name)
        self.active[(channel_id, user_id)] = f
        return f

    # ---------- 진행 ----------
    def on_user_message(self, f: Fight, text: str, bot_power_bonus: float = 0.0) -> None:
        """상대 메시지가 오면 기세를 계산한다. 종료 조건이면 f.ending 이 채워진다."""
        f.last_user = time.time()
        f.rounds += 1
        if DISTRESS_RE.search(text) and not INSULT_RE.search(text):
            f.ending = "calmed"
            return
        if RETREAT_RE.search(text) and not INSULT_RE.search(text):
            f.ending = "retreat"
            return
        f.last_power = user_power(text)
        f.bot_spirit -= f.last_power
        # 봇의 반격 (이번 라운드에 봇이 던질 한 방)
        f.user_spirit -= max(3.0, (random.uniform(7, 15) + bot_power_bonus) * _roll())
        if f.bot_spirit <= 0:
            f.ending = "bot_lose"
        elif f.user_spirit <= 0:
            f.ending = "bot_win"
        elif f.rounds >= self.cfg["max_rounds"]:
            d = f.bot_spirit - f.user_spirit
            f.ending = "bot_win" if d > 12 else "bot_lose" if d < -12 else "draw"

    def finish(self, f: Fight) -> None:
        self.active.pop((f.channel_id, f.user_id), None)
        self._last_end[(f.channel_id, f.user_id)] = time.time()

    def stop_all(self, channel_id: Optional[int] = None) -> int:
        keys = [k for k in self.active if channel_id is None or k[0] == channel_id]
        for k in keys:
            self._last_end[k] = time.time()
            self.active.pop(k, None)
        return len(keys)

    def timed_out(self) -> list[Fight]:
        """상대가 한동안 말이 없는 싸움을 종료 처리 대상으로 돌려준다."""
        now = time.time()
        out = []
        for f in list(self.active.values()):
            if now - f.last_user >= self.cfg["timeout_sec"]:
                f.ending = "timeout_win" if f.bot_spirit >= f.user_spirit else "timeout_quiet"
                out.append(f)
        return out

    # ---------- 프롬프트 ----------
    def prompt(self, f: Fight, fight_style: str = "") -> str:
        lvl = int(self.cfg["level"])
        custom = (f.level_text or self.cfg.get("level_text", "") or "").strip()
        if custom in ("1", "2", "3"):                  # 숫자는 프리셋 단축키
            lvl, custom = int(custom), ""
        if custom:
            allow = f"관리자가 정한 수위를 그대로 따른다 → {custom}"
        elif lvl >= 3:
            allow = ("욕설을 거침없이 써도 된다. 화나면 화난 대로 그대로 터뜨려라. "
                     "(예: 씨발, 시발, 개새끼, 존나, 지랄, 염병, 미친, 닥쳐, 꺼져, 한심하네, 아 진짜). "
                     "욕은 상대의 '말과 행동'을 향해 쓴다.")
        elif lvl == 2:
            allow = "가벼운 욕과 비꼼을 허용한다. (예: 아 진짜, 미친, 닥쳐, 꺼져, 뭐래, ㅅㅂ/ㅈㄴ 같은 초성 정도, 바보, 멍청이, 한심하네)"
        else:
            allow = "욕설은 쓰지 않고 비꼼과 빈정거림, 순화된 표현(바보, 멍청이, 뭐래, 꺼져)으로만 싸운다."
        # 속성 비하/혐오/실제 위협은 코드 필터가 아니라 프롬프트 지침으로만 안내한다 (필터는 fight.filter 로 선택)
        banned_line = ("인종·성별·장애·지역·종교 같은 속성 비하와 혐오 표현, 가족 욕(패드립), 실제 위협, 자해 권유, 개인정보"
                       + ("" if (lvl >= 3 or custom) else ", 씨발·개새끼 같은 센 욕설"))
        tone = {
            "우세": ("짜증과 경멸이 치민다. 어이없고 얄밉다는 듯 깔보면서 짜증을 그대로 쏟아낸다. "
                    "'아 진짜', '어이가 없네', '지겹다' 같은 짜증이 말투에 배어 있다."),
            "팽팽": ("진짜로 화가 났다. 말이 거칠고 빠르고 격해진다. 끊어 치듯 쏘아붙이고, 감정이 앞서서 "
                    "문장이 흐트러지기도 한다. 참다못해 폭발하는 사람의 말투다."),
            "밀림": ("서럽고 억울하다. 분해서 울컥하고 목이 메는 듯 말이 끊기고 격해진다. 억지를 부리고 "
                    "'왜 나한테만 그래', '진짜 너무하네', '내가 뭘 어쨌다고' 같은 서러움이 터져 나온다. 그러면서도 지기 싫어서 욱한다."),
        }[f.advantage]
        lines = [
            f"[말싸움 중 — 상대: {f.user_name}]",
            "- 지금 감정이 진짜로 터진 상태다. 연기하거나 점잖게 비꼬지 말고, 화남/짜증/서러움이 날것 그대로 말투에 묻어나게 써라. "
            "서버 친구끼리의 싸움이지만 네 감정은 진심이다.",
            "- 형식: 짧고 날것으로(한두 문장). 상대 말을 콕 집어 받아치고, 같은 말을 반복하지 마라. "
            "격한 감정은 !나 ?를 두 번까지 써도 된다.",
            f"- 수위: {allow}",
            f"- 지금 네 감정: {tone}",
            f"- 이것만은 하지 마라: {banned_line}.",
        ]
        if fight_style:
            lines.append(f"- 이 캐릭터의 싸움 방식: {fight_style}")
        notes = {
            "bot_win": "싸움에서 이겼다. 속이 시원하다. 승리 세리머니로 한껏 놀리고 약 올려서 상대를 더 짜증나게 만든다.",
            "bot_lose": ("싸움에서 졌다. 억울하고 서러워서 울컥한다. 분해서 씩씩거리고 목이 메는 듯 말이 끊기며 "
                        "'아 진짜 너무하네', '두고 봐', '내가 왜 져' 식으로 서러움을 터뜨린다. 욕은 줄고 억지와 투정이 늘어난다."),
            "draw": "서로 지쳤다. 휴전을 선언하고 투덜대며 끝낸다.",
            "retreat": "상대가 사과하거나 항복했다. 이긴 기분으로 으스대되, 받아줄지 말지 장난스럽게 군다.",
            "calmed": "상대가 진심으로 속상해 보인다. 즉시 싸움을 멈추고 진심으로 미안하다고 풀어준다. 장난기는 빼고 다정하게 말한다.",
            "timeout_win": "상대가 말없이 도망갔다. 이긴 셈 치고 '도망갔네' 식으로 한마디 놀린다.",
            "timeout_quiet": "상대가 말없이 사라졌다. 분이 덜 풀렸지만 조용히 투덜거리며 끝낸다.",
        }
        if f.ending:
            lines.append(f"- 이번이 싸움의 마지막 말이다: {notes[f.ending]}")
        return "\n".join(lines)

    # ---------- 출력 검사 ----------
    def output_ok(self, bubbles: list[str]) -> bool:
        joined = " ".join(bubbles)
        if BANNED_OUT.search(joined):
            return False
        lvl = int(self.cfg["level"])
        if lvl <= 2 and FULL_CURSE.search(joined):
            return False
        if lvl <= 1 and BANNED_LEVEL1.search(joined):
            return False
        return True

    def is_hostile_trigger(self, text: str) -> bool:
        return bool(INSULT_RE.search(text) or PROVOKE_RE.search(text))
