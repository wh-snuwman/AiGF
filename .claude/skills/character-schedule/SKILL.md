---
name: character-schedule
description: character-pipeline 6단계(병렬 가능). 캐릭터의 가상 생활 시간표와 전용 장소를 세계관에 맞게 만들어 schedule.yaml 로 저장한다.
---

# character-schedule

입력: research.yaml. 출력: `schedule.yaml` 의 `schedule:` 블록 (`places`, `blocks`). 형식은 `bot/schedule.py` 맨 위 설명 참고.

- 블록: `{at: "HH:MM-HH:MM", do: 활동, place: 장소id, sleep, energy(-0.5~0.5), chat(0.05~2.0 대답 빈도 배수), length(0.3~1.8 답변 길이 배수), days: all|weekday|weekend}`. 자정을 넘기는 구간도 된다.
- **24시간이 빈틈없이** 덮이게 하고, 자는 블록(`sleep: true`) 하나는 반드시 넣는다. 학생이나 직장인이면 평일과 주말 블록을 나눈다.
- 장소: 공통(home school office store street park)은 그대로 쓰고, **세계관에 맞는 전용 장소만** `places` 에 추가한다 (신사, 작업실 등). 세계관에 안 맞는 장소를 억지로 넣지 않는다.
- **다른 캐릭터와 다른 패턴**을 쓴다 (올빼미형, 아침형 등 성격과 직업에서 도출). 활동 문구는 프롬프트에 그대로 들어가니 20자 안팎으로 짧게.
