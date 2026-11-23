"""캐릭터 이미지 프롬프트 만들기:  python scripts/image_prompt.py <캐릭터id> [--emotion joy] [--place hideout] [--scene game] [--outfit home] [--hour 23]
현재 감정/스케줄 장소에 맞춰 이미지 생성 모델에 넣을 태그 문자열을 출력한다 (positive / negative)."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import load_yaml  # noqa: E402
from bot.visual import build  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("char")
    ap.add_argument("--emotion", default="calm")
    ap.add_argument("--place")
    ap.add_argument("--scene")
    ap.add_argument("--outfit")
    ap.add_argument("--hour", type=int)
    a = ap.parse_args()
    d = load_yaml(f"characters/{a.char}.yaml")
    if not d.get("visual"):
        print(f"! {a.char}: yaml 에 visual: 이 없어 profile 로 대신합니다")
    pos, neg = build(d, a.emotion, a.place, a.scene, a.outfit, a.hour)
    print("positive:", pos)
    print("negative:", neg)


if __name__ == "__main__":
    main()
