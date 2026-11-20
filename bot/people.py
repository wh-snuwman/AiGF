"""서버의 텍스트 파일(config/people.txt)로 사람별 호칭과 '캐릭터가 가진 이미지'를 관리한다.

형식 (한 줄에 한 명):
    유저아이디:캐릭터가부를이름:사람에대한AI의이미지

파일을 수정하면 봇 재시작 없이 다음 대화부터 자동 반영된다.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .memory import Memory

log = logging.getLogger("people")

HEADER = """\
# ============================================================
# 사람별 호칭 / 이미지 설정 (이 파일을 직접 수정하세요. 저장하면 바로 반영됩니다)
#
# 형식 (한 줄에 한 명):
#   유저아이디:캐릭터가부를이름:사람에대한AI의이미지
#
# 예시:
#   123456789012345678:대호:멍청한 개발자. 버그만 만들면서 허세를 부림. 그래도 미워할 수는 없음
#   234567890123456789:민수야:같이 노는 친한 친구. 장난을 많이 치지만 힘들어 보이면 챙겨줌
#
# - 유저아이디: 디스코드 개발자 모드를 켜고 프로필 우클릭 → '사용자 ID 복사'
# - 캐릭터가부를이름: 닉네임 대신 쓸 진짜 이름/호칭 (비워두면 닉네임 사용)
# - 이미지: 캐릭터가 그 사람을 어떻게 보는지 (비워도 됨, 이미지에 ':' 가 들어가도 괜찮음)
# - '#' 로 시작하는 줄은 주석입니다.
# ============================================================
"""


def _clean(s: str) -> str:
    """프롬프트에 들어가는 이름/인식 문구에서 제어문자, 백틱, 꺾쇠/대괄호 같은 구조 문자를 없앤다 (프롬프트 오염 방지)."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f`<>{}\[\]|]", " ", s)).strip()


def parse(text: str) -> tuple[dict[int, tuple[str, str | None]], dict[int, str], list[str]]:
    names: dict[int, tuple[str, str | None]] = {}
    views: dict[int, str] = {}
    errors: list[str] = []
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        line = line.replace("：", ":")
        parts = line.split(":", 2)
        uid_s = parts[0].strip()
        if len(parts) < 2 or not uid_s.isdigit():
            errors.append(f"{no}번째 줄: '유저아이디:이름:이미지' 형식이 아니에요 → {raw.strip()[:40]}")
            continue
        uid = int(uid_s)
        name = _clean(parts[1])[:30]
        image = _clean(parts[2])[:300] if len(parts) > 2 else ""
        if name:
            names[uid] = (name, None)
        if image:
            views[uid] = image
    return names, views, errors


class People:
    def __init__(self, path: Path, memory: Memory) -> None:
        self.path = path
        self.mem = memory
        self.names: dict[int, tuple[str, str | None]] = {}
        self.views: dict[int, str] = {}
        self.errors: list[str] = []
        self._mtime = 0.0

    async def init(self) -> None:
        """파일이 없으면 만든다. (이전 버전의 /이름, /인식 DB 값이 있으면 옮겨 적는다)"""
        if not self.path.exists():
            lines = [HEADER]
            try:
                names = await self.mem.all_real_names()
                views = await self.mem.all_views()
                for uid in sorted(set(names) | set(views)):
                    real = (names.get(uid) or ("", None))
                    name = (real[1] or real[0]) if real else ""
                    lines.append(f"{uid}:{name}:{views.get(uid, '')}")
            except Exception:  # noqa: BLE001
                log.exception("이전 설정 이전 실패")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log.info("people.txt 생성: %s", self.path)
        self.reload()

    def reload(self) -> int:
        try:
            text = self.path.read_text(encoding="utf-8")
            self._mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self.names, self.views, self.errors = {}, {}, []
            return 0
        self.names, self.views, self.errors = parse(text)
        for e in self.errors:
            log.warning("people.txt %s", e)
        return len(set(self.names) | set(self.views))

    def reload_if_changed(self) -> bool:
        try:
            m = self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        if m != self._mtime:
            n = self.reload()
            log.info("people.txt 변경 감지 → %d명 다시 불러옴", n)
            return True
        return False
