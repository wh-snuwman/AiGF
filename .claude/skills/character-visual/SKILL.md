---
name: character-visual
description: character-pipeline 4단계(병렬 가능). 캐릭터의 외형, 분위기, 의상, 전용 장소, 장면 이미지 프롬프트를 visual.yaml 로 만든다. 성격과 모순되지 않게 쓴다.
---

# character-visual

입력: research.yaml (외형 정보). 출력: `visual.yaml` 의 `visual:` 블록.

- 영어 태그를 쉼표로 나열한다. 공통 품질/스타일, 감정 표정 30종, 공통 장소는 `config/visual_common.yaml` 에 이미 있으니 **중복해서 쓰지 않는다**.
- `appearance`: 머리, 눈, 체형, 상징적 소품 (한 번만 적는 기본 외형). `vibe`: 성격이 드러나는 자세와 분위기 (예: 츤데레면 arms crossed, slight blush).
- `outfits`: default 와 필요한 의상 1~2개. `places`: 이 캐릭터 세계관에만 있는 장소 (공통 장소 home school office store street park 는 쓰지 않는다). `scenes`: 이야기나 취미 장면 2~4개.
- 확인: assemble 후 `python scripts/image_prompt.py <id> --emotion smug --scene <장면>`.
