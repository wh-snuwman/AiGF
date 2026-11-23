"""문법 교정 보조 모델: 메인 모델(gemma3 12B)이 가끔 만드는 어색한 말(조사/어미 오류, 없는 낱말, 번역투)을
작은 한국어 모델이 한 번 더 고친다.

원칙
- 의미, 말투(반말/존댓말), 길이, ㅋㅋ 같은 표현, 줄 수는 그대로 두고 틀린 부분만 고친다.
- 느리거나 이상하게 고치면 원문을 쓴다: 시간 제한, 줄 수/유사도/외국 문자/설명문 검사에 걸리면 버린다.
- 모델이 없거나 연속으로 실패하면 스스로 잠시 꺼지고(대화는 영향 없음) 일정 시간 뒤 다시 시도한다.
- 메인 모델과 GPU 를 동시에 쓰지 않도록 호출하는 쪽(응답 생성)이 이미 GPU 락 안에 있어 순서대로 돈다.
- 싸움 중(거친 말)에는 쓰지 않는다. 호출하는 쪽에서 fix=False 로 건너뛴다.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from .diversity import dice

log = logging.getLogger("corrector")

_F = (r"Ͱ-ϿЀ-ԯ԰-֏֐-׿؀-ۿऀ-ॿ"
      r"฀-๿Ⴀ-ჿ぀-ヿ㐀-䶿一-鿿")
FOREIGN = re.compile(rf"[{_F}]")
HANGUL = re.compile(r"[가-힣]")
SKIP_RE = re.compile(r"^\W*\[?SKIP\]?\W*$", re.I)
META = re.compile(r"(수정|교정|맞춤법|고쳐|입력|출력|문장입니다|결과)")
PREFIX = re.compile(r"^\s*(?:입력|출력|수정|교정|결과)\s*[:：]\s*")

SYSTEM = ("너는 한국어 채팅 문장 교정기다. 입력의 맞춤법, 띄어쓰기, 조사, 어미, 어색하거나 존재하지 않는 낱말, 번역투만 자연스럽게 고친다. "
          "뜻, 말투(반말/존댓말), 길이, ㅋㅋ 같은 표현은 바꾸지 않는다. 줄 수도 그대로 둔다. "
          "이미 자연스러우면 그대로 출력한다. 설명 없이 고친 문장만 출력한다.")
SHOTS = [
    ("오늘 날씨가 좋아서 기분이 좋아 졌어", "오늘 날씨가 좋아서 기분이 좋아졌어"),
    ("그거 진짜 웃기네요 ㅋㅋ", "그거 진짜 웃기네요 ㅋㅋ"),
]


def declared_register(speech_style: str) -> str:
    """캐릭터 말투 설명에서 격식을 읽는다: 반말 / 존댓말 / 섞임 / 모름"""
    s = str(speech_style or "")
    neg = re.sub(r"존댓말(은|을)?\s*(절대\s*)?(쓰지|안)", "", s)
    has_ban, has_jon = "반말" in neg, ("존댓말" in neg or "해요체" in neg)
    return "섞임" if has_ban and has_jon else "반말" if has_ban else "존댓말" if has_jon else "모름"


def accept(orig: str, fixed: str, min_sim: float = 0.55) -> bool:
    """교정 결과를 믿어도 되는가 (보수적으로)."""
    if not fixed or fixed.strip() == orig.strip():
        return False
    o_lines = [x for x in orig.splitlines() if x.strip()]
    f_lines = [x for x in fixed.splitlines() if x.strip()]
    if len(o_lines) != len(f_lines):
        return False
    if FOREIGN.search(fixed) and not FOREIGN.search(orig):
        return False
    if HANGUL.search(orig) and not HANGUL.search(fixed):
        return False
    if META.search(fixed) and not META.search(orig):
        return False                                         # 설명문/머리말이 섞였다
    ratio = len(fixed) / max(1, len(orig))
    if not 0.6 <= ratio <= 1.5:
        return False
    return all(dice(a, b) >= min_sim for a, b in zip(o_lines, f_lines))


class Corrector:
    def __init__(self, cfg: dict | None, host: str = "http://127.0.0.1:11434") -> None:
        self.cfg = {"enabled": True, "model": "qwen2.5:3b", "timeout_sec": 1.5, "min_chars": 4, "max_chars": 240,
                    "keep_alive": "24h", "min_similarity": 0.55, **(cfg or {})}
        self.host = host
        self._client: Any = None
        self.disabled_until = 0.0
        self._fail_streak = 0
        self.stats = {"calls": 0, "changed": 0, "rejected": 0, "timeout": 0, "error": 0, "sec": 0.0}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled")) and time.time() >= self.disabled_until

    def _cli(self):
        if self._client is None:
            import ollama
            self._client = ollama.AsyncClient(host=self.host)
        return self._client

    def _pause(self, sec: float, why: str) -> None:
        self.disabled_until = time.time() + sec
        log.warning("문법 교정을 %d분 쉽니다: %s (대화에는 영향 없음)", max(1, int(sec // 60)), why)

    @staticmethod
    def _kv_error(e: Exception) -> bool:
        """KV 캐시 양자화(OLLAMA_KV_CACHE_TYPE=q8_0)와 모델의 헤드 크기가 안 맞아 로드에 실패한 경우 (예: exaone 의 head 80)"""
        m = str(e)
        return "does not divide" in m or "failed to create context" in m

    def _pause_kv(self) -> None:
        self._pause(3600, f"모델 {self.cfg['model']} 은 서버의 KV 캐시 설정(q8_0)과 맞지 않아 로드되지 않아요 → settings.yaml 의 "
                          "corrector.model 을 qwen2.5:3b 처럼 헤드 크기가 128/256 인 모델로 바꾸세요")

    async def warmup(self) -> bool:
        """시작할 때 보조 모델을 VRAM 에 올려 둔다. 모델이 없으면 알려 주고 꺼 둔다."""
        if not self.cfg.get("enabled"):
            return False
        try:
            await self._cli().chat(model=self.cfg["model"], messages=[{"role": "user", "content": "안녕"}],
                                   options={"num_predict": 1, "num_ctx": 1024}, keep_alive=self.cfg["keep_alive"])
            log.info("문법 교정 모델 준비: %s", self.cfg["model"])
            return True
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status_code", None) == 404:
                self._pause(600, f"모델 {self.cfg['model']} 이 없어요 → `ollama pull {self.cfg['model']}`")
            elif self._kv_error(e):
                self._pause_kv()
            else:
                self._pause(300, f"{type(e).__name__}")
            return False

    async def fix(self, text: str, register: str = "") -> str:
        """text(여러 줄 가능)를 교정해서 돌려준다. 문제가 있으면 원문 그대로."""
        t = (text or "").strip()
        if (not self.enabled or len(t) < int(self.cfg["min_chars"]) or len(t) > int(self.cfg["max_chars"])
                or SKIP_RE.match(t) or not HANGUL.search(t)):
            return text
        msgs: list[dict[str, str]] = [{"role": "system", "content": SYSTEM + (f" 이 말은 {register}이다." if register in ("반말", "존댓말") else "")}]
        for a, b in SHOTS:
            msgs += [{"role": "user", "content": a}, {"role": "assistant", "content": b}]
        msgs.append({"role": "user", "content": t})
        self.stats["calls"] += 1
        t0 = time.perf_counter()
        try:
            r = await asyncio.wait_for(
                self._cli().chat(model=self.cfg["model"], messages=msgs, keep_alive=self.cfg["keep_alive"],
                                 options={"temperature": 0.1, "num_predict": min(200, int(len(t) * 1.3) + 24), "num_ctx": 1024}),
                timeout=float(self.cfg["timeout_sec"]))
        except asyncio.TimeoutError:
            self.stats["timeout"] += 1
            self._fail_streak += 1
            if self._fail_streak >= 5:
                self._pause(300, "시간 초과가 계속돼요")
                self._fail_streak = 0
            return text
        except Exception as e:  # noqa: BLE001
            self.stats["error"] += 1
            if getattr(e, "status_code", None) == 404:
                self._pause(600, f"모델 {self.cfg['model']} 이 없어요 → `ollama pull {self.cfg['model']}`")
            elif self._kv_error(e):
                self._pause_kv()
            else:
                self._fail_streak += 1
                if self._fail_streak >= 3:
                    self._pause(300, f"{type(e).__name__}")
                    self._fail_streak = 0
            return text
        self._fail_streak = 0
        dt = time.perf_counter() - t0
        self.stats["sec"] += dt
        out = PREFIX.sub("", str((r["message"]["content"] if isinstance(r, dict) or hasattr(r, "__getitem__") else "") or "")).strip().strip('"').strip()
        if accept(t, out, float(self.cfg["min_similarity"])):
            self.stats["changed"] += 1
            log.info("문법 교정(%.2fs): %r -> %r", dt, t[:60], out[:60])
            return out
        if out and out != t:
            self.stats["rejected"] += 1
            log.debug("문법 교정 결과를 버림: %r -> %r", t[:40], out[:40])
        return text

    def summary(self) -> str:
        s = self.stats
        if not self.cfg.get("enabled"):
            return "문법 교정: 꺼짐"
        n = max(1, s["calls"])
        state = "" if self.enabled else " (잠시 쉬는 중)"
        return (f"문법 교정{state}: {self.cfg['model']} · 호출 {s['calls']} · 수정 {s['changed']} · 버림 {s['rejected']} · "
                f"시간초과 {s['timeout']} · 오류 {s['error']} · 평균 {s['sec'] / n:.2f}s")
