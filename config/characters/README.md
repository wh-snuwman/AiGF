# config/characters

캐릭터 한 명 = yaml 파일 하나. 파일 이름(확장자 제외)이 캐릭터 id 입니다 (`eunlang.yaml` → `eunlang`).

## 빠르게 만들기
```bash
python scripts/new_character.py <id> "<표시 이름>" [프리셋]   # 템플릿 생성
python scripts/check_character.py <id>                       # 작성 후 검사 (✗ 오류는 필수, ! 경고는 권장)
```
디스코드에서: `/캐릭터 동작:부르기 캐릭터:<이름>` (함께 활동) 또는 `/캐릭터스왑 캐릭터:<이름>` (교체). 파일을 고친 뒤에는 `/상태 동작:리로드`.

## 다른 모델에게 작성시키기
[`docs/캐릭터_작성_가이드.md`](../../docs/캐릭터_작성_가이드.md) 맨 아래의 "모델에게 줄 지시문"을 그대로 복사해서 캐릭터 설명만 채워 건네세요.
Claude Code 를 쓴다면 `character-writer` 스킬(`/character-writer 하루: 다정한 …`)이 생성·검사·적용까지 합니다.

## 핵심 요약
- 짧게: profile 1~2문장, personality/speech_style 3~4줄(줄당 한 문장).
- 말버릇을 따옴표로 나열 금지 (모델이 그대로 반복). 분위기로 서술.
- examples 6~8개, 답은 1~2줄·줄당 10~25자·기호 없음·시작 단어 다양하게.
- 컨셉 소재는 가끔만 (빈도를 글로 적기).
- 선택 항목: `avatar_url`(웹훅 아바타), `talk_weight`(말 많은 정도), `emotion_preset`, `concept_hooks`, `web_topics`.
