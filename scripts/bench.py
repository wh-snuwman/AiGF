"""서버 성능 측정 (GPU/Ollama 가 있는 곳에서 실행):  python scripts/bench.py [옵션]

  (기본)              실제 캐릭터 프롬프트로 N번 요청해 TTFT / 전체 시간 / 토큰 / 생성 속도를 P50·P95·P99 로 측정하고 data/bench/<시각>.json 에 저장
  --n 20              요청 횟수 (기본 20, 첫 요청은 캐시 워밍업이라 통계에서 뺀다)
  --char eunlang      측정에 쓸 캐릭터 (기본: settings 의 character)
  --compare 파일.json   이전 측정 파일과 비교해 변화율을 표시 (프롬프트 압축/설정 변경 전후 비교용)
  --calibrate         글자당 토큰 수를 실측해 data/bench/calib.json 에 저장 (bot/budget.py 가 사용)
  --aux 모델명         보조 교정 모델 후보를 시험: 단독 지연, VRAM 증가, 메인 모델과 동시에 돌릴 때 메인 P95 가 얼마나 느려지는지

TTFT 는 요청 후 첫 토큰 조각이 올 때까지의 시간(스트리밍 측정)이다. 개발 PC(GPU 없음)에서는 의미 있는 값이 나오지 않는다.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "bench"

SAMPLES = ["안녕 뭐해", "점심 뭐 먹을까", "나 오늘 너무 피곤해 ㅠㅠ", "이번 주말에 뭐 할 거야", "너 게임 잘해?",
           "ㅋㅋㅋㅋ 방금 그거 봤어?", "요즘 잠이 안 와", "추천해줄 노래 있어?", "아까 한 말 무슨 뜻이야", "배고프다 야식 시킬까"]
CORRECT_SAMPLES = ["오늘은 날씨가 좋아서 기분이 좋아 졌어", "그거 진짜 맛있는거 같은데 한번 먹어볼래", "나도 같이 하고 싶은데 시간이 없어서 못 해",
                   "방금 뭐라고 했는지 못 들었어 다시 말해줄래", "게임 하다가 밤 샜더니 너무 피곤하네"]


def pct(values: list[float], q: float) -> float:
    from bot.stats import percentile
    return percentile(values, q)


def dist(values: list[float]) -> dict:
    return {"n": len(values), "avg": round(statistics.fmean(values), 3) if values else 0.0, "p50": round(pct(values, 50), 3),
            "p95": round(pct(values, 95), 3), "p99": round(pct(values, 99), 3), "max": round(max(values), 3) if values else 0.0}


def fit_slope(points: list[tuple[int, int]]) -> float:
    """(글자 수, 토큰 수) 점들의 기울기(글자당 토큰). 고정 오버헤드(템플릿)는 기울기에서 빠진다."""
    n = len(points)
    if n < 2:
        return 0.0
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    den = sum((x - mx) ** 2 for x, _ in points)
    return sum((x - mx) * (y - my) for x, y in points) / den if den else 0.0


def host_info(host: str) -> dict:
    info: dict = {"ps": []}
    try:
        with urllib.request.urlopen(f"{host}/api/ps", timeout=5) as r:                # noqa: S310 (로컬 Ollama)
            info["ps"] = [{"name": m.get("name"), "size_gb": round(m.get("size", 0) / 1e9, 2),
                           "vram_gb": round(m.get("size_vram", 0) / 1e9, 2)} for m in json.loads(r.read()).get("models", [])]
    except Exception as e:  # noqa: BLE001
        info["ps_error"] = str(e)[:80]
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if out:
            name, used, total, util = [x.strip() for x in out.splitlines()[0].split(",")]
            info["gpu"] = {"name": name, "used_mb": int(used), "total_mb": int(total), "util_pct": int(util)}
    except Exception:  # noqa: BLE001
        info["gpu"] = None
    return info


def build_messages(cid: str, text: str) -> list[dict]:
    from bot.persona import Persona
    p = Persona(cid)
    return [{"role": "system", "content": p.static_prompt()}] + p.example_messages(3) + [
        {"role": "user", "content": f"유저1: {text}\n\n[지금] 평일 저녁\n기분:평온(약)\n길이:[한 줄]한 문장(20자 안팎)"}]


def stream_once(client, model: str, messages: list[dict], options: dict, keep_alive: str) -> dict:
    t0 = time.perf_counter()
    first = None
    last = None
    for chunk in client.chat(model=model, messages=messages, options=options, keep_alive=keep_alive, stream=True):
        if first is None and (chunk.get("message") or {}).get("content"):
            first = time.perf_counter() - t0
        last = chunk
    total = time.perf_counter() - t0
    g = (lambda k: float(last.get(k, 0) or 0)) if last else (lambda k: 0.0)
    ev = g("eval_duration") / 1e9
    return {"ttft": first if first is not None else total, "total": total, "prompt_tokens": int(g("prompt_eval_count")),
            "completion_tokens": int(g("eval_count")), "load": g("load_duration") / 1e9, "prompt_sec": g("prompt_eval_duration") / 1e9,
            "tps": (g("eval_count") / ev) if ev else 0.0}


def run_bench(args, settings) -> dict:
    import ollama
    cfg = settings["ollama"]
    client = ollama.Client(host=cfg.get("host", "http://127.0.0.1:11434"))
    cid = args.char or settings.get("character", "eunlang")
    opts = dict(cfg.get("options", {}))
    rows = []
    for i in range(args.n + 1):
        rows.append(stream_once(client, cfg["model"], build_messages(cid, SAMPLES[i % len(SAMPLES)]), opts, cfg.get("keep_alive", "24h")))
    rows = rows[1:]                                                  # 첫 요청(모델 로드/캐시 워밍업) 제외
    res = {"time": datetime.now().isoformat(timespec="seconds"), "model": cfg["model"], "character": cid, "n": len(rows),
           "host": host_info(cfg.get("host", "http://127.0.0.1:11434")),
           "ttft": dist([r["ttft"] for r in rows]), "total": dist([r["total"] for r in rows]),
           "prompt_tokens": dist([float(r["prompt_tokens"]) for r in rows]), "completion_tokens": dist([float(r["completion_tokens"]) for r in rows]),
           "tokens_per_sec": dist([r["tps"] for r in rows]),
           "target_3s": {"ttft_p95_ok": pct([r["ttft"] for r in rows], 95) < 3.0, "total_p95_ok": pct([r["total"] for r in rows], 95) < 3.0}}
    return res


def calibrate(settings) -> dict:
    """한글/영문 글자당 토큰 수를 실측한다: 길이가 다른 텍스트들의 prompt_eval_count 기울기. 캐시를 피하려고 매번 다른 무늬를 앞에 붙인다."""
    import random
    import ollama
    cfg = settings["ollama"]
    client = ollama.Client(host=cfg.get("host", "http://127.0.0.1:11434"))

    def count(text: str) -> int:
        nonce = "".join(random.choice("가나다라마바사") for _ in range(6))
        r = client.chat(model=cfg["model"], messages=[{"role": "user", "content": nonce + text}],
                        options={"num_predict": 1, "num_ctx": cfg.get("options", {}).get("num_ctx", 6144)}, keep_alive=cfg.get("keep_alive", "24h"))
        return int(getattr(r, "prompt_eval_count", 0) or r.get("prompt_eval_count", 0))

    ko = "오늘은 날씨가 좋아서 친구랑 같이 밥을 먹고 게임도 한 판 하고 싶어졌다 "
    en = "the quick brown fox jumps over the lazy dog while chatting about games "
    pts_ko = [(len(ko * k), count(ko * k)) for k in (1, 3, 6, 10)]
    pts_en = [(len(en * k), count(en * k)) for k in (1, 3, 6, 10)]
    hangul_ratio = sum(1 for ch in ko if "가" <= ch <= "힣") / len(ko)
    out = {"hangul": round(fit_slope(pts_ko) / max(hangul_ratio, 0.5), 3), "ascii": round(fit_slope(pts_en), 3)}
    out["other"] = round(max(out["ascii"], 0.5), 3)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "calib.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def aux_test(args, settings) -> dict:
    """보조 교정 모델 후보: 단독 지연, VRAM 증가, 메인과 동시에 돌릴 때의 간섭을 잰다."""
    import ollama
    cfg = settings["ollama"]
    host = cfg.get("host", "http://127.0.0.1:11434")
    client = ollama.Client(host=host)
    cid = args.char or settings.get("character", "eunlang")
    before = host_info(host)
    prompt = "다음 한국어 채팅 문장의 맞춤법과 어색한 표현만 고쳐 한 줄로 출력해: "
    solo = []
    for s in CORRECT_SAMPLES * 2:
        t0 = time.perf_counter()
        client.chat(model=args.aux, messages=[{"role": "user", "content": prompt + s}], options={"num_predict": 60, "temperature": 0.1}, keep_alive="5m")
        solo.append(time.perf_counter() - t0)
    solo = solo[1:]                                                   # 첫 호출(로드) 제외
    after = host_info(host)

    def main_run(n: int) -> list[float]:
        return [stream_once(client, cfg["model"], build_messages(cid, SAMPLES[i % len(SAMPLES)]), dict(cfg.get("options", {})), cfg.get("keep_alive", "24h"))["total"]
                for i in range(n)]
    alone = main_run(8)
    stop = threading.Event()

    def bg() -> None:
        while not stop.is_set():
            client.chat(model=args.aux, messages=[{"role": "user", "content": prompt + CORRECT_SAMPLES[0]}], options={"num_predict": 60}, keep_alive="5m")
    th = threading.Thread(target=bg, daemon=True)
    th.start()
    busy = main_run(8)
    stop.set()
    th.join(timeout=10)
    slow = (pct(busy, 95) / pct(alone, 95) - 1) * 100 if pct(alone, 95) else 0.0
    verdict = ("도입 가능" if pct(solo, 95) < 0.5 and slow < 20 else "도입 비권장")
    return {"aux_model": args.aux, "aux_solo": dist(solo), "main_alone_total": dist(alone), "main_with_aux_total": dist(busy),
            "main_p95_slowdown_pct": round(slow, 1), "vram_before": before.get("ps"), "vram_after": after.get("ps"),
            "verdict": f"{verdict} (기준: 보조 P95 < 0.5s, 메인 P95 증가 < 20%)"}


def compare(new: dict, old: dict) -> list[str]:
    lines = []
    for key in ("ttft", "total", "prompt_tokens", "completion_tokens", "tokens_per_sec"):
        for q in ("avg", "p50", "p95"):
            a, b = old.get(key, {}).get(q), new.get(key, {}).get(q)
            if a and b is not None:
                lines.append(f"{key:<18}{q:<5} {a:>8.3f} -> {b:>8.3f}  ({(b / a - 1) * 100:+.1f}%)")
    return lines


def show(res: dict) -> None:
    print(f"== {res['model']} / {res['character']} / {res['n']}회 ==")
    for key, name in (("ttft", "첫 토큰까지(TTFT)"), ("total", "전체 생성 시간"), ("prompt_tokens", "입력 토큰"), ("completion_tokens", "출력 토큰"),
                      ("tokens_per_sec", "생성 속도(토큰/초)")):
        d = res[key]
        print(f"{name:<20} 평균 {d['avg']} · P50 {d['p50']} · P95 {d['p95']} · P99 {d['p99']} · 최대 {d['max']}")
    print("GPU/VRAM:", res["host"].get("gpu") or "nvidia-smi 없음", res["host"].get("ps"))
    print("3초 목표:", res["target_3s"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--char")
    ap.add_argument("--compare")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--aux")
    args = ap.parse_args()
    from bot.config import load_settings
    settings = load_settings()
    try:
        if args.calibrate:
            print("토큰 계수 실측:", calibrate(settings), "→ data/bench/calib.json 저장")
            return
        if args.aux:
            print(json.dumps(aux_test(args, settings), ensure_ascii=False, indent=2))
            return
        res = run_bench(args, settings)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"측정 실패: {type(e).__name__}: {e}\nOllama 가 켜져 있고 모델이 설치돼 있는지 확인하세요 (python -m bot.doctor).")
    show(res)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"bench-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print("저장:", path)
    if args.compare:
        print("\n== 이전 측정과 비교 ==")
        print("\n".join(compare(res, json.loads(Path(args.compare).read_text(encoding="utf-8")))))


if __name__ == "__main__":
    main()
