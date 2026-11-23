"""캐릭터 생성 파이프라인 (스킬들이 공통으로 쓰는 결정적 도구).

  python scripts/character_pipeline.py init <id>            작업 폴더 data/character_work/<id>/ 와 단계별 빈 파일 만들기
  python scripts/character_pipeline.py assemble <id> [--force] [--avatar 이미지경로]
                                                          단계 파일들을 합쳐 config/characters/<id>.yaml 만들기 (이미 있으면 --force 필요)
  python scripts/character_pipeline.py validate <id>        통합 검증 + 종합 보고서 (오류가 있으면 종료 코드 1)

단계 파일 (자연어 대신 구조화된 yaml 로 주고받는다. 각 하위 스킬은 자기 파일만 쓴다):
  research.yaml   조사 결과: sources[], facts{}, conflicts[]            (character-research)
  profile.yaml    name nickname profile personality speech_style address_style likes dislikes backstory rules  (character-profile)
  prompt.yaml     examples greeting example_user guard_replies fight_style fight_replies concept_hooks concept_strength web_topics lore quirks  (character-prompt)
  visual.yaml     visual: {appearance, vibe, outfits, places, scenes}    (character-visual)
  emotion.yaml    emotion_preset mood_baseline emotions talk_weight      (character-emotion)
  schedule.yaml   schedule: {places, blocks}                             (character-schedule)
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("pip install pyyaml 이 필요해요")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

WORK = ROOT / "data" / "character_work"
CHAR_DIR = ROOT / "config" / "characters"
AVATAR_DIR = CHAR_DIR / "avatars"
STAGES = ["research", "profile", "prompt", "visual", "emotion", "schedule"]
KEY_ORDER = ["name", "nickname", "profile", "personality", "speech_style", "address_style", "likes", "dislikes", "backstory", "rules",
             "mood_baseline", "greeting", "example_user", "examples", "guard_replies", "lore", "quirks", "concept_strength",
             "concept_hooks", "fight_style", "fight_replies", "talk_weight", "avatar_url", "web_topics", "emotion_preset", "emotions",
             "visual", "schedule"]
ID_RE = re.compile(r"^[a-z0-9_]{2,24}$")
FORMAL_END = re.compile(r"(요|죠|니다|니까|세요|네요|군요|어요|아요|해요|예요|에요|습니다)\s*\??\s*$")


def _represent_str(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None)


yaml.add_representer(str, _represent_str)

TEMPLATES = {
    "research": {"sources": [], "facts": {"origin": "", "world": "", "relationships": "", "speech_notes": "", "likes": "", "dislikes": ""}, "conflicts": []},
    "profile": {"name": "", "nickname": "", "profile": "", "personality": "", "speech_style": "", "address_style": "", "likes": "",
                "dislikes": "", "backstory": "", "rules": []},
    "prompt": {"example_user": "철수", "examples": [], "greeting": "", "guard_replies": [], "fight_style": "", "fight_replies": [],
               "concept_strength": 0.5, "concept_hooks": [], "web_topics": [], "lore": "", "quirks": ""},
    "visual": {"visual": {"appearance": "", "vibe": "", "outfits": {"default": ""}, "places": {}, "scenes": {}}},
    "emotion": {"emotion_preset": "default", "mood_baseline": {"mood": 0.2, "energy_bias": 0.0, "irritation": 0.1, "lonely": 0.1, "interest": 0.5}},
    "schedule": {"schedule": {"places": {}, "blocks": []}},
}


def work_dir(cid: str) -> Path:
    if not ID_RE.match(cid):
        sys.exit("id 는 영문 소문자/숫자/_ 2~24자여야 해요")
    return WORK / cid


def cmd_init(cid: str) -> None:
    d = work_dir(cid)
    d.mkdir(parents=True, exist_ok=True)
    for st in STAGES:
        p = d / f"{st}.yaml"
        if not p.exists():
            p.write_text(yaml.safe_dump(TEMPLATES[st], allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8")
    print(f"작업 폴더: {d}\n단계 파일: {', '.join(s + '.yaml' for s in STAGES)}")


def load_stage(d: Path, st: str) -> dict:
    p = d / f"{st}.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def merge(stages: dict[str, dict]) -> dict:
    """research 는 참고용이라 캐릭터 파일에 넣지 않는다. 나머지를 합치고 빈 값은 뺀 뒤 KEY_ORDER 순서로 정렬."""
    out: dict = {}
    for st in ("profile", "prompt", "emotion", "visual", "schedule"):
        for k, v in (stages.get(st) or {}).items():
            if v in ("", None, [], {}):
                continue
            out[k] = v
    ordered = {k: out[k] for k in KEY_ORDER if k in out}
    ordered.update({k: v for k, v in out.items() if k not in ordered})
    return ordered


def cmd_assemble(cid: str, force: bool, avatar: str | None) -> None:
    d = work_dir(cid)
    if not d.exists():
        sys.exit(f"먼저 init 하세요: {d}")
    target = CHAR_DIR / f"{cid}.yaml"
    if target.exists() and not force:
        sys.exit(f"{target} 이(가) 이미 있어요. 덮어쓰려면 --force (기존 파일은 {cid}.yaml.bak 로 백업됩니다)")
    data = merge({st: load_stage(d, st) for st in STAGES})
    if not data.get("name"):
        sys.exit("profile.yaml 에 name 이 필요해요")
    if target.exists():
        (CHAR_DIR / f"{cid}.yaml.bak").write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    header = f"# {data['name']} — scripts/character_pipeline.py 로 {datetime.now():%Y-%m-%d} 에 합쳐서 만든 파일\n\n"
    target.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8")
    if avatar:
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        im = Image.open(avatar).convert("RGBA")
        w, h = im.size
        s = min(w, h)
        top = 0 if h <= w else min(int(h * 0.08), h - s)
        im.crop(((w - s) // 2, top, (w - s) // 2 + s, top + s)).resize((512, 512), Image.LANCZOS).save(AVATAR_DIR / f"{cid}.png", optimize=True)
        print(f"프로필 사진 저장: {AVATAR_DIR / (cid + '.png')}")
    print(f"만들었어요: {target}")


# ---------------- 검증 ----------------
def _ex_text(d: dict) -> str:
    return " ".join(str(e.get("assistant", "")) for e in (d.get("examples") or []) if isinstance(e, dict))


def _bigrams(s: str) -> set[str]:
    t = re.sub(r"[^가-힣A-Za-z0-9]", "", s)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def register_of(d: dict) -> tuple[str, str]:
    """(말투 설명이 가리키는 격식, 예시에서 관측한 격식) — 각각 formal / informal / mixed / unknown"""
    style = str(d.get("speech_style", ""))
    declared = "informal" if "반말" in style and "존댓말" not in style.replace("존댓말은 쓰지", "").replace("존댓말을 쓰지", "") else \
        "formal" if ("존댓말" in style or "해요체" in style) and "반말" not in style else "mixed" if "반말" in style and "존댓말" in style else "unknown"
    lines = [ln.strip() for e in (d.get("examples") or []) if isinstance(e, dict) for ln in str(e.get("assistant", "")).splitlines() if ln.strip()]
    if not lines:
        return declared, "unknown"
    f = sum(1 for ln in lines if FORMAL_END.search(ln)) / len(lines)
    observed = "formal" if f >= 0.6 else "informal" if f <= 0.2 else "mixed"
    return declared, observed


def validate(cid: str) -> tuple[list[str], list[str], list[str]]:
    """(오류, 경고, 정보). 기존 check_character + 스케줄/외형/감정/말투 일관성/다른 캐릭터와의 구분."""
    import check_character as cc
    path = CHAR_DIR / f"{cid}.yaml"
    if not path.exists():
        return [f"파일이 없어요: {path}"], [], []
    errs, warns = cc.check(path)
    info: list[str] = []
    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # 스케줄: 24시간이 전부 덮이는지, 자는 시간이 있는지
    sch = d.get("schedule")
    if not sch:
        warns.append("schedule 이 없어 기본 시간표를 써요 (캐릭터마다 다른 생활 패턴을 쓰려면 character-schedule)")
    else:
        from bot.schedule import Schedule
        s = Schedule(sch)
        gaps = [h for h in range(24) if not any(b.covers(h * 60) for b in s.blocks)]
        if gaps:
            warns.append(f"schedule 이 비어 있는 시각: {gaps} (그 시간엔 첫 블록이 쓰여요)")
        info.append(f"스케줄 블록 {len(s.blocks)}개, 전용 장소 {list(s.own_places.values())}")
    # 외형 프롬프트 / 표정 30종
    vis = d.get("visual") or {}
    if not vis.get("appearance"):
        warns.append("visual.appearance 가 없어요 (이미지 프롬프트는 profile 로 대신해요)")
    from bot.mood import EMO
    common = yaml.safe_load((ROOT / "config" / "visual_common.yaml").read_text(encoding="utf-8"))
    missing = set(EMO) - set(common.get("expressions") or {})
    if missing:
        errs.append(f"visual_common.yaml 에 표정이 없는 감정: {sorted(missing)}")
    # 감정 프리셋
    presets = (yaml.safe_load((ROOT / "config" / "emotions.yaml").read_text(encoding="utf-8")).get("presets") or {})
    if d.get("emotion_preset") and d["emotion_preset"] not in presets:
        errs.append(f"emotion_preset '{d['emotion_preset']}' 가 config/emotions.yaml 에 없어요 ({sorted(presets)})")
    # 프로필 사진
    if not (AVATAR_DIR / f"{cid}.png").exists() and not d.get("avatar_url"):
        warns.append("프로필 사진이 없어요 (config/characters/avatars/<id>.png 또는 /캐릭터 동작:프로필)")
    # 말투 일관성: 설명한 격식과 예시의 격식이 같은가
    declared, observed = register_of(d)
    info.append(f"말투 격식: 설명={declared} / 예시={observed}")
    if declared in ("formal", "informal") and observed in ("formal", "informal") and declared != observed:
        errs.append(f"speech_style 은 {declared} 인데 examples 는 {observed} 로 써져 있어요 — 모델이 예시를 따라 말투가 섞여요")
    # 다른 캐릭터와 말투가 너무 비슷하지 않은가 (예시 글자쌍 겹침)
    mine = _bigrams(_ex_text(d))
    for p in sorted(CHAR_DIR.glob("*.yaml")):
        if p.stem == cid:
            continue
        other = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        theirs = _bigrams(_ex_text(other))
        if mine and theirs:
            j = len(mine & theirs) / len(mine | theirs)
            if j >= 0.3:
                warns.append(f"examples 가 {p.stem} 와 {j:.0%} 겹쳐요 — 캐릭터마다 말투가 달라야 해요")
    # 프롬프트 크기
    try:
        from bot.budget import estimate
        from bot.persona import Persona
        sysp = Persona(cid).static_prompt()
        info.append(f"고정 프롬프트 {len(sysp)}자 (약 {estimate(sysp)}토큰)")
        if len(sysp) > 1400:
            warns.append(f"고정 프롬프트가 {len(sysp)}자로 길어요 (권장 1,400자 이하)")
        if "['" in sysp or '["' in sysp:
            errs.append("프롬프트에 파이썬 목록 모양이 그대로 들어갔어요 (rules 형식 확인)")
    except Exception as e:  # noqa: BLE001
        warns.append(f"프롬프트 조립 확인 실패: {type(e).__name__}: {e}")
    return errs, warns, info


def cmd_validate(cid: str) -> int:
    errs, warns, info = validate(cid)
    print(f"== 캐릭터 검증 보고서: {cid} ==")
    for i in info:
        print(f"  · {i}")
    for e in errs:
        print(f"  ✗ {e}")
    for w in warns:
        print(f"  ! {w}")
    print(f"{'✗ 실패' if errs else '✓ 통과'}: 오류 {len(errs)} / 경고 {len(warns)}")
    return 1 if errs else 0


def main() -> None:
    a = sys.argv[1:]
    if len(a) < 2 or a[0] not in ("init", "assemble", "validate"):
        sys.exit(__doc__)
    cmd, cid = a[0], a[1]
    if cmd == "init":
        cmd_init(cid)
    elif cmd == "assemble":
        av = a[a.index("--avatar") + 1] if "--avatar" in a else None
        cmd_assemble(cid, "--force" in a, av)
    else:
        sys.exit(cmd_validate(cid))


if __name__ == "__main__":
    main()
