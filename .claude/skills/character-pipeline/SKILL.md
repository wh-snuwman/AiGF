---
name: character-pipeline
description: 새 캐릭터를 처음부터 끝까지 만드는 메인 스킬. "캐릭터 추가", "새 캐릭터 만들어줘", 원작 캐릭터를 봇에 넣기, 인격/감정/스케줄/외형까지 한 번에 만들 때 사용한다. 조사, 프로필, 프롬프트, 외형, 감정, 스케줄, 통합, 검증 순서로 하위 스킬을 조정한다. 파일 하나만 빠르게 고치는 일은 character-writer 를 쓴다.
---

# character-pipeline (메인)

하위 스킬을 순서대로 실행하고, **마지막에 통합 보고서 하나만** 사용자에게 낸다. 중간 보고는 하지 않는다.

## 순서와 병렬화
1. `python scripts/character_pipeline.py init <id>` (id: 영문 소문자/숫자/_ 2~24자, 이미 있으면 덮어쓰지 말고 질문)
2. **character-research** -> `data/character_work/<id>/research.yaml` (먼저, 혼자)
3. 아래 셋은 research.yaml 만 읽으므로 **서로 독립이라 병렬로 실행**해도 된다 (서브에이전트를 쓸 수 있으면 한 번에 세 개):
   - **character-profile** -> profile.yaml
   - **character-visual** -> visual.yaml
   - **character-schedule** -> schedule.yaml
4. **character-prompt** -> prompt.yaml (profile.yaml 이 끝난 뒤: 말투와 예시가 일치해야 함)
5. **character-emotion** -> emotion.yaml (profile.yaml 의 성격을 보고 프리셋/기분 기준값 결정)
6. **character-integration**: `assemble` 로 합치기 (+ 프로필 사진)
7. **character-validation**: `validate` 로 검증, 오류가 있으면 해당 단계 파일만 고쳐 다시 assemble, validate (최대 3회)

## 원칙
- 단계 사이에는 자연어 설명이 아니라 **yaml 파일**로만 넘긴다. 각 스킬은 자기 파일만 쓴다.
- 공통 정보를 중복 생성하지 않는다: 외형은 visual.yaml 에만, 장소는 schedule.yaml 의 places 에만, 공통 규칙은 config/system_prompt.txt 에 이미 있다.
- 질문은 **중대한 설정 충돌이나 정보 부족**이 있을 때만, 한 번에 묶어서 한다 (research.yaml 의 conflicts 가 비어 있으면 묻지 않는다).
- 원작 대사/가사는 인용하지 않는다. 실존 인물 사칭, 성적/혐오/폭력 내용 금지.

## 최종 보고서 (한 번만, 짧게)
캐릭터 id/이름, 핵심 성격 한 줄, 말투 격식, 가정한 점, validate 결과(오류/경고), 적용 방법:
서버에서 `git pull` 후 `/캐릭터 동작:부르기 캐릭터:<이름>`, 사진은 `/캐릭터 동작:프로필`. 커밋/푸시는 사용자가 원할 때만.
