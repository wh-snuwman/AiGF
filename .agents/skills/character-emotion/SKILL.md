---
name: character-emotion
description: character-pipeline 5단계. 캐릭터 성격에 맞는 감정 표현 프리셋, 기분 기준값, 말 많은 정도를 emotion.yaml 로 정한다.
---

# character-emotion

입력: profile.yaml. 출력: `emotion.yaml` (emotion_preset, mood_baseline, 필요하면 emotions 덮어쓰기, talk_weight).

- `emotion_preset`: default / gentle(다정, 화나도 조용) / tsundere(쑥스러우면 틱틱) / energetic(텐션 높음) / cool(건조, 이모티콘 적음). 목록은 `config/emotions.yaml`.
- `mood_baseline`: `{mood, energy_bias, irritation, lonely, interest}`. 밝으면 mood 0.5~0.7, 무심하거나 냉소적이면 0.1~0.2, 발끈하면 irritation 0.15~0.25. **극단값(±0.9 등) 금지**.
- 감정 30종은 코드의 상태 엔진이 계산하므로 30개를 직접 쓰지 않는다. 특정 감정만 캐릭터답게 바꿀 때 `emotions: {tones: {angry: "한 줄"}}` 로 덮어쓴다.
- `talk_weight`: 여러 캐릭터 중 말 많은 정도 (기본 1.0, 0.5~2.0).
- 호감도 단계별 태도는 공통 시스템(0~100, 7단계)이 처리한다. 캐릭터 고유의 차이가 필요하면 profile 의 personality 에 한 줄만 추가.
