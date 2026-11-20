"""정형행동(같은 말/같은 문장 틀 반복) 방지: LLM 호출 없이 계산하는 다양성 추적기.

토큰 단위 반복 페널티는 '같은 문장 틀'이나 '여러 번의 독립 생성에 걸친 반복'을 못 잡는다.
그래서 봇이 최근에 실제로 보낸 말풍선을 채널별로 기억하고, 아래를 코드로 감시한다.
- 시작어: 최근 6줄 중 2번 이상 같은 말로 시작하면 금지 (예: '아니', '그게')
- 끝맺음: 최근 8줄 중 3번 이상 같은 어미로 끝나면 프롬프트에서 다르게 말하라고 안내 (예: '~거든')
- 문장 중복: 최근 20줄 중 거의 같은 줄(문자 2-gram Dice 유사도)이면 그 말풍선을 버린다
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict, deque

_KEEP = re.compile(r"[^0-9A-Za-z가-힣]")


def norm(text: str) -> str:
    return _KEEP.sub("", text).lower()


def _bigrams(s: str) -> Counter[str]:
    return Counter(s[i:i + 2] for i in range(len(s) - 1)) if len(s) >= 2 else Counter([s]) if s else Counter()


def dice(a: str, b: str) -> float:
    """문자 2-gram Dice 유사도 (0~1). 띄어쓰기/문장부호/이모티콘 차이를 무시한다."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ba, bb = _bigrams(na), _bigrams(nb)
    inter = sum((ba & bb).values())
    return 2.0 * inter / (sum(ba.values()) + sum(bb.values()))


def opener(line: str) -> str:
    """줄의 첫 단어(조사/어미 변화를 무시하기 위해 앞 두 글자). 한 글자 감탄사('응','와')는 대상 아님."""
    m = re.match(r"\s*([가-힣A-Za-z]{2,8})", line)
    if not m:
        return ""
    w = m.group(1)
    return w[:2] if len(w) > 2 else w


def ender(line: str) -> str:
    """줄의 끝 두 글자(한글만). 'ㅋㅋ', 'ㅠㅠ' 같은 자모 이모티콘, 물음표/느낌표는 걷어낸 뒤 판단."""
    t = re.sub(r"[ㄱ-ㅎㅏ-ㅣ\s.,!?~;^/]+$", "", line)
    m = re.search(r"([가-힣]{2})$", t)
    return m.group(1) if m else ""


class Diversity:
    def __init__(self, cfg: dict | None = None) -> None:
        c = {"enabled": True, "keep": 40, "dup_thresh": 0.72, "dup_window": 20,
             "opener_window": 6, "opener_min": 2, "ender_window": 8, "ender_min": 3, **(cfg or {})}
        self.cfg = c
        self._lines: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=int(c["keep"])))

    # ---------- 기록 ----------
    def record(self, channel_id: int, bubbles: list[str]) -> None:
        for b in bubbles:
            if b and b.strip():
                self._lines[channel_id].append(b.strip())

    def lines(self, channel_id: int, own: list[str] | None = None) -> list[str]:
        """메모리에 기록된 말과 DB 기록(own)을 합쳐 최근 순서로 (재시작 직후에도 동작)."""
        mem = list(self._lines[channel_id])
        if not own:
            return mem
        extra = [ln for ln in own if ln not in mem]
        return (extra + mem)[-int(self.cfg["keep"]):]

    # ---------- 분석 ----------
    def banned_openers(self, lines: list[str]) -> list[str]:
        w = int(self.cfg["opener_window"])
        c = Counter(o for o in (opener(ln) for ln in lines[-w:]) if o)
        return [o for o, n in c.most_common() if n >= int(self.cfg["opener_min"])][:4]

    def repeated_enders(self, lines: list[str]) -> list[str]:
        w = int(self.cfg["ender_window"])
        c = Counter(e for e in (ender(ln) for ln in lines[-w:]) if e)
        return [e for e, n in c.most_common() if n >= int(self.cfg["ender_min"])][:3]

    def is_duplicate(self, line: str, lines: list[str]) -> bool:
        n = norm(line)
        if len(n) < 4:                                # 'ㅇㅇ','응' 같은 초단문은 직전 2줄과 완전히 같을 때만 중복
            return bool(n) and any(norm(p) == n for p in lines[-2:])
        thr = float(self.cfg["dup_thresh"])
        return any(dice(line, p) >= thr for p in lines[-int(self.cfg["dup_window"]):])

    def drop_duplicates(self, bubbles: list[str], lines: list[str]) -> list[str]:
        """이미 한 말과 거의 같은 말풍선은 버린다. (같은 응답 안의 중복도 제거)"""
        if not self.cfg["enabled"]:
            return bubbles
        out: list[str] = []
        for b in bubbles:
            if not self.is_duplicate(b, lines + out):
                out.append(b)
        return out

    def hint(self, lines: list[str]) -> str:
        """프롬프트에 넣을 한 줄 (비어 있으면 빈 문자열)"""
        if not self.cfg["enabled"]:
            return ""
        parts = []
        bo = self.banned_openers(lines)
        if bo:
            parts.append("시작어 금지:" + ",".join(bo))
        en = self.repeated_enders(lines)
        if en:
            parts.append("끝맺음 반복중(다르게 끝내라):~" + ",~".join(en))
        return " / ".join(parts)

    def stats(self, channel_id: int) -> str:
        lines = list(self._lines[channel_id])
        if not lines:
            return "기록 없음"
        c = Counter(o for o in (opener(ln) for ln in lines) if o).most_common(3)
        e = Counter(x for x in (ender(ln) for ln in lines) if x).most_common(3)
        o_txt = ", ".join(f"{k}x{n}" for k, n in c if n >= 2) or "-"
        e_txt = ", ".join(f"~{k}x{n}" for k, n in e if n >= 2) or "-"
        return f"최근 {len(lines)}줄 | 시작어 {o_txt} | 끝맺음 {e_txt}"
