---
name: character-integration
description: character-pipeline 7단계. 단계별 yaml 을 config/characters/<id>.yaml 로 합치고 프로필 사진을 등록한다. 코드로 결정적으로 합치므로 내용을 새로 쓰지 않는다.
---

# character-integration

1. `python scripts/character_pipeline.py assemble <id> [--avatar 이미지경로]` 로 빈 값은 빼고 정해진 순서로 합친다. 이미 있는 캐릭터는 `--force` 가 있어야 덮어쓰고 기존 파일은 `.bak` 로 백업된다 (사용자 확인 없이 `--force` 금지).
2. 프로필 사진이 주어졌으면 `--avatar` 로 얼굴 위치를 맞춘 512px 정사각형 PNG (`config/characters/avatars/<id>.png`)를 만든다. 사진이 없으면 건너뛰고 보고서에 적는다.
3. 서버 적용 방법은 보고서에만 안내한다. 커밋과 푸시는 사용자가 원할 때만 한다.
