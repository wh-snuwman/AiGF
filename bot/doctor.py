"""환경 진단:  python -m bot.doctor
봇이 안 켜지거나 대답을 못 할 때 원인과 해결 명령을 알려 준다 (토큰 값은 출력하지 않는다).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

OK, BAD, WARN = "✅", "❌", "⚠ "


def _candidate_dirs() -> list[Path]:
    home = Path.home()
    env = os.environ.get("OLLAMA_MODELS")
    c = [Path(env)] if env else []
    c += [Path("/mnt/hdd/ollama/models"), home / ".ollama" / "models", Path("/usr/share/ollama/.ollama/models")]
    out: list[Path] = []
    for p in c:
        if p not in out:
            out.append(p)
    return out


def find_model_dirs(model: str) -> list[Path]:
    """모델 매니페스트가 실제로 들어 있는 Ollama 모델 폴더를 찾는다."""
    name, _, tag = model.partition(":")
    found = []
    for d in _candidate_dirs():
        m = d / "manifests" / "registry.ollama.ai" / "library" / name
        try:
            if m.is_dir() and (not tag or (m / tag).exists()):
                found.append(d)
        except OSError:
            continue
    return found


async def run() -> int:
    bad = 0
    print("== 봇 환경 진단 ==")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    print(f"{OK if in_venv else WARN} 파이썬 {sys.version.split()[0]} ({'가상환경 사용 중' if in_venv else '가상환경 아님: source .venv/bin/activate 를 먼저 하세요'})")

    try:
        from .config import load_settings
        settings = load_settings()
        print(f"{OK} settings.yaml 읽기 성공")
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} settings.yaml 을 읽지 못했어요: {e}")
        return 1

    token = os.environ.get("DISCORD_TOKEN", "")
    if not token or token.startswith("여기에"):
        print(f"{BAD} DISCORD_TOKEN 이 없어요. `cp .env.example .env` 후 .env 에 봇 토큰을 넣으세요.")
        bad += 1
    else:
        print(f"{OK} DISCORD_TOKEN 설정됨 (길이 {len(token)})")

    model = settings["ollama"]["model"]
    host = settings["ollama"].get("host", "http://127.0.0.1:11434")
    try:
        from ollama import AsyncClient
        resp = await AsyncClient(host=host).list()
        names = [str(getattr(m, "model", "")) for m in (resp.models or [])]
        print(f"{OK} Ollama 연결 성공 ({host}), 설치된 모델 {len(names)}개: {', '.join(names[:8]) or '(없음)'}")
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} Ollama 에 연결할 수 없어요 ({host}): {type(e).__name__}\n     → 다른 터미널에서 `bash scripts/run_ollama.sh` 로 켜세요.")
        return bad + 1

    base = lambda n: n[:-7] if n.endswith(":latest") else n          # noqa: E731
    cc = settings.get("corrector") or {}
    if cc.get("enabled"):                                             # 문법 교정용 작은 모델
        cm = cc.get("model", "qwen2.5:3b")
        if any(base(n) == base(cm) for n in names):
            print(f"{OK} 문법 교정 모델 {cm} 설치됨 (OLLAMA_MAX_LOADED_MODELS 는 2 이상이어야 메인과 함께 상주해요: `bash scripts/run_ollama.sh`)")
        else:
            print(f"{WARN}문법 교정 모델 {cm} 이(가) 없어요 → `ollama pull {cm}` (없으면 교정만 자동으로 꺼지고 대화는 정상)")
    if any(base(n) == base(model) for n in names):
        print(f"{OK} 모델 {model} 설치됨")
    else:
        fam = [n for n in names if n.split(":")[0] == model.split(":")[0]]
        if fam:
            print(f"{WARN}모델 {model} 은 없지만 같은 계열 {fam[0]} 이 있어 봇이 그걸 대신 씁니다 (정식: `ollama pull {model}`)")
        else:
            bad += 1
            print(f"{BAD} 모델 {model} 이 이 Ollama 서버에 없어요.")
            dirs = find_model_dirs(model)
            if dirs:
                d = dirs[0]
                cands = [Path(os.environ["OLLAMA_MODELS"])] if os.environ.get("OLLAMA_MODELS") else []
                cands += [Path("/mnt/hdd/ollama/models"), Path.home() / ".ollama" / "models"]
                target = next((c for c in cands if c.exists() and c != d), Path("/mnt/hdd/ollama/models"))
                print(f"     모델 파일은 {d} 에 있어요. 지금 켜진 Ollama 는 다른 폴더를 보고 있어요.")
                print("     방법 A) 그 파일을 지금 쓰는 폴더로 복사 (다운로드 없음, 권장):")
                print(f"        sudo cp -a {d}/. {target}/ && sudo chown -R $USER: {target}")
                print("     방법 B) 그 폴더로 Ollama 를 다시 켜기 (읽기 권한 필요):")
                print(f"        pkill -x ollama; OLLAMA_MODELS={d} ollama serve")
            else:
                print("     모델 파일을 찾지 못했어요(/mnt/hdd/ollama/models, ~/.ollama/models, /usr/share/ollama/.ollama/models).")
                print(f"     → 새로 받으세요(약 8GB): `ollama pull {model}`")
            if names:
                print(f"     (또는 config/settings.yaml 의 ollama.model 을 설치된 모델 {', '.join(names[:4])} 중 하나로 바꿔도 되지만 말투/속도가 달라지고, 12GB VRAM 에는 26b 급이 너무 커요)")

    if not settings.get("web", {}).get("allowed", True):
        print(f"{OK} 웹 검색: 설정에서 꺼짐 (web.allowed: false)")
    else:
        from .web import ipv6_reachable
        v6 = await asyncio.to_thread(ipv6_reachable)
        if v6 is False:
            print(f"{WARN}IPv6 로 나갈 수 없는 서버예요 → 검색 엔진 일부가 간헐적으로 실패할 수 있어요(봇이 자동으로 다른 엔진으로 전환). 근본 해결은 README '문제 해결'의 IPv6 안내")
        elif v6:
            print(f"{OK} IPv6 사용 가능")

        import importlib.util
        if importlib.util.find_spec("ddgs") or importlib.util.find_spec("duckduckgo_search"):
            from .web import ddgs_version
            print(f"{OK} ddgs {ddgs_version() or ''} 설치됨 (무료 검색 엔진)")
        else:
            print(f"{WARN}ddgs 가 없어요 → 무료 검색 엔진은 꺼집니다 (`pip install ddgs`). 봇 동작에는 영향 없음")
        try:
            from .web import WebInfo
            chain, skipped = WebInfo(settings.get("web", {}), None).chain()
            names = [n if n == u else f"{n}[{u}]" for n, u in chain]
            print(f"{OK if chain else WARN}검색 순서: {' > '.join(names) or '없음'}" + (f"  (제외: {', '.join(skipped)})" if skipped else ""))
            if not any(n in ("naver", "searxng") for n, _u in chain):
                print("     무료 엔진은 서버 요청을 자주 막아요. 안정적으로 쓰려면 .env 에 NAVER_CLIENT_ID/NAVER_CLIENT_SECRET (README '인터넷 정보')")
        except Exception as e:  # noqa: BLE001
            print(f"{WARN}검색 설정 확인 실패: {type(e).__name__}")

    try:
        from .persona import Persona
        for cid, name in Persona.available():
            p = Persona(cid)
            n = len(p.static_prompt())
            print(f"{OK if n < 1800 else WARN}캐릭터 {name}({cid}) 프롬프트 {n}자")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"{BAD} 캐릭터 파일 오류: {e}")

    data = Path(settings.get("memory", {}).get("db_path", "data/memory.db")).parent
    try:
        data.mkdir(parents=True, exist_ok=True)
        t = data / ".write_test"
        t.write_text("ok")
        t.unlink()
        print(f"{OK} 데이터 폴더 쓰기 가능 ({data})")
    except OSError as e:
        bad += 1
        print(f"{BAD} 데이터 폴더에 쓸 수 없어요 ({data}): {e}")

    print("\n" + ("문제 없음. `python -m bot.main` 으로 실행하세요." if not bad else f"고칠 문제 {bad}개."))
    return 1 if bad else 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
