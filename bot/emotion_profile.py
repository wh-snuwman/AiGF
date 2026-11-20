"""캐릭터별 감정 표현 프로필 (말투 지시문 + 이모티콘/표현 설정).

감정 30개의 기본값은 코드(mood.py)에 있고, 캐릭터마다 다르게 하고 싶은 부분만 덮어쓴다.
적용 순서: 코드 기본값 → config/emotions.yaml 의 프리셋(캐릭터의 emotion_preset) → 캐릭터 yaml 의 emotions.
그래서 새 캐릭터를 추가할 때는 `emotion_preset: gentle` 한 줄 + 바꾸고 싶은 감정만 적으면 된다.

캐릭터 yaml 예:
    emotion_preset: gentle          # default / gentle / tsundere / energetic / cool (config/emotions.yaml)
    emotions:
      scale: 0.9                    # 이모티콘/표현 강도 배수 (프리셋 값에 곱해짐)
      tones:
        angry: "화났지만 목소리를 높이지 않고 단호하게 짚는다."
      expr:
        joy: {rate: 0.4, marks: ["ㅎㅎ", "^^"], cat: ok, excl: 0.1, stretch: false}
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

EXPR_FIELDS = {"rate", "marks", "cat", "excl", "stretch"}


@dataclass
class EmotionProfile:
    preset: str = "default"
    scale: float = 1.0                       # 표현 강도 배수 (settings expression.scale 에 곱해짐)
    tones: dict[str, str] = field(default_factory=dict)
    expr: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def tone(self, emo_id: str, default: str = "") -> str:
        return self.tones.get(emo_id, default)


def _apply_expr(base: Any, patch: Any, emo: str, where: str, warnings: list[str]) -> Any:
    if not isinstance(patch, dict):
        warnings.append(f"{where}: expr.{emo} 는 {{rate: .., marks: [..]}} 형태여야 해요")
        return base
    kw: dict[str, Any] = {}
    for k, v in patch.items():
        if k not in EXPR_FIELDS:
            warnings.append(f"{where}: expr.{emo}.{k} 는 모르는 항목이에요 (가능: {', '.join(sorted(EXPR_FIELDS))})")
            continue
        try:
            if k in ("rate", "excl"):
                kw[k] = max(0.0, min(1.0, float(v)))
            elif k == "marks":
                kw[k] = tuple(str(x) for x in (v or []))
            elif k == "stretch":
                kw[k] = bool(v)
            else:
                kw[k] = (str(v) if v else None)
        except (TypeError, ValueError):
            warnings.append(f"{where}: expr.{emo}.{k} 값이 올바르지 않아요: {v!r}")
    return replace(base, **kw) if kw else base


def build_profile(char_data: dict, presets: dict, base_tones: dict[str, str], base_expr: dict[str, Any],
                  char_id: str = "") -> EmotionProfile:
    """기본값 + 프리셋 + 캐릭터 설정을 합친 프로필을 만든다. 잘못된 항목은 무시하고 warnings 에 모은다."""
    prof = EmotionProfile(tones=dict(base_tones), expr=dict(base_expr))
    name = str(char_data.get("emotion_preset") or "default")
    if name not in presets and name != "default":
        prof.warnings.append(f"{char_id}: emotion_preset '{name}' 이(가) config/emotions.yaml 에 없어요 (있는 것: {', '.join(presets) or '-'})")
        name = "default"
    prof.preset = name
    layers = [(f"프리셋 {name}", presets.get(name) or {}), (f"{char_id} emotions", char_data.get("emotions") or {})]
    for where, layer in layers:
        if not isinstance(layer, dict):
            prof.warnings.append(f"{where}: 딕셔너리 형태여야 해요")
            continue
        for k in layer:
            if k not in ("scale", "tones", "expr"):
                prof.warnings.append(f"{where}: '{k}' 는 모르는 항목이에요 (가능: scale, tones, expr)")
        if "scale" in layer:
            try:
                prof.scale *= max(0.0, min(3.0, float(layer["scale"])))
            except (TypeError, ValueError):
                prof.warnings.append(f"{where}: scale 은 숫자여야 해요")
        for emo, text in (layer.get("tones") or {}).items():
            if emo not in base_tones:
                prof.warnings.append(f"{where}: tones.{emo} 는 없는 감정이에요")
            else:
                prof.tones[emo] = str(text).strip()
        for emo, patch in (layer.get("expr") or {}).items():
            if emo not in base_expr:
                prof.warnings.append(f"{where}: expr.{emo} 는 없는 감정이에요")
            else:
                prof.expr[emo] = _apply_expr(prof.expr[emo], patch, emo, where, prof.warnings)
    return prof
