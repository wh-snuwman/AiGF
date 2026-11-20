"""새 캐릭터 템플릿 생성:  python scripts/new_character.py <id> "<표시 이름>" [프리셋]
예) python scripts/new_character.py haru "하루" gentle
config/characters/<id>.yaml 이 만들어진다. 채워 넣고 `/상태 동작:리로드` 하면 재시작 없이 반영된다.
프리셋: config/emotions.yaml 의 presets 키 (default / gentle / tsundere / energetic / cool)
"""
import re
import sys
from pathlib import Path

TEMPLATE = '''name: "{name}"
nickname: "{name}"
profile: "한두 문장으로 캐릭터 소개"
personality: "성격 (한 줄)"
speech_style: "말투 (반말/존댓말, 자주 쓰는 어미, 금지할 말투)"
address_style: "상대를 어떻게 부르는지"
likes: ""
dislikes: ""
backstory: "배경 한 줄"
rules:
  - "이 캐릭터에만 필요한 추가 규칙 (없으면 지워도 됨)"
mood_baseline: {{mood: 0.2, energy_bias: 0.0, irritation: 0.1, lonely: 0.1, interest: 0.5}}
greeting: "캐릭터 첫 인사"

# 감정 표현: 프리셋 한 줄 + 바꾸고 싶은 감정만 덮어쓰기 (config/emotions.yaml 참고)
emotion_preset: {preset}
# emotions:
#   scale: 1.0
#   tones:
#     angry: "화났을 때 이 캐릭터다운 말투"
#   expr:
#     joy: {{rate: 0.4, marks: ["ㅎㅎ", "^^"], cat: ok, excl: 0.1, stretch: false}}

# few-shot 예시: 6개 이상 쓰면 settings 의 prompt.example_rotate_min 마다 다른 조합으로 돌려 써서 말버릇 고착을 막는다
example_user: "유저"
examples:
  - user: "안녕"
    assistant: "왔어?"

guard_replies:            # 명령/주입을 넘길 때 쓰는 캐릭터 목소리 문구 (공통 거절 방식과 섞임)
  - "그런 건 안 해"
fight_style: "싸울 때의 말투/태도"
fight_replies:
  - "그만하자"
concept_strength: 0.5
concept_hooks: []
web_topics: ["{name} 최신 소식"]   # 가끔 인터넷에서 미리 받아 둘 검색어 (최대 4개)
'''


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cid, name = sys.argv[1], sys.argv[2]
    preset = sys.argv[3] if len(sys.argv) > 3 else "default"
    if not re.fullmatch(r"[a-z0-9_]{2,24}", cid):
        sys.exit("id 는 영문 소문자/숫자/_ 2~24자로 해주세요.")
    path = Path(__file__).resolve().parent.parent / "config" / "characters" / f"{cid}.yaml"
    if path.exists():
        sys.exit(f"이미 있어요: {path}")
    path.write_text(TEMPLATE.format(name=name, preset=preset), encoding="utf-8")
    print(f"만들었어요: {path}")


if __name__ == "__main__":
    main()
