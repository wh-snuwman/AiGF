---
name: character-validation
description: character-pipeline 8단계. 완성된 캐릭터를 통합 검증한다 (yaml 형식, 분량, 말투 격식 일치, 다른 캐릭터와 말투 겹침, 스케줄 24시간, 감정 프리셋, 프롬프트 크기, 프로필 사진).
---

# character-validation

1. `python scripts/character_pipeline.py validate <id>` 를 실행한다. 오류(✗)는 반드시, 경고(!)는 가능한 한 고친다. 종료 코드 1 이면 실패다.
2. 고칠 때는 `character_work/<id>/` 의 해당 **단계 파일만** 수정하고 assemble(방금 만든 캐릭터에 한해 `--force`) 후 validate 를 반복한다 (최대 3회).
3. 자주 나오는 오류: 말투 격식 불일치 (profile 의 speech_style 과 prompt 의 examples), 예시의 기호, 다른 캐릭터와 예시 겹침, 스케줄 빈 시간.
4. 통과하면 `python scripts/image_prompt.py <id> --emotion calm` 으로 외형 프롬프트가 성격과 모순되지 않는지 한 번 확인한다.
5. 결과는 메인 스킬의 최종 보고서에 한 줄로 넣는다.
