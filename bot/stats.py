"""운영 통계: 응답 지연/토큰 퍼센타일(trace 기록 기반)과 호감도·감정 변화 그래프(matplotlib).

- 그래프는 matplotlib 가 없으면 만들 수 없다고 알려 주고 봇은 계속 동작한다 (선택 의존성).
- 한글 글꼴을 찾지 못하면 감정/호감도 이름 대신 영문 id 를 쓴다.
"""
from __future__ import annotations

import io
import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("stats")

FONT_CANDIDATES = ("Malgun Gothic", "NanumGothic", "Noto Sans CJK KR", "Noto Sans KR", "AppleGothic", "UnDotum", "Gulim")


def percentile(values: list[float], q: float) -> float:
    """선형 보간 퍼센타일 (q: 0~100). 값이 없으면 0."""
    xs = sorted(values)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return float(xs[0])
    k = (len(xs) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return float(xs[lo] + (xs[hi] - xs[lo]) * (k - lo))


def _dist(values: list[float]) -> dict[str, float]:
    return {"n": len(values), "avg": round(sum(values) / len(values), 2) if values else 0.0,
            "p50": round(percentile(values, 50), 2), "p95": round(percentile(values, 95), 2),
            "p99": round(percentile(values, 99), 2), "max": round(max(values), 2) if values else 0.0}


def latency_report(files: list[Path], hours: float = 24.0) -> dict:
    """trace JSONL 에서 지연/토큰 통계를 뽑는다.
    total: 메시지 처리 전체(생성~전송 직전), llm: 모델 호출 시간, load: 모델 로드 대기, queue: 수신 후 대기."""
    since = time.time() - hours * 3600
    total, llm_sec, load, queue, ptok, ctok, tps = [], [], [], [], [], [], []
    outcomes: dict[str, int] = {}
    errors = 0
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            try:
                if float(r.get("ts", 0)) < since:
                    continue
            except (TypeError, ValueError):
                continue
            if r.get("kind") not in ("reply", "initiate", "proactive"):
                continue
            oc = str(r.get("outcome") or "?")
            outcomes[oc] = outcomes.get(oc, 0) + 1
            if oc in ("error", "failed"):
                errors += 1
            if oc == "replied" and isinstance(r.get("total_sec"), (int, float)):
                total.append(float(r["total_sec"]))
            sel = r.get("selection") or {}
            if isinstance(r.get("wait_sec"), (int, float)):
                queue.append(float(r["wait_sec"]))
            elif isinstance(sel, dict) and isinstance(sel.get("wait_sec"), (int, float)):
                queue.append(float(sel["wait_sec"]))
            for e in r.get("events", []) or []:
                if isinstance(e, dict) and e.get("e") == "llm":
                    if isinstance(e.get("sec"), (int, float)):
                        llm_sec.append(float(e["sec"]))
                    if isinstance(e.get("load_sec"), (int, float)):
                        load.append(float(e["load_sec"]))
                    pt, ct = e.get("prompt_tokens"), e.get("completion_tokens")
                    if isinstance(pt, (int, float)) and pt:
                        ptok.append(float(pt))
                    if isinstance(ct, (int, float)) and ct:
                        ctok.append(float(ct))
                        if isinstance(e.get("sec"), (int, float)) and e["sec"] > 0:
                            tps.append(float(ct) / float(e["sec"]))
    return {"hours": hours, "outcomes": outcomes, "errors": errors, "total_sec": _dist(total), "llm_sec": _dist(llm_sec),
            "load_sec": _dist(load), "queue_sec": _dist(queue), "prompt_tokens": _dist(ptok),
            "completion_tokens": _dist(ctok), "tokens_per_sec": _dist(tps)}


def format_latency(rep: dict) -> str:
    def row(name: str, d: dict, unit: str = "s") -> str:
        if not d["n"]:
            return f"{name}: 기록 없음"
        return f"{name}: 평균 {d['avg']}{unit} · P50 {d['p50']} · P95 {d['p95']} · P99 {d['p99']} · 최대 {d['max']} ({d['n']}건)"
    oc = ", ".join(f"{k} {v}" for k, v in sorted(rep["outcomes"].items(), key=lambda kv: -kv[1])[:6]) or "없음"
    return "\n".join([
        f"**최근 {rep['hours']:g}시간 응답 통계**",
        row("응답 전체", rep["total_sec"]), row("모델 호출", rep["llm_sec"]), row("모델 로드 대기", rep["load_sec"]),
        row("수신 후 대기", rep["queue_sec"]), row("입력 토큰", rep["prompt_tokens"], ""), row("출력 토큰", rep["completion_tokens"], ""),
        row("생성 속도(토큰/초)", rep["tokens_per_sec"], ""), f"결과: {oc}"])


# ---------------- 그래프 ----------------
def _plt():
    """matplotlib 를 Agg(화면 없는 서버용)로 불러온다. 없으면 None."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except Exception:  # noqa: BLE001
        return None, False
    korean = False
    names = {f.name for f in font_manager.fontManager.ttflist}
    for c in FONT_CANDIDATES:
        if c in names:
            plt.rcParams["font.family"] = c
            korean = True
            break
    plt.rcParams["axes.unicode_minus"] = False
    return plt, korean


def available() -> bool:
    return _plt()[0] is not None


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    buf.seek(0)
    return buf.read()


def affinity_graph(history: list[tuple], title: str, to100) -> bytes | None:
    """history: [(ts, old, new, reason, emotion)] 오래된 순. to100: 내부값 → 0~100 변환 함수. matplotlib 없으면 None."""
    plt, korean = _plt()
    if plt is None:
        return None
    fig, ax = plt.subplots(figsize=(8, 3.6))
    if history:
        xs = [datetime.fromtimestamp(h[0]) for h in history]
        ys = [to100(h[2]) for h in history]
        ax.step(xs, ys, where="post", color="#4c78a8", linewidth=1.8)
        ax.scatter(xs, ys, s=14, color="#4c78a8", zorder=3)
        big = sorted(range(len(history)), key=lambda i: -abs(history[i][2] - history[i][1]))[:4]    # 큰 변화 몇 곳에 원인 표시
        for i in big:
            if korean and history[i][3]:
                ax.annotate(str(history[i][3])[:14], (xs[i], ys[i]), textcoords="offset points", xytext=(0, 8),
                            ha="center", fontsize=8, color="#c44e52")
    else:
        ax.text(0.5, 0.5, "기록 없음" if korean else "no data", ha="center", va="center", transform=ax.transAxes)
    ax.axhline(50, color="#999999", linewidth=0.8, linestyle="--")
    for lvl in (25, 40, 60, 80):
        ax.axhline(lvl, color="#dddddd", linewidth=0.6)
    ax.set_ylim(0, 100)
    ax.set_ylabel("호감도 (0~100)" if korean else "affinity (0-100)")
    ax.set_title(title)
    fig.autofmt_xdate()
    try:
        return _png(fig)
    finally:
        plt.close(fig)


def emotion_graph(history: list[tuple], title: str, label_of) -> bytes | None:
    """history: [(ts, emotion_id, intensity)] 오래된 순. label_of: 감정 id → 표시 이름. 감정 종류를 세로축, 강도를 점 크기로."""
    plt, korean = _plt()
    if plt is None:
        return None
    fig, ax = plt.subplots(figsize=(8, 4.2))
    if history:
        ids = sorted({h[1] for h in history})
        pos = {e: i for i, e in enumerate(ids)}
        xs = [datetime.fromtimestamp(h[0]) for h in history]
        ys = [pos[h[1]] for h in history]
        ax.plot(xs, ys, color="#bbbbbb", linewidth=0.8, drawstyle="steps-post", zorder=1)
        ax.scatter(xs, ys, s=[30 + 220 * float(h[2]) for h in history], color="#e45756", alpha=0.7, zorder=2)
        ax.set_yticks(range(len(ids)))
        ax.set_yticklabels([label_of(e) if korean else e for e in ids])
    else:
        ax.text(0.5, 0.5, "기록 없음" if korean else "no data", ha="center", va="center", transform=ax.transAxes)
    ax.set_title(title)
    fig.autofmt_xdate()
    try:
        return _png(fig)
    finally:
        plt.close(fig)
