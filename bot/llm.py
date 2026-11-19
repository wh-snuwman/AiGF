"""Ollama 호출 래퍼 (토큰 사용량 반환 포함)."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from ollama import AsyncClient

log = logging.getLogger("llm")


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _tokens(resp: Any) -> tuple[int, int]:
    return (
        int(getattr(resp, "prompt_eval_count", 0) or 0),
        int(getattr(resp, "eval_count", 0) or 0),
    )


class LLM:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.client = AsyncClient(host=cfg.get("host", "http://127.0.0.1:11434"))
        self.model = cfg["model"]
        self.keep_alive = cfg.get("keep_alive", "30m")
        self.options = cfg.get("options", {})

    async def chat(self, messages: list[dict[str, str]], **override: Any) -> LLMResult:
        opts = {**self.options, **override}
        resp = await self.client.chat(
            model=self.model, messages=messages, options=opts, keep_alive=self.keep_alive,
        )
        pt, ct = _tokens(resp)
        return LLMResult((resp["message"]["content"] or "").strip(), pt, ct)

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
                options={"temperature": 0.2, "num_ctx": self.options.get("num_ctx", 8192), "num_predict": 300},
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
