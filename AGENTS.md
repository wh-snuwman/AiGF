# AGENTS.md (Codex 등 코딩 에이전트용 프로젝트 안내)

Ollama(gemma3:12b)로 돌아가는 디스코드 캐릭터 챗봇. 한국어 프로젝트이므로 **답변과 주석은 한국어**로, 짧고 핵심만 쓴다.

## 구조
- `bot/` 봇 코드 (`main.py` 진입점, `chat.py` 응답 생성, `mood.py` 감정/호감도, `schedule.py` 가상 생활, `corrector.py` 문법 교정 모델, `stats.py` 통계/그래프 등)
- `config/` 설정 (`settings.yaml`, `system_prompt.txt` 공통 시스템 프롬프트, `characters/<id>.yaml` 캐릭터, `visual_common.yaml`)
- `scripts/` 도구 (`bench.py` 서버 벤치마크, `character_pipeline.py` 캐릭터 생성/검증, `check_character.py`, `image_prompt.py`)
- `tests/` 단위 테스트, `docs/` 계획서와 가이드, `README.md` 기능 설명(변경하면 같이 갱신)

## 명령
- 테스트: `python -m unittest discover -s tests` (aiosqlite, discord.py, ollama, pyyaml, matplotlib 이 없으면 일부는 건너뜀: `pip install -r requirements.txt`)
- 실행(서버): `bash run.sh`, Ollama: `bash scripts/run_ollama.sh` (메인 + 교정 모델 동시 상주를 위해 `OLLAMA_MAX_LOADED_MODELS=2`)
- 캐릭터 검증: `python scripts/character_pipeline.py validate <id>`

## 규칙
- 기존 코드 스타일과 구조를 따르고 요청 범위 밖은 리팩터링하지 않는다. 기존 기능과 저장 데이터(`data/`)와의 호환을 지킨다.
- `.env`, 토큰, API 키는 읽거나 출력하거나 커밋하지 않는다. `data/` 는 git 에 올리지 않는다.
- 프롬프트는 길수록 응답이 느려지고 말이 꼬인다. 캐릭터 설명은 짧게, 말버릇을 따옴표로 나열하지 않는다.
- 변경 뒤에는 테스트를 돌리고, 돌려 보지 못한 것(서버 GPU, 실제 디스코드)은 성공했다고 쓰지 않는다.
- 커밋/푸시는 사용자가 요청했을 때만 한다.

## 스킬 (`.agents/skills/`)
새 캐릭터를 만들 때는 `character-pipeline` 스킬을 쓴다 (조사 → 프로필 → 대화 예시 → 외형 → 감정 → 스케줄 → 통합 → 검증, 각 단계는 하위 스킬 `character-research`, `character-profile`, `character-prompt`, `character-visual`, `character-emotion`, `character-schedule`, `character-integration`, `character-validation`). 파일 하나만 고칠 때는 `character-writer`.
원본은 `.claude/skills/` 이고, 고친 뒤에는 `python scripts/sync_codex_skills.py` 로 `.agents/skills/` 에 동기화한다 (`--check` 로 차이만 확인).
