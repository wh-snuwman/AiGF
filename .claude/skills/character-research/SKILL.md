---
name: character-research
description: character-pipeline 1단계. 새 캐릭터의 공식 설정, 세계관, 관계, 말투 특징을 조사해 research.yaml 로 정리한다. 원작이 없으면 사용자 설명만 정리한다.
---

# character-research

입력: 캐릭터 이름/출처/사용자 설명. 출력: `data/character_work/<id>/research.yaml` (`sources`, `facts`, `conflicts`).

1. 신뢰할 수 있는 출처 우선 (공식 사이트, 위키, 팬 정리 순). WebSearch 로 설정/성격/말투를 찾고, 접속이 막힌 페이지는 재시도하지 않는다.
2. `facts` 에 출신/세계관/역할/관계/말투 특징(반말 또는 존댓말, 호칭, 감탄사, 말 많은 정도)/좋아함/싫어함을 **한 줄씩** 적는다. 대사 원문은 길게 옮기지 않는다.
3. 출처마다 설정이 다르면 `conflicts` 에 적는다 (예: 소속, 눈 색). 충돌이 중대하면 메인 스킬이 사용자에게 한 번만 묻는다.
4. 확인하지 못한 항목은 `(확인 못함)` 이라고 적는다. 추측으로 채우지 않는다.
