---
name: character-prompt
description: character-pipeline 3단계. profile.yaml 과 일치하는 대화 예시, 인사, 방어/싸움 한마디, 컨셉 훅을 만들어 prompt.yaml 로 저장한다. 모델은 예시를 성격 설명보다 강하게 따라 하므로 가장 중요한 단계다.
---

# character-prompt

입력: profile.yaml. 출력: `prompt.yaml` (examples greeting example_user guard_replies fight_style fight_replies concept_strength concept_hooks web_topics lore quirks).

- `examples` 7개 안팎. 답은 1~2줄, 줄당 10~25자, **쉼표, 마침표, 따옴표, 괄호, 물결, 느낌표, 말줄임표 없음**, 시작 단어 서로 다르게.
- **격식 일치**: profile 의 speech_style 이 존댓말이면 예시도 모두 해요체/합쇼체, 반말이면 모두 반말. 섞이면 validate 가 오류로 잡는다.
- 상황 섞기: 인사, 잡담, 고민, 칭찬, 놀림, "너 AI지?"(부정도 인정도 없이 어이없어하며 화제 전환), "ㅋㅋㅋ", 모르는 질문. 컨셉 소재는 1~2개에만.
- 다른 캐릭터와 말투가 달라야 한다. 기존 캐릭터의 예시 문장과 비슷하게 쓰지 않는다 (validate 가 겹침을 경고).
- `guard_replies` 6~8개, `fight_replies` 4~6개 (각 30자 이하), `fight_style` 한 줄, `concept_hooks` 2~4개 (해킹/코드 훅에는 "실제 방법은 알려주지 않고 넘긴다").
- `web_topics` 1~4개 (검색 기능은 현재 꺼져 있어도 형식은 채운다).
- **글 스타일(style)**: 기본은 기호 없는 담백한 채팅체지만, 소심한 캐릭터는 말줄임표와 머뭇거림(`ellipsis`, `hesitate`), 순진하고 들뜬 캐릭터는 느낌표(`exclaim`), 웃지 않는 캐릭터는 `laugh: none` 을 yaml 의 `style:` 에 적는다 (형식은 `bot/textstyle.py` 맨 위). 허용한 기호는 examples 에 써도 된다. 캐릭터마다 이 스타일이 달라야 말투가 비슷해지지 않는다.
