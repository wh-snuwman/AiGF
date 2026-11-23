"""프롬프트 토큰 예산: 토큰 수를 어림 계산하고, 예산을 넘으면 오래된 대화부터 줄인다.

- Ollama 에는 토큰 계산 API 가 없어서 글자 종류별 계수로 어림한다. 계수는 `scripts/bench.py --calibrate` 가
  서버의 실제 모델에서 측정해 `data/bench/calib.json` 에 저장하면 그 값을 쓴다 (측정 전에는 넉넉한 기본값).
- 줄이는 순서: 가장 오래된 대화 기록부터. 현재 메시지(마지막)와 최근 `keep_last` 개는 지킨다.
- 프롬프트가 짧을수록 프롬프트 처리(prefill) 시간이 줄어 응답이 빨라진다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CALIB_FILE = Path(__file__).resolve().parent.parent / "data" / "bench" / "calib.json"
DEFAULT = {"hangul": 0.75, "ascii": 0.3, "other": 0.6, "per_message": 6}     # 글자당 토큰 수 (넉넉하게 잡은 기본값)
_HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
_ASCII = re.compile(r"[A-Za-z0-9]")

_calib: dict | None = None


def calibration() -> dict:
    global _calib
    if _calib is None:
        _calib = dict(DEFAULT)
        try:
            data = json.loads(CALIB_FILE.read_text(encoding="utf-8"))
            _calib.update({k: float(v) for k, v in data.items() if k in DEFAULT})
        except (OSError, ValueError):
            pass
    return _calib


def reset_calibration() -> None:
    global _calib
    _calib = None


def estimate(text: str, calib: dict | None = None) -> int:
    c = calib or calibration()
    s = str(text or "")
    h = len(_HANGUL.findall(s))
    a = len(_ASCII.findall(s))
    o = max(0, len(s) - h - a)
    return int(round(h * c["hangul"] + a * c["ascii"] + o * c["other"]))


def messages_tokens(messages: list[dict[str, Any]], calib: dict | None = None) -> int:
    c = calib or calibration()
    return sum(estimate(m.get("content", ""), c) + int(c["per_message"]) for m in messages)


def fit(system: str, examples: list[dict[str, Any]], history: list[dict[str, Any]], budget: int,
        keep_last: int = 4) -> tuple[list[dict[str, Any]], dict]:
    """system + examples + history 의 토큰 어림값이 budget 을 넘으면 history 의 오래된 것부터 뺀다.
    history 의 마지막(현재 메시지)은 항상 남긴다. 반환: (줄인 history, {before, after, dropped, budget})."""
    c = calibration()
    fixed = estimate(system, c) + messages_tokens(examples, c)
    before = fixed + messages_tokens(history, c)
    hist = list(history)
    dropped = 0
    if budget > 0:
        while len(hist) > max(1, keep_last) and fixed + messages_tokens(hist, c) > budget:
            hist.pop(0)
            dropped += 1
    # 대화가 user/assistant 번갈아 나오도록 맨 앞이 assistant 면 하나 더 뺀다 (시작이 어색해지지 않게)
    while dropped and len(hist) > max(1, keep_last) and hist[0].get("role") == "assistant":
        hist.pop(0)
        dropped += 1
    after = fixed + messages_tokens(hist, c)
    return hist, {"before": before, "after": after, "dropped": dropped, "budget": budget}
