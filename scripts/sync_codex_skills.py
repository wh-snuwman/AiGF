"""Claude Code 스킬(.claude/skills)을 Codex 용(.agents/skills)으로 복사한다.

  python scripts/sync_codex_skills.py          .claude/skills -> .agents/skills 동기화
  python scripts/sync_codex_skills.py --check  차이가 있으면 종료 코드 1 (복사 안 함)

원본은 항상 .claude/skills 하나다. 두 도구의 SKILL.md 형식(name/description 머리글 + 본문)은 같고, 도구 이름만 일반 표현으로 바꾼다.
Codex 는 저장소의 .agents/skills 를 작업 폴더부터 저장소 루트까지 읽는다 (사용자 전체용은 ~/.codex/skills).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / ".claude" / "skills"
DST = ROOT / ".agents" / "skills"

# Claude Code 도구 이름 -> 도구 중립 표현
REPLACE = [
    ("WebSearch 로", "웹 검색으로"),
    ("(서브에이전트를 쓸 수 있으면 한 번에 세 개)", "(병렬 작업 기능이 있으면 한 번에 세 개, 없으면 순서대로)"),
    ("Edit/Write 로 채운다", "파일 편집으로 채운다"),
]


def convert(text: str) -> str:
    for a, b in REPLACE:
        text = text.replace(a, b)
    return text


def main() -> int:
    check = "--check" in sys.argv
    diff = 0
    for d in sorted(p for p in SRC.iterdir() if (p / "SKILL.md").exists()):
        want = convert((d / "SKILL.md").read_text(encoding="utf-8"))
        out = DST / d.name / "SKILL.md"
        have = out.read_text(encoding="utf-8") if out.exists() else None
        if have != want:
            diff += 1
            if not check:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(want, encoding="utf-8")
                print(f"동기화: {out.relative_to(ROOT)}")
            else:
                print(f"다름: {out.relative_to(ROOT)}")
    for d in (DST.iterdir() if DST.exists() else []):          # 원본에서 지워진 스킬은 Codex 쪽에서도 지운다
        if d.is_dir() and not (SRC / d.name / "SKILL.md").exists():
            diff += 1
            if not check:
                shutil.rmtree(d)
                print(f"삭제: {d.relative_to(ROOT)}")
            else:
                print(f"원본 없음: {d.relative_to(ROOT)}")
    if not diff:
        print("이미 동기화돼 있어요")
    return 1 if (check and diff) else 0


if __name__ == "__main__":
    sys.exit(main())
