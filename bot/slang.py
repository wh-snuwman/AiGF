"""유행어 사전(config/slang_dict.txt)에서 지금 상황에 어울리는 말 몇 개를 골라 '가끔, 하나 정도' 쓰라고 힌트를 준다.

- 인터넷 검색과 무관하게 동작한다 (파일만 읽음, 지연 없음).
- 조금씩: 확률(chance)로만 힌트를 넣고, 봇이 최근 말에 사전 단어를 이미 썼으면 쿨다운 동안 힌트를 넣지 않는다.
- 상황 맞추기: 상대 말의 단어와 봇의 현재 감정으로 태그를 고르고, 그 태그의 말 위주로 후보를 뽑는다.
- 같은 말이 계속 나오지 않게 최근 힌트로 준 말은 한동안 빼고 고른다.
"""
from __future__ import annotations

import logging
import random
import re
from collections import deque
from pathlib import Path

log = logging.getLogger("slang")
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# 상대 말 → 태그
TEXT_TAGS: list[tuple[str, re.Pattern]] = [
    ("웃음", re.compile(r"ㅋㅋ|ㅎㅎ|웃기|웃겨|개웃|빵터")),
    ("놀람", re.compile(r"헐|대박|미쳤|실화|진짜\?|레알|헉|와\s|ㄷㄷ")),
    ("칭찬", re.compile(r"잘했|최고|멋있|멋지|예쁘|이쁘|귀엽|귀여|천재|잘한다|대단")),
    ("동의", re.compile(r"ㅇㅇ|맞아|맞지|인정|ㄹㅇ|그치|그니까")),
    ("슬픔", re.compile(r"ㅠ|ㅜ|슬퍼|우울|망했|속상|서운|외로|눈물")),
    ("위로", re.compile(r"힘들|지쳤|망했|떨어졌|탈락|ㅠ|우울")),
    ("짜증", re.compile(r"짜증|열받|빡치|빡쳐|화나|싫어|어이없")),
    ("피곤", re.compile(r"피곤|졸려|졸리|잠와|지쳤|힘들|밤샘|새벽")),
    ("귀찮음", re.compile(r"귀찮|심심|할\s*거\s*없")),
    ("음식", re.compile(r"배고|먹|밥|야식|치킨|피자|라면|메뉴|맛있|커피|카페")),
    ("공부일", re.compile(r"시험|공부|과제|회사|출근|퇴근|알바|야근|학교|수업|월요일")),
    ("게임", re.compile(r"게임|겜|롤|발로|랭크|티어|가챠|뽑기|스팀|레이드|보스")),
    ("돈", re.compile(r"돈|월급|샀|지름|비싸|싸다|통장|주식|코인|용돈")),
    ("연애", re.compile(r"연애|썸|고백|좋아해|커플|솔로|애인|여친|남친|데이트")),
    ("인사", re.compile(r"안녕|하이|ㅎㅇ|잘자|굿모닝|좋은\s*아침|왔다|ㅂㅂ")),
    ("자랑", re.compile(r"자랑|했다|성공|합격|붙었|해냈|샀다")),
    ("질문", re.compile(r"\?$|뭐해|뭐함|어디|추천")),
]
# 봇 감정 → 태그
EMO_TAGS: dict[str, tuple[str, ...]] = {
    "joy": ("웃음", "칭찬", "텐션"), "excited": ("텐션", "놀람", "웃음"), "playful": ("놀림", "웃음"),
    "content": ("동의", "일상"), "relaxed": ("일상",), "calm": ("일상", "동의"),
    "bored": ("귀찮음",), "lazy": ("귀찮음", "피곤"), "listless": ("피곤", "귀찮음"), "sleepy": ("피곤",),
    "tired": ("피곤",), "annoyed": ("짜증",), "angry": ("짜증",), "sulky": ("짜증", "슬픔"),
    "hurt": ("슬픔",), "sad": ("슬픔",), "gloomy": ("슬픔", "피곤"), "lonely": ("슬픔",),
    "smug": ("자랑", "놀림"), "proud": ("자랑",), "caring": ("위로",), "worried": ("위로",),
    "dumbfounded": ("놀람",), "flustered": ("놀람",), "curious": ("질문", "놀람"),
}
# 이런 감정일 때는 유행어를 거의 쓰지 않는다 (진지/가라앉은 분위기)
QUIET_EMOS = {"sad", "hurt", "gloomy", "cold", "focused", "anxious"}


class Slang:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = {"enabled": True, "file": "slang_dict.txt", "chance": 0.22, "per_hint": 3,
                    "cooldown_lines": 6, "recent_hint_size": 40, **(cfg or {})}
        self.entries: list[tuple[str, str, frozenset[str]]] = []
        self._mtime = 0.0
        self._recent: deque[str] = deque(maxlen=int(self.cfg["recent_hint_size"]))

    # ---------- 파일 ----------
    def _path(self) -> Path | None:
        name = str(self.cfg.get("file") or "")
        if not name:
            return None
        p = (CONFIG_DIR / name).resolve()
        return p if CONFIG_DIR.resolve() in p.parents else None      # config 폴더 밖은 읽지 않는다

    def load(self) -> None:
        """파일이 바뀌었으면 다시 읽는다 (재시작 불필요)."""
        p = self._path()
        try:
            m = p.stat().st_mtime if p else 0.0
        except OSError:
            m = 0.0
        if not p or m == self._mtime:
            return
        self._mtime = m
        out = []
        try:
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#") or "|" not in ln:
                    continue
                parts = [x.strip() for x in ln.split("|")]
                word, mean = parts[0], parts[1] if len(parts) > 1 else ""
                tags = frozenset(t.strip() for t in (parts[2] if len(parts) > 2 else "").split(",") if t.strip())
                if word:
                    out.append((word, mean, tags))
        except OSError as e:
            log.warning("유행어 사전을 읽지 못했어요: %s", e)
            return
        self.entries = out
        log.info("유행어 사전 %d개 읽음", len(out))

    # ---------- 고르기 ----------
    @staticmethod
    def _core(word: str) -> str:
        """출력에서 찾을 핵심 글자 (예: '~ 장인' → '장인', '몰?루' → '몰?루')."""
        return word.replace("~", "").strip()

    def used_recently(self, own_recent: list[str]) -> bool:
        """봇이 최근 몇 줄에, 최근 힌트로 받은 유행어를 이미 썼는가 (ㅇㅇ ㅋㅋ 같은 평소 말투는 세지 않게 힌트 받은 말만 본다)."""
        lines = own_recent[-int(self.cfg["cooldown_lines"]):]
        if not lines or not self._recent:
            return False
        text = "\n".join(lines)
        return any(len(c) >= 2 and c in text for c in (self._core(w) for w in self._recent))

    def tags_for(self, text: str, emotion_id: str = "") -> list[str]:
        tags = [t for t, rx in TEXT_TAGS if rx.search(text or "")]
        tags += [t for t in EMO_TAGS.get(emotion_id or "", ()) if t not in tags]
        return tags

    def pick(self, text: str, emotion_id: str = "", n: int | None = None, rng: random.Random | None = None) -> list[tuple[str, str]]:
        r = rng or random
        self.load()
        n = n or int(self.cfg["per_hint"])
        tags = self.tags_for(text, emotion_id)
        pool = [e for e in self.entries if e[0] not in self._recent]
        if not pool:
            self._recent.clear()
            pool = list(self.entries)
        fit = [e for e in pool if e[2] & set(tags)]
        r.shuffle(fit)
        out = fit[: max(1, n - 1)] if fit else []
        rest = [e for e in pool if e not in out]
        r.shuffle(rest)
        out += rest[: n - len(out)]                    # 하나쯤은 태그와 무관한 말도 섞어 매번 같은 결이 안 되게
        for w, _m, _t in out:
            self._recent.append(w)
        return [(w, m) for w, m, _t in out]

    def hint(self, text: str = "", emotion_id: str = "", own_recent: list[str] | None = None,
             free: bool = False, rng: random.Random | None = None) -> str:
        """프롬프트에 넣을 한 줄. 대부분은 빈 문자열 (가끔만, 조금씩)."""
        if not self.cfg.get("enabled", True):
            return ""
        r = rng or random
        chance = float(self.cfg["chance"]) * (1.2 if free else 1.0)
        if emotion_id in QUIET_EMOS:
            chance *= 0.25
        if r.random() >= chance:
            return ""
        self.load()
        if not self.entries or self.used_recently(own_recent or []):
            return ""                                   # 방금 유행어를 썼으면 한동안 쉰다 (남발 방지)
        picks = self.pick(text, emotion_id, rng=r)
        if not picks:
            return ""
        items = " ; ".join(f"{w}({m})" for w, m in picks)
        return ("유행어 참고(어울릴 때만 이 중 하나를 자연스럽게, 억지로 끼우지 말고 안 써도 됨·뜻 설명 금지·같은 말 반복 금지): "
                + items)
