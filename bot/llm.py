"""Ollama 호출 래퍼."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ollama import AsyncClient

log = logging.getLogger("llm")


class LLM:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.client = AsyncClient(host=cfg.get("host", "http://127.0.0.1:11434"))
        self.model = cfg["model"]
        self.keep_alive = cfg.get("keep_alive", "30m")
        self.options = cfg.get("options", {})

    async def chat(self, messages: list[dict[str, str]], **override: Any) -> str:
        opts = {**self.options, **override}
        resp = await self.client.chat(
            model=self.model,
            messages=messages,
            options=opts,
            keep_alive=self.keep_alive,
        )
        return (resp["message"]["content"] or "").strip()

    async def extract_facts(self, user_title: str, dialogue: str, known: list[str], char_name: str) -> list[str]:
        """대화에서 오래 기억할 만한 사실만 JSON 으로 뽑는다."""
        known_block = "\n".join(f"- {k}" for k in known[:40]) or "(없음)"
        prompt = f"""아래 대화에서 '{user_title}'(사용자)에 대해 오래 기억해둘 만한 사실만 뽑아라.
(이름, 나이, 직업/학교, 취향, 싫어하는 것, 가족/친구, 중요한 일정, 기념일, 고민, 약속 등)
- 한 줄에 한 가지 사실, 한국어 평서문으로 짧게.
- 일시적인 잡담, 인사, 캐릭터({char_name})에 관한 내용은 제외.
- 이미 알고 있는 정보와 중복되면 제외.
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
            text = resp["message"]["content"]
            m = re.search(r"\{.*\}", text, re.S)
            data = json.loads(m.group(0) if m else text)
            facts = data.get("facts", [])
            return [str(f).strip() for f in facts if isinstance(f, (str,)) and f.strip()][:8]
        except Exception as e:  # noqa: BLE001
            log.warning("기억 추출 실패: %s", e)
            return []
