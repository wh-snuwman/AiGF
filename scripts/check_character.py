"""캐릭터 yaml 검사:  python scripts/check_character.py [id ...]   (생략하면 config/characters/*.yaml 전부)
다른 모델/사람이 쓴 캐릭터 파일이 봇에서 잘 돌아갈지 미리 점검한다. 오류(✗)는 고쳐야 하고, 경고(!)는 권장 사항이다.
"""
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("pip install pyyaml 이 필요해요 (봇 requirements 에 들어 있음)")

ROOT = Path(__file__).resolve().parent.parent
CHAR_DIR = ROOT / "config" / "characters"
SYMBOLS = re.compile(r"[,.!~…\"'“”‘’()（）\[\]*_`<>]|\.\.")
PRESETS_FILE = ROOT / "config" / "emotions.yaml"
REQUIRED = ["name", "profile", "personality", "speech_style", "examples"]
LIMITS = {"profile": 140, "personality": 400, "speech_style": 400, "likes": 100, "dislikes": 100, "backstory": 160, "greeting": 80}


def flat(v) -> str:
    return " ".join(str(v or "").split())


def _style(d: dict):
    sys.path.insert(0, str(ROOT))
    from bot.textstyle import from_data
    return from_data(d.get("style"))


def _symbols(d: dict) -> "re.Pattern":
    """캐릭터 style: 이 허용한 기호(느낌표/물결)는 경고하지 않는다."""
    st = _style(d)
    chars = ",.~…\"'“”‘’()（）[]*_`<>"
    chars = chars + ("" if st.exclaim else "!")
    chars = chars.replace("~", "") if st.tilde else chars
    return re.compile("[" + re.escape(chars) + "]" + ("" if st.ellipsis else r"|\.\."))


def _allowed(ln: str, d: dict) -> str:
    """말줄임표를 허용하는 캐릭터는 '...' 를 지우고 검사한다."""
    return ln.replace("...", "").replace("…", "") if _style(d).ellipsis else ln


def check(path: Path) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []
    try:
        d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        return [f"yaml 문법 오류: {e}"], []
    for k in REQUIRED:
        if not d.get(k):
            errs.append(f"필수 항목 없음: {k}")
    for k, n in LIMITS.items():
        if len(flat(d.get(k))) > n:
            warns.append(f"{k} 가 길어요 ({len(flat(d.get(k)))}자 > {n}자) — 프롬프트가 무거워지면 말이 꼬여요")
    if d.get("mood_baseline"):
        mb = d["mood_baseline"]
        bad = [k for k in ("mood", "energy_bias", "irritation", "lonely", "interest") if k not in mb]
        if bad:
            warns.append(f"mood_baseline 에 없는 키: {bad}")
        if not (-1 <= float(mb.get("mood", 0)) <= 1):
            errs.append("mood_baseline.mood 는 -1~1")
    preset = d.get("emotion_preset")
    if preset:
        try:
            presets = (yaml.safe_load(PRESETS_FILE.read_text(encoding="utf-8")) or {}).get("presets", {})
            if preset not in presets:
                errs.append(f"emotion_preset '{preset}' 없음 (선택: {', '.join(presets)})")
        except Exception:  # noqa: BLE001
            pass
    exs = d.get("examples") or []
    if len(exs) < 4:
        warns.append(f"examples 가 {len(exs)}개 — 6개 이상 권장 (돌려 쓰면 말버릇 고착이 줄어요)")
    firsts: list[str] = []
    for i, ex in enumerate(exs, 1):
        if not isinstance(ex, dict) or "user" not in ex or "assistant" not in ex:
            errs.append(f"examples[{i}] 는 user/assistant 가 필요해요")
            continue
        a = str(ex["assistant"]).strip()
        for ln in a.splitlines():
            if len(ln.strip()) > 45:
                warns.append(f"examples[{i}] 답이 길어요(45자 초과): {ln.strip()[:20]}…")
            if _symbols(d).search(_allowed(ln, d)):
                warns.append(f"examples[{i}] 답에 기호(, . ! ~ … 따옴표 괄호)가 있어요 — 모델이 따라 해요: {ln.strip()[:24]}")
            if re.search(r";;|ㅡㅡ", ln):
                warns.append(f"examples[{i}] 답에 ;; ㅡㅡ 가 있어요")
        if len(a.splitlines()) > 3:
            warns.append(f"examples[{i}] 답이 {len(a.splitlines())}줄 — 3줄 이하 권장")
        firsts.append(re.sub(r"\W", "", a.splitlines()[0])[:2] if a else "")
    if firsts and max(firsts.count(f) for f in set(firsts)) >= max(3, len(firsts) // 2 + 1):
        warns.append(f"examples 답의 시작 글자가 비슷해요({max(set(firsts), key=firsts.count)}…) — 정형 말버릇이 생겨요")
    blob = flat(d.get("personality")) + flat(d.get("speech_style"))
    quoted = re.findall(r"[\"“]([^\"”]{1,12})[\"”]", blob)
    if len(quoted) > 4:
        warns.append(f"말투에 따옴표 말버릇이 {len(quoted)}개 — 나열하면 모델이 그대로 반복해요. 분위기로 서술하세요")
    for k in ("guard_replies", "fight_replies"):
        if d.get(k) and any(len(str(x)) > 30 for x in d[k]):
            warns.append(f"{k} 는 한마디(30자 이하)여야 해요")
    for h in d.get("concept_hooks") or []:
        try:
            re.compile(str(h.get("when", "")))
        except re.error as e:
            errs.append(f"concept_hooks 정규식 오류: {h.get('when')} ({e})")
    if d.get("avatar_url") and not str(d["avatar_url"]).startswith("http"):
        errs.append("avatar_url 은 http(s) 주소여야 해요")
    if "talk_weight" in d and not (0.1 <= float(d["talk_weight"]) <= 5):
        warns.append("talk_weight 는 0.1~5 권장")
    sch = d.get("schedule")
    if sch:
        sys.path.insert(0, str(ROOT))
        from bot.schedule import Schedule, _minutes
        blocks = sch.get("blocks") or []
        ok = 0
        for i, b in enumerate(blocks, 1):
            try:
                a, c = str(b["at"]).split("-")
                _minutes(a), _minutes(c)
                ok += 1
            except (KeyError, ValueError, TypeError):
                errs.append(f"schedule.blocks[{i}] 형식 오류 (at: 'HH:MM-HH:MM' 필요)")
            if b.get("place") and b["place"] not in Schedule(sch).places:
                warns.append(f"schedule.blocks[{i}] 장소 '{b['place']}' 가 places 나 공통 장소에 없어요")
        if ok and not any(Schedule(sch).current(__import__("datetime").datetime(2026, 1, 1, h, 0)).sleep for h in range(24)):
            warns.append("schedule 에 자는 시간(sleep: true)이 없어요")
    return errs, warns


def main() -> None:
    ids = sys.argv[1:] or [p.stem for p in sorted(CHAR_DIR.glob("*.yaml"))]
    bad = 0
    for cid in ids:
        p = Path(cid) if cid.endswith(".yaml") else CHAR_DIR / f"{cid}.yaml"
        if not p.exists():
            print(f"✗ {cid}: 파일이 없어요 ({p})")
            bad += 1
            continue
        errs, warns = check(p)
        print(f"{'✗' if errs else '✓'} {p.stem}: 오류 {len(errs)} / 경고 {len(warns)}")
        for e in errs:
            print(f"   ✗ {e}")
        for w in warns:
            print(f"   ! {w}")
        bad += bool(errs)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
