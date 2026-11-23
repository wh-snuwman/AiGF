"""프롬프트 크기 리포트:  python scripts/prompt_report.py
캐릭터마다 고정 프롬프트(system)와 예시의 글자 수/토큰 어림값, 대화 10개를 붙인 전형적인 요청의 크기를 보여 준다.
설정을 바꾸기 전후(압축 효과)를 비교할 때 쓴다. 토큰은 bot/budget.py 의 어림 계수(실측 보정 전엔 넉넉한 기본값)로 계산한다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.budget import calibration, estimate, messages_tokens  # noqa: E402
from bot.config import load_settings  # noqa: E402
from bot.persona import Persona  # noqa: E402


def main() -> None:
    s = load_settings()
    pc = s.get("prompt", {})
    budget = int(pc.get("budget_tokens", 0) or 0)
    print(f"토큰 계수(글자당): {calibration()}   예산: {budget or '꺼짐'}   num_ctx: {s['ollama']['options'].get('num_ctx')}")
    print(f"{'캐릭터':<24}{'system':>8}{'(토큰)':>8}{'예시':>8}{'(토큰)':>8}{'전형 요청(토큰)':>16}")
    hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": "유저1: 오늘 뭐 했어 나는 게임하고 밥 먹었어" if i % 2 == 0 else "게임했지 뭐"}
            for i in range(10)]
    dyn = "[지금] 11/23(월) 저녁 20:00\n참여:철수(4)\n활동:집에서 쉬는 중\n기분:평온(약)\n말투:차분하게\n대상:철수 에 자연스럽게 답해라.\n길이:[한 줄]\n최근한말(반복금지):응 | 그래"
    for cid, name in Persona.available():
        p = Persona(cid)
        sysp = p.static_prompt()
        ex = p.example_messages(int(pc.get("max_examples", 3)))
        exc = sum(len(m["content"]) for m in ex)
        typical = estimate(sysp) + messages_tokens(ex) + messages_tokens(hist) + estimate(dyn)
        print(f"{name + '(' + cid + ')':<24}{len(sysp):>8}{estimate(sysp):>8}{exc:>8}{messages_tokens(ex):>8}{typical:>16}")


if __name__ == "__main__":
    main()
