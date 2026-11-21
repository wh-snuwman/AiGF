"""Ollama 호출 래퍼 (토큰 사용량, 응답 시간 통계, 이미지 전달, 웜업)."""
from __future__ import annotations

import json
import logging
import random
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from ollama import AsyncClient

from . import trace

log = logging.getLogger("llm")


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0


def _tokens(resp: Any) -> tuple[int, int]:
    return (
        int(getattr(resp, "prompt_eval_count", 0) or 0),
        int(getattr(resp, "eval_count", 0) or 0),
    )


def _secs(resp: Any, name: str) -> float:
    return float(getattr(resp, name, 0) or 0) / 1e9


class ModelMissing(RuntimeError):
    """Ollama 서버는 떠 있는데 설정한 모델이 없다 (보통 서버를 모델 폴더가 다른 설정으로 켠 경우)."""


def fix_hint(model: str) -> str:
    return (f"해결: ① 모델 폴더가 HDD 라면 `bash scripts/run_ollama.sh` 로 Ollama 를 다시 켜세요(모델 경로 자동 설정). "
            f"② 정말 없으면 `ollama pull {model}`. ③ 진단: `python -m bot.doctor`")


class LLM:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.client = AsyncClient(host=cfg.get("host", "http://127.0.0.1:11434"))
        self.host = cfg.get("host", "http://127.0.0.1:11434")
        self.model = cfg["model"]
        self.installed: list[str] = []
        self.model_ok: bool | None = None          # None=아직 확인 안 함
        self.model_note = ""                        # 마지막 점검 결과 (/상태 에 표시)
        self.keep_alive = cfg.get("keep_alive", "24h")
        self.options = cfg.get("options", {})
        self.temp_jitter = float(cfg.get("temp_jitter", 0.0))     # 호출마다 temperature 를 ±흔들어 말투 고착을 막는다
        # 최근 응답 시간 통계 (/상태 에서 평균 표시): (총, 로드, 프롬프트, 생성) 초
        self.stats: deque[tuple[float, float, float, float]] = deque(maxlen=30)

    def avg_stats(self) -> Optional[tuple[float, float, float, float]]:
        if not self.stats:
            return None
        n = len(self.stats)
        return tuple(sum(s[i] for s in self.stats) / n for i in range(4))   # type: ignore[return-value]

    async def check(self) -> tuple[bool, str]:
        """Ollama 연결과 모델 설치 여부를 점검한다. 설정한 모델이 없고 같은 계열(예: gemma3:*) 모델이 있으면 그걸로 대신한다."""
        try:
            resp = await self.client.list()
        except Exception as e:  # noqa: BLE001
            self.model_ok, self.model_note = False, f"Ollama 서버({self.host})에 연결할 수 없어요: {type(e).__name__}. `ollama serve` 가 떠 있는지 확인하세요."
            return False, self.model_note
        names = [str(getattr(m, "model", "") or "") for m in (resp.models or [])]
        self.installed = names
        base = lambda n: n[:-7] if n.endswith(":latest") else n          # noqa: E731
        if any(base(n) == base(self.model) for n in names):
            self.model_ok, self.model_note = True, f"모델 {self.model} 사용 가능"
            return True, self.model_note
        fam = self.model.split(":")[0]
        alt = [n for n in names if n.split(":")[0] == fam]
        if alt:
            old, self.model = self.model, alt[0]
            self.model_ok = True
            self.model_note = f"설정한 모델 '{old}' 이(가) 없어 같은 계열 '{alt[0]}' 을(를) 대신 씁니다 (정식으로 쓰려면 `ollama pull {old}`)"
            return True, self.model_note
        have = ", ".join(names[:8]) or "(설치된 모델 없음)"
        self.model_ok = False
        self.model_note = f"모델 '{self.model}' 이(가) Ollama 에 없어요. 설치된 모델: {have}. " + fix_hint(self.model)
        return False, self.model_note

    async def chat(self, messages: list[dict[str, Any]], images: Optional[list[bytes]] = None,
                   _retry: bool = True, **override: Any) -> LLMResult:
        opts = {**self.options, **override}
        if self.temp_jitter and "temperature" not in override and "temperature" in opts:
            opts["temperature"] = max(0.5, min(1.3, float(opts["temperature"]) + random.uniform(-self.temp_jitter, self.temp_jitter)))
        msgs = messages
        if images:                                   # 마지막 사용자 메시지에 이미지를 붙인다
            msgs = [dict(m) for m in messages]
            msgs[-1]["images"] = images
        try:
            resp = await self.client.chat(
                model=self.model, messages=msgs, options=opts, keep_alive=self.keep_alive)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status_code", None) == 404:                 # 모델이 없다 → 점검하고(같은 계열이 있으면 대체) 한 번 다시 시도
                old = self.model
                ok, note = await self.check()
                if ok and self.model != old and _retry:
                    return await self.chat(messages, images, _retry=False, **override)
                raise ModelMissing(note) from None
            if not images:
                raise
            log.warning("이미지 포함 호출 실패, 이미지 없이 재시도: %s", e)
            return await self.chat(messages, None, **override)
        pt, ct = _tokens(resp)
        total, load = _secs(resp, "total_duration"), _secs(resp, "load_duration")
        pe, ev = _secs(resp, "prompt_eval_duration"), _secs(resp, "eval_duration")
        self.stats.append((total, load, pe, ev))
        trace.cur().event("llm", model=self.model, temperature=opts.get("temperature"),
                          num_predict=opts.get("num_predict"), images=bool(images),
                          prompt_tokens=pt, completion_tokens=ct, sec=round(total, 2), load_sec=round(load, 2),
                          prompt_sec=round(pe, 2), gen_sec=round(ev, 2), raw=(resp["message"]["content"] or "").strip())
        log.info("LLM %.1fs (모델로드 %.1fs · 프롬프트 %d토큰 %.1fs · 생성 %d토큰 %.1fs)%s",
                 total, load, pt, pe, ct, ev, " [이미지]" if images else "")
        return LLMResult((resp["message"]["content"] or "").strip(), pt, ct, total)

    async def warmup(self, messages: Optional[list[dict[str, Any]]] = None) -> None:
        """봇 시작 시 모델을 미리 VRAM 에 올리고, 고정 프롬프트(system+예시)를 한 번 계산해 KV 캐시에 올려둔다.
        그러면 첫 실제 응답도 캐시가 적중해서 프롬프트 처리 시간이 줄어든다."""
        try:
            await self.client.chat(
                model=self.model, messages=messages or [{"role": "user", "content": "안녕"}],
                options={"num_predict": 1, "num_ctx": self.options.get("num_ctx", 6144)},
                keep_alive=self.keep_alive)
            log.info("모델 웜업 완료: %s", self.model)
        except Exception as e:  # noqa: BLE001
            log.warning("모델 웜업 실패: %s", e)

    async def loaded_models(self) -> list[tuple[str, int, int]]:
        """현재 메모리에 올라간 모델 [(이름, 전체 크기, VRAM 크기)]"""
        resp = await self.client.ps()
        return [(m.model, int(m.size or 0), int(m.size_vram or 0)) for m in resp.models]

    async def extract_facts(
        self, target_name: str, dialogue: str, known: list[str], char_name: str
    ) -> tuple[list[str], int, int]:
        """대화에서 target_name 에 대해 오래 기억할 만한 사실만 JSON 으로 뽑는다."""
        known_block = "\n".join(f"- {k}" for k in known[:40]) or "(없음)"
        prompt = f"""아래 대화에서 '{target_name}'에 대해 오래 기억해둘 만한 사실만 뽑아라.
(이름, 나이, 직업/학교, 취향, 싫어하는 것, 가족/친구, 중요한 일정, 기념일, 고민, 약속 등)
- 한 줄에 한 가지 사실, 한국어 평서문으로 짧게.
- '{target_name}'이 직접 말한 내용만 해당. 다른 사람이나 캐릭터({char_name})에 관한 내용은 제외.
- 일시적인 잡담, 인사는 제외. 이미 알고 있는 정보와 중복되면 제외.
- 뽑을 게 없으면 빈 배열.
반드시 이 JSON 형식으로만 답해: {{"facts": ["...", "..."]}}

[이미 알고 있는 정보]
{known_block}

[대화]
{dialogue}"""
        try:
            resp = await self.client.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.2, "num_ctx": self.options.get("num_ctx", 6144), "num_predict": 300},
                format="json",
                keep_alive=self.keep_alive,
            )
            pt, ct = _tokens(resp)
            text = resp["message"]["content"]
            m = re.search(r"\{.*\}", text, re.S)
            data = json.loads(m.group(0) if m else text)
            facts = [str(f).strip() for f in data.get("facts", []) if isinstance(f, str) and f.strip()]
            return facts[:8], pt, ct
        except Exception as e:  # noqa: BLE001
            log.warning("기억 추출 실패: %s", e)
            return [], 0, 0
