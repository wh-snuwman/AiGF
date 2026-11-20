"""능동 대화 엔진: 아무도 말을 걸지 않아도 스스로 상황을 분석해서 먼저 말할지 판단한다.

판단 재료(종합): 침묵 시간, 대화 속도 감소(최근 3분 vs 직전 10분), 채팅 비율 부족, 답을 못 받은 사람,
랜덤 쿨타임, 시간대, 시간당 횟수 한도, 봇이 먼저 말했는데 답이 없었던 횟수.
LLM 은 부르지 않는 순수 계산이라 빠르다. (실제 문장은 engine.initiate 가 만든다.)
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class Snapshot:
    silence: float = 0.0                 # 마지막 사람 메시지 이후 초
    msgs_3m: int = 0
    msgs_prev: int = 0                   # 3분~13분 전 메시지 수
    users_3m: int = 0
    since_bot: float = 9999.0            # 봇이 마지막으로 말한 지 초
    last_init_age: float = 9999.0        # 봇이 마지막으로 먼저 말한 지 초
    unanswered_inits: int = 0            # 먼저 말했는데 아직 사람이 답하지 않은 횟수
    inits_hour: int = 0                  # 최근 1시간 먼저 말한 횟수
    deficit: float = 0.0                 # 비율 부족(+) / 과다(-)
    hour: int = 12
    gap_needed: float = 90.0             # 이번 쿨타임(랜덤)
    unaddressed: list = field(default_factory=list)   # [(uid, name, text, age_sec)]
    last_speaker: str = ""
    ratio: float = 0.0
    target: float = 0.0


@dataclass
class Decision:
    fire: bool = False
    kind: str = ""
    target: tuple | None = None          # (uid, name, text)
    reason: str = ""
    p: float = 0.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def decide(snap: Snapshot, cfg: dict, engage: float = 1.0, init_mult: float = 1.0,
           rng: random.Random | None = None) -> Decision:
    rng = rng or random
    lo_h, hi_h = cfg.get("hours", [10, 24])
    if not cfg.get("enabled", True) or not (lo_h <= snap.hour < hi_h):
        return Decision(reason="비활성/시간대 밖")
    if snap.silence >= cfg.get("idle_handoff_sec", 900):
        return Decision(reason="장시간 침묵(혼잣말 이벤트 담당)")
    if snap.unanswered_inits >= cfg.get("max_unanswered", 2):
        return Decision(reason="먼저 한 말에 답이 없어 대기")
    if snap.inits_hour >= cfg.get("max_per_hour", 14) * max(1.0, engage) ** 0.5:
        return Decision(reason="시간당 한도")
    if snap.last_init_age < snap.gap_needed or snap.since_bot < min(snap.gap_needed * 0.5, 40):
        return Decision(reason="쿨타임")

    min_sil = cfg.get("min_silence_sec", 35)
    s1 = _clamp((snap.silence - min_sil) / 270.0, 0.0, 1.0) if snap.silence >= min_sil else 0.0       # 침묵
    expected = snap.msgs_prev * (3.0 / 10.0)
    s2 = _clamp(1.0 - snap.msgs_3m / max(1.0, expected), 0.0, 1.0) if (snap.msgs_prev >= 5 and snap.msgs_3m < 0.5 * expected) else 0.0  # 식어감
    s3 = _clamp(snap.deficit / 0.6, 0.0, 1.0)                                                          # 비율 부족
    waiting = [u for u in snap.unaddressed if u[3] >= 40]
    s4 = 1.0 if waiting else 0.0                                                                       # 답 못 받은 사람

    p = 1 - (1 - 0.5 * s1) * (1 - 0.55 * s2) * (1 - 0.45 * s3) * (1 - 0.6 * s4)
    p *= max(0.0, init_mult)
    if snap.silence < min_sil and s4 == 0:
        p *= 0.3                                    # 대화가 한창일 땐 끼어들기를 드물게
    if snap.silence < min_sil and s3 > 0 and s4 == 0:
        p *= 0.5
    p_tick = min(0.5, p * 0.15 * max(0.5, engage) ** 0.5)
    parts = f"침묵{s1:.2f} 식어감{s2:.2f} 비율{s3:.2f} 미응답{s4:.0f}"
    if rng.random() >= p_tick:
        return Decision(p=p_tick, reason=parts)

    if s4 and rng.random() < 0.75:
        uid, name, text, _ = max(waiting, key=lambda u: u[3])        # 가장 오래 기다린 사람부터
        return Decision(True, "ask_user", (uid, name, text), parts, p_tick)
    if s2 > 0 and s2 >= s1:
        return Decision(True, "follow_topic", None, parts, p_tick)
    if s1 >= 0.45:
        return Decision(True, "new_topic", None, parts, p_tick)
    return Decision(True, "chime", None, parts, p_tick)


INSTRUCTIONS = {
    "ask_user": '아까 {name}의 말 "{text}"에 아직 아무도 반응하지 않았다. 네가 그 사람에게 먼저 반응하거나 궁금한 걸 되물어라.',
    "follow_topic": "대화가 식어 간다. 직전 이야기의 맥락을 파악해서 거기서 이어지는 궁금한 점을 묻거나 네 의견·경험을 한마디 덧붙여 대화를 살려라.",
    "new_topic": "한동안 조용하다. 기억하는 사람 얘기나 지금 시각, 심심함에서 자연스러운 새 화제를 던져라.",
    "chime": "대화에 자연스럽게 한마디 끼어들어라. 방금 얘기에 대한 의견, 농담, 리액션.",
}


def instruction(d: Decision) -> str:
    base = INSTRUCTIONS.get(d.kind, INSTRUCTIONS["chime"])
    if d.target:
        return base.format(name=d.target[1], text=str(d.target[2])[:40])
    return base


def analysis_text(s: Snapshot) -> str:
    """프롬프트에 넣을 상황 분석 한 줄 (코드가 계산한 사실만)"""
    pace = "빠름" if s.msgs_3m >= 8 else "보통" if s.msgs_3m >= 3 else "느림"
    fade = " (식어가는 중)" if s.msgs_prev >= 5 and s.msgs_3m < 0.5 * s.msgs_prev * 0.3 else ""
    waiting = ", ".join(f"{u[1]}({int(u[3])}초)" for u in s.unaddressed[:3]) or "없음"
    last = f"{s.last_speaker}({int(s.silence)}초 전)" if s.last_speaker else f"{int(s.silence)}초 전"
    return (f"분석:최근3분 {s.msgs_3m}개/{s.users_3m}명, 속도 {pace}{fade}, 마지막 발언 {last}, "
            f"답 못 받은 사람:{waiting}, 봇비율 {s.ratio:.2f}/목표 {s.target:.2f}")
