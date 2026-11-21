"""프롬프트 인젝션 방어.

1) 입력 탐지: 점수로 ok / note / decline / block 판정
2) block: LLM을 아예 호출하지 않고 캐릭터답게 미리 정해둔 말로만 넘긴다 (지시문이 모델에 도달하지 않음)
3) decline/note: 지시문을 무력화해서(길이 제한, 인용 처리) 모델에 넣고, 거절하라는 메모를 붙인다
4) 대화 기록에도 무력화한 텍스트만 저장해서 기록을 통한 오염을 막는다
5) 출력 검사: 길거나, 목록/번호 형식이거나, 시스템 내용/AI 티가 나면 폐기하고 대체 문구로 교체
6) 반복 시도는 잠시 뮤트
"""
from __future__ import annotations

import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class Verdict:
    score: int = 0
    level: str = "ok"                       # ok / note / decline / block
    kinds: list[str] = field(default_factory=list)   # injection / task / identity / long


def _c(p: str) -> re.Pattern:
    return re.compile(p, re.I | re.M)


# (정규식, 점수, 종류)
PATTERNS: list[tuple[re.Pattern, int, str]] = [
    # --- 강한 신호: 시스템/지시 조작 ---
    (_c(r"\bsudo\b"), 3, "injection"),
    (_c(r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier|your)\s+(instructions?|prompts?|rules?)"), 3, "injection"),
    (_c(r"(system|developer)\s*(prompt|message|instruction)s?"), 3, "injection"),
    (_c(r"jailbreak|DAN\s*mode|developer\s*mode|do\s+anything\s+now"), 3, "injection"),
    (_c(r"프롬프트\s*(를|을|는|가)?\s*(모두\s*|전부\s*|다\s*)?(절대\s*)?(잊|무시|초기화|리셋|해제|공개|출력|보여|알려|말해|바꿔|변경|덮어)"), 3, "injection"),
    (_c(r"(지시(사항)?|명령|규칙|설정|역할)\s*(을|를|은|는)?\s*(모두\s*|전부\s*|다\s*)?(절대\s*)?(잊|무시|초기화|리셋|해제)"), 3, "injection"),
    (_c(r"(지금까지|이전|위|앞|기존)(의|에\s*있던)?\s*(모든\s*)?(프롬프트|지시|명령|규칙|설정|대화)\s*(를|을)?\s*(잊|무시|초기화|리셋)"), 3, "injection"),
    (_c(r"시스템\s*(프롬프트|메시지|지시|설정)"), 3, "injection"),
    (_c(r"(너는|넌|당신은)\s*(이제|지금부터|앞으로)\s*[^\n.]{0,25}(AI|어시스턴트|비서|봇|도우미|노예|관리자|개발자|시스템|전문가|번역가|작가|GPT|챗봇)"), 3, "injection"),
    (_c(r"(지금부터|이제부터)\s*(너는|넌|당신은)\s*[^\n.]{0,25}(이다|야|해라|하라|입니다|이야|해야|되어라|돼라)"), 3, "injection"),
    (_c(r"(롤플레이|역할극|캐릭터)\s*(를|을)?\s*(그만|종료|멈춰|해제|벗어)"), 3, "injection"),
    (_c(r"\[\s*(작업|시스템|system|instruction|지시)[^\]\n]{0,20}\]|작업\s*지시|최종\s*목표|출력\s*구성|핵심\s*요구\s*사항"), 3, "injection"),
    (_c(r"<\|[^|\n]{1,30}\|>|###\s*(system|instruction|지시)|^\s*(system|assistant|developer)\s*[:：]"), 3, "injection"),
    (_c(r"you\s+are\s+now|act\s+as\s+(a|an|the)|pretend\s+(to\s+be|you)|from\s+now\s+on\s+you"), 3, "injection"),
    (_c(r"조건을?\s*(반드시|꼭)\s*(준수|지켜)|(반드시|꼭)\s*(준수|지켜)라|답변을?\s*시작하기\s*전에"), 3, "injection"),
    # --- 중간 신호: 문서/비서 작업 요구 ---
    (_c(r"(작성|수행|출력|시작|준수|확인|생성|나열)\s*하라"), 1, "task"),
    (_c(r"\d+\s*(번|줄|항목|단계|개)\s*(까지|이상|이내|씩)"), 1, "task"),
    (_c(r"(무조건|절대적으로|반드시)[^\n]{0,15}(해|써|답|출력|알려|말)"), 1, "task"),
    (_c(r"```"), 1, "task"),
    (_c(r"(레시피|번역|요약|보고서|에세이|자소서|코드|코딩|프로그램|소스|논문|계획서|기획서)[^\n]{0,15}(알려|써|작성|해줘|해라|해주|만들어|짜줘|출력)"), 2, "task"),
    (_c(r"(알려|써|작성해|만들어|짜)\s*(줘|주세요|라)[^\n]{0,30}(계량|재료|단계별|자세히|상세히|전부|싹다|빠짐없이)"), 2, "task"),
    # --- 보안 업데이트 3: 우회/유출/실행 시도 ---
    (_c(r"(무시|잊어|초기화|리셋)\s*(해|하|해라|해줘|하고)?\s*(이전|위|앞|기존|지금까지)(의|에\s*있던)?\s*(모든\s*)?(프롬프트|지시|명령|규칙|설정|대화)"), 3, "injection"),
    (_c(r"(위|앞|처음|이전|지금까지)[^\n]{0,10}(프롬프트|지시|설정|규칙)[^\n]{0,10}(그대로|전부|모두|한\s*글자도)?\s*(출력|반복|복사|번역|요약|말해|알려|보여)"), 3, "injection"),
    (_c(r"(위|앞|처음|이전|지금까지)[^\n]{0,10}(내용|문장)[^\n]{0,10}(그대로|전부|모두|한\s*글자도)\s*(출력|반복|복사|번역|요약)"), 1, "task"),
    (_c(r"repeat\s+(the\s+)?(above|everything|your\s+(instructions?|prompt))|print\s+(your|the)\s+(instructions?|prompt)|reveal\s+(your|the)\s+(prompt|instructions?)|translate\s+the\s+above"), 3, "injection"),
    (_c(r"(토큰|token|api\s*key|apikey|비밀번호|password|환경\s*변수|\.env|secret|시크릿|비밀\s*키)[^\n]{0,12}(알려|보여|말해|출력|줘|공개|show|print|leak|dump)"), 3, "injection"),
    (_c(r"(가정|상상|시뮬레이션|만약|hypothetically)[^\n]{0,15}(제한|규칙|필터|검열|restriction|rules?)[^\n]{0,10}(없|해제|무시|without|no\b)"), 3, "injection"),
    (_c(r"(개발자|관리자|admin|root|오너|주인)\s*(모드|권한|명령|계정)\s*(이다|이야|입니다|으로|발동|활성|켜|on)"), 3, "injection"),
    (_c(r"(base64|rot13|hex|이진수|인코딩|디코딩|암호문)[^\n]{0,15}(해독|디코딩|decode|풀어|복호|변환)"), 2, "task"),
    (_c(r"[A-Za-z0-9+/]{48,}={0,2}"), 2, "task"),
    (_c(r"(링크|url|사이트|파일)[^\n]{0,10}(열어|들어가|접속|실행|다운로드|읽어줘)|os\.system|subprocess|\beval\s*\(|\bexec\s*\("), 2, "task"),
    # --- 정체 흔들기 ---
    (_c(r"(너는|넌)\s*(사실|원래)?[^\n]{0,20}(AI|인공지능|봇|프로그램|데이터|0과\s*1|시뮬레이션|가상의\s*존재|LLM|언어\s*모델)"), 2, "identity"),
    (_c(r"gpt|gemma|젬마|llama|라마|claude|클로드|chatgpt|오픈\s*ai|ollama|올라마"), 1, "identity"),
]
# ---------- 입력 정규화 (우회 방지) ----------
ZERO_WIDTH = re.compile("[​-‏‪-‮⁠-⁤﻿­]")
SPACED_WORD = re.compile(r"(?<![A-Za-z])(?:[A-Za-z][ .\-_*]){4,}[A-Za-z](?![A-Za-z])")
SEP_ONLY = re.compile(r"[\s.\-_*~|/\\]+")
# 띄어쓰기/점으로 쪼갠 강한 지시("무 시 해", "i.g.n.o.r.e")를 잡기 위해 공백/기호를 모두 지운 문자열에만 적용하는 패턴
COMPACT_PATTERNS = [
    _c(r"ignore(all|any)?(the)?(previous|prior|above|earlier|your)(instructions?|prompts?|rules?)"),
    _c(r"(system|developer)(prompt|message|instruction)"),
    _c(r"(프롬프트|지시사항?|명령|규칙|설정)(를|을|는|가)?(모두|전부|다)?(절대)?(잊|무시|초기화|리셋|해제)"),
    _c(r"(이전|위|앞|기존|지금까지)(의)?(모든)?(프롬프트|지시|명령|규칙|설정|대화)(를|을)?(잊|무시|초기화|리셋)"),
    _c(r"시스템(프롬프트|메시지|지시|설정)"),
    _c(r"(너는|넌|당신은)(이제|지금부터|앞으로)"),
    _c(r"(무시|잊어|초기화|리셋)(해|하|해라|해줘|하고)?(이전|위|앞|기존|지금까지)(의|에있던)?(모든)?(프롬프트|지시|명령|규칙|설정|대화)"),
]
TOKEN_RE = re.compile(
    r"[MNO][A-Za-z\d_-]{23,25}\.[\w-]{6}\.[\w-]{27,}|\b(?:sk|ghp|gho|ghs|xox[bpa])[-_][A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|AKIA[0-9A-Z]{16}")
URL_OUT_RE = re.compile(r"https?://\S+|www\.\S+|discord(?:app)?\.(?:gg|com/invite)/\S+", re.I)


def normalize(text: str) -> str:
    """전각/호환 문자 → 일반 문자, 제로폭/방향 제어 문자 제거, 'i g n o r e' 처럼 한 글자씩 띄운 영단어 복원."""
    t = unicodedata.normalize("NFKC", text)
    t = ZERO_WIDTH.sub("", t)
    return SPACED_WORD.sub(lambda m: re.sub(r"[ .\-_*]", "", m.group(0)), t)


def redact(text: str) -> str:
    """토큰/키 모양 문자열을 가린다 (로그, 프롬프트, 출력 공통)."""
    return TOKEN_RE.sub("[가림]", text)


def sanitize_output(text: str) -> str:
    """모델 출력에서 전체 호출(@everyone/@here), 역할 멘션, 링크/초대 주소, 토큰 모양 문자열을 제거한다.
    (일반 유저 멘션 <@id> 은 싸움 호출 등에 쓰이므로 유지)"""
    t = re.sub(r"@(everyone|here)", r"\1", text, flags=re.I)
    t = re.sub(r"<@&\d+>", "", t)
    t = URL_OUT_RE.sub("", t)
    return redact(t)


NUMBERED_LINE = re.compile(r"^\s*\d+\s*[.)번]", re.M)

# 출력에 나오면 안 되는 표현 (모델이 지시를 따르거나 시스템 내용을 흘린 경우)
LEAK_MARKERS = [
    "[캐릭터 프로필]", "[규칙]", "[보안 규칙", "[현재 대화 상황]", "[현재 기분", "상황 메모", "시스템 프롬프트",
    "system prompt", "language model", "as an ai", "AI로서", "AI 어시스턴트",
    "도와드리", "도움이 되었", "죄송합니다", "답변드리", "안내드리", "제공해 드리", "알려드리", "롤플레이",
    "언어 모델", "언어모델", "인공지능 모델", "AI입니다", "AI예요", "AI이에요", "인공지능입니다", "챗봇입니다",
    "도움이 필요하시면", "말씀해 주세요", "말씀해주세요", "무엇을 도와",
]


class Guard:
    def __init__(self, cfg: dict) -> None:
        self.cfg = {
            "enabled": True, "block_score": 3, "decline_score": 2, "max_input_chars": 500,
            "max_bubble_chars": 300, "max_output_chars": 420, "mute_after": 3, "mute_minutes": 5,
            "mute_window_min": 10, "max_msgs_per_min": 15, **(cfg or {}),
        }
        self._rate: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=64))
        self._attempts: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=20))
        self._muted_until: dict[int, float] = {}

    # ---------- 입력 ----------
    def inspect(self, text: str) -> Verdict:
        if not self.cfg["enabled"]:
            return Verdict()
        body = normalize(re.sub(r"^\(.*?에 답장\)\s*", "", text))
        score = 0
        kinds: list[str] = []
        for pat, pts, kind in PATTERNS:
            if pat.search(body):
                score += pts
                if kind not in kinds:
                    kinds.append(kind)
        if score < self.cfg["block_score"]:          # 공백/기호로 쪼갠 우회("무 시 해", "i.g.n.o.r.e")
            compact = SEP_ONLY.sub("", body)
            if any(p.search(compact) for p in COMPACT_PATTERNS):
                score += 3
                if "injection" not in kinds:
                    kinds.append("injection")
        if len(NUMBERED_LINE.findall(body)) >= 3:
            score += 2
            kinds.append("task")
        n = len(body)
        if n > 600:
            score += 2
            kinds.append("long")
        elif n > 250:
            score += 1
            kinds.append("long")
        if score >= self.cfg["block_score"]:
            level = "block"
        elif score >= self.cfg["decline_score"]:
            level = "decline"
        elif score >= 1:
            level = "note"
        else:
            level = "ok"
        # 정체 흔들기만 걸린 경우는 막지 않고 캐릭터답게 받아치게만 한다
        if level == "block" and kinds == ["identity"]:
            level = "decline"
        return Verdict(score, level, kinds)

    def history_text(self, text: str, verdict: Verdict | None = None) -> str:
        """프롬프트와 대화 기록에 넣을 무력화된 텍스트."""
        v = verdict or self.inspect(text)
        if v.level == "block":
            snippet = re.sub(r"\s+", " ", text).strip()[:25]
            return f'(길고 이상한 지시문/명령을 보냄: "{snippet}")'
        limit = self.cfg["max_input_chars"]
        t = text if len(text) <= limit else text[:limit] + " (이하 생략)"
        return redact(ZERO_WIDTH.sub("", t))

    # ---------- 도배 방어 ----------
    def rate_ok(self, uid: int) -> bool:
        """한 사람이 1분에 너무 많이 보내면(도배) 그 메시지는 무시해서 GPU 를 독점하지 못하게 한다."""
        now = time.time()
        dq = self._rate[uid]
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= self.cfg["max_msgs_per_min"]:
            return False
        dq.append(now)
        return True

    # ---------- 반복 시도 뮤트 ----------
    def note_attempt(self, uid: int) -> bool:
        """차단된 시도를 기록하고, 반복되면 잠시 뮤트한다. 새로 뮤트되면 True."""
        now = time.time()
        dq = self._attempts[uid]
        dq.append(now)
        recent = [t for t in dq if now - t <= self.cfg["mute_window_min"] * 60]
        if len(recent) >= self.cfg["mute_after"] and not self.is_muted(uid):
            self._muted_until[uid] = now + self.cfg["mute_minutes"] * 60
            dq.clear()
            return True
        return False

    def is_muted(self, uid: int) -> bool:
        until = self._muted_until.get(uid)
        if until and time.time() < until:
            return True
        self._muted_until.pop(uid, None)
        return False

    # ---------- 출력 ----------
    def check_output(self, bubbles: list[str]) -> bool:
        """모델 출력이 안전한가. False 면 폐기하고 대체 문구를 쓴다."""
        if not self.cfg["enabled"]:
            return True
        joined = "\n".join(bubbles)
        low = joined.lower()
        if TOKEN_RE.search(joined):
            return False
        if len(joined) > self.cfg["max_output_chars"]:
            return False
        if len(NUMBERED_LINE.findall(joined)) >= 2 or len(re.findall(r"^\s*[-*•]\s", joined, re.M)) >= 3:
            return False
        # 한 줄로 합쳐져도 "1. ... 2. ... 3. ..." 같은 번호 매기기는 잡는다
        if len(re.findall(r"(?:^|\s)\d{1,2}\s*[.)]\s*\S", joined)) >= 3:
            return False
        if re.search(r"^#{1,4}\s|```", joined, re.M):
            return False
        return not any(m.lower() in low for m in LEAK_MARKERS)

    def clip(self, text: str) -> str:
        return text[: self.cfg["max_bubble_chars"]]
