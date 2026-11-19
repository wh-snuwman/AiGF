"""Ollama 호출 래퍼 (토큰 사용량, 응답 시간 통계, 이미지 전달, 웜업)."""
from __future__ import annotations

import json
import logging
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from ollama import AsyncClient

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


class LLM:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.client = AsyncClient(host=cfg.get("host", "http://127.0.0.1:11434"))
        self.model = cfg["model"]
        self.keep_alive = cfg.get("keep_alive", "24h")
        self.options = cfg.get("options", {})
        # 최근 응답 시간 통계 (/상태 에서 평균 표시): (총, 로드, 프롬프트, 생성) 초
        self.stats: deque[tuple[float, float, float, float]] = deque(maxlen=30)

    def avg_stats(self) -> Optional[tuple[float, float, float, float]]:
        if not self.stats:
            return None
        n = len(self.stats)
        return tuple(sum(s[i] for s in self.stats) / n for i in range(4))   # type: ignore[return-value]

    async def chat(self, messages: list[dict[str, Any]], images: Optional[list[bytes]] = None,
                   **override: Any) -> LLMResult:
        opts = {**self.options, **override}
        msgs = messages
        if images:                                   # 마지막 사용자 메시지에 이미지를 붙인다
            msgs = [dict(m) for m in messages]
            msgs[-1]["images"] = images
        try:
            resp = await self.client.chat(
                model=self.model, messages=msgs, options=opts, keep_alive=self.keep_alive)
        except Exception as e:  # noqa: BLE001
            if not images:
                raise
            log.warning("이미지 포함 호출 실패, 이미지 없이 재시도: %s", e)
            return await self.chat(messages, None, **override)
        pt, ct = _tokens(resp)
        total, load = _secs(resp, "total_duration"), _secs(resp, "load_duration")
        pe, ev = _secs(resp, "prompt_eval_duration"), _secs(resp, "eval_duration")
        self.stats.append((total, load, pe, ev))
        log.info("LLM %.1fs (모델로드 %.1fs · 프롬프트 %d토큰 %.1fs · 생성 %d토큰 %.1fs)%s",
                 total, load, pt, pe, ct, ev, " [이미지]" if images else "")
        return LLMResult((resp["message"]["content"] or "").strip(), pt, ct, total)

    async def warmup(self) -> None:
        """봇 시작 시 모델을 미리 VRAM 에 올려서 첫 응답이 느리지 않게 한다."""
        try:
            await self.client.chat(
                model=self.model, messages=[{"role": "user", "content": "안녕"}],
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
