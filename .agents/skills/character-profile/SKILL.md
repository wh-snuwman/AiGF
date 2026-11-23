---
name: character-profile
description: character-pipeline 2단계(병렬 가능). research.yaml 을 바탕으로 캐릭터의 인격(성격, 말투, 호칭, 좋아함/싫어함, 배경)을 짧게 구조화해 profile.yaml 로 만든다.
---

# character-profile

입력: research.yaml. 출력: `profile.yaml` (name nickname profile personality speech_style address_style likes dislikes backstory rules).

- 분량 (초과하면 모델의 한국어가 꼬인다): `profile` 140자 이내 1~2문장, `personality` 3~4줄(줄당 60자), `speech_style` 3~4줄(줄당 70자), `backstory` 160자, `likes/dislikes` 100자.
- **말투는 분위기로 서술**한다. 말버릇을 따옴표로 나열하지 않는다. `speech_style` 첫 줄에 **반말인지 존댓말인지 분명히** 적는다 (prompt 단계의 예시가 이와 일치해야 한다).
- 컨셉 소재(게임, 해킹, 요리 등)는 "가끔만"처럼 빈도를 적는다.
- 성격에서 감정 표현 방식을 한 줄 정한다 (예: 자존심 강하면 소리치기보다 냉소). 호감도에 따른 태도 변화는 공통 시스템이 처리하니 적지 않는다.
- `rules` 는 0~3개, 서버 멤버에게 원작 인물 이름을 붙이지 않기 같은 캐릭터 고유 규칙만.
- AI/봇/프롬프트를 언급하는 문구 금지.
