"""상세 기록(data/logs/trace-*.jsonl) 요약 리포트.

사용:  python scripts/trace_report.py [일수=3] [--user 유저아이디] [--dump 기록id]
- 받은 메시지 처리 결과, 응답 결과, 평균 속도/토큰
- 가장 많이 반복한 봇 말(정형행동 확인), 가장 많이 쓴 시작어
- 대상 선정 분포(누구에게 얼마나 대답했는지), 거절/방어 발생, 느린 응답 TOP
- --dump 기록id : 그 응답의 전체 기록(프롬프트, 후보 점수, LLM 원문, 후처리)을 보기 좋게 출력
"""
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "data" / "logs"


def load(days: float):
    since = time.time() - days * 86400
    rows = []
    for p in sorted(LOGS.glob("trace-*.jsonl")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("ts", 0) >= since:
                rows.append(r)
    return rows


def first_word(s: str) -> str:
    m = re.match(r"\s*([가-힣A-Za-z]{2,8})", s)
    return m.group(1)[:2] if m else ""


def main() -> None:
    args = sys.argv[1:]
    user = None
    dump = None
    if "--user" in args:
        i = args.index("--user"); user = int(args[i + 1]); del args[i:i + 2]
    if "--dump" in args:
        i = args.index("--dump"); dump = args[i + 1]; del args[i:i + 2]
    days = float(args[0]) if args else 3.0
    rows = load(days)
    if dump:
        for r in rows:
            if r.get("id") == dump:
                print(json.dumps(r, ensure_ascii=False, indent=2))
                return
        sys.exit("그 id 의 기록이 없어요.")
    if user:
        rows = [r for r in rows if r.get("user_id") == user or r.get("target_id") == user]
    if not rows:
        sys.exit(f"{LOGS} 에 최근 {days:g}일 기록이 없어요.")
    resp = [r for r in rows if r.get("kind") in ("reply", "initiate", "proactive")]
    print(f"== 최근 {days:g}일 기록 {len(rows)}건 ==")
    print("종류:", dict(Counter(r["kind"] for r in rows)))
    print("받은 메시지 처리:", dict(Counter(r.get("outcome") for r in rows if r["kind"] == "recv")))
    print("응답 결과:", dict(Counter(r.get("outcome") for r in resp)))
    print("결정(무응답):", dict(Counter(r.get("action") for r in rows if r["kind"] == "decision")))
    secs = [r["total_sec"] for r in resp if r.get("outcome") == "replied" and "total_sec" in r]
    if secs:
        print(f"응답 생성 평균 {sum(secs) / len(secs):.1f}s / 최대 {max(secs):.1f}s ({len(secs)}건)")
    toks = [sum(e.get("prompt_tokens", 0) for e in r.get("events", []) if e.get("e") == "llm") for r in resp]
    toks = [t for t in toks if t]
    if toks:
        print(f"프롬프트 토큰 평균 {sum(toks) // len(toks)}")
    outs = [b for r in resp for b in (r.get("bubbles") or [])]
    print("\n== 봇이 가장 많이 반복한 말 ==")
    for t, n in Counter(outs).most_common(8):
        if n >= 2:
            print(f"  {n}회  {t[:60]}")
    print("== 가장 많이 쓴 시작어 ==")
    for w, n in Counter(first_word(b) for b in outs if first_word(b)).most_common(8):
        print(f"  {w}  {n}회 ({n * 100 // max(1, len(outs))}%)")
    print("== 대상 선정 분포 (reply) ==")
    for name, n in Counter(r.get("target_label") for r in resp if r["kind"] == "reply").most_common(8):
        print(f"  {name}: {n}회")
    guard = [r for r in resp if (r.get("guard") or {}).get("level") in ("block", "decline")]
    print(f"== 방어 발생 {len(guard)}건 ==")
    for r in guard[:6]:
        print(f"  [{r['id']}] {r.get('target_label')}: {str(r.get('input'))[:40]} → {r.get('guard')}")
    dedupe = sum(1 for r in resp for e in r.get("events", []) if e.get("e") == "dedupe" and e.get("kept") != e.get("before"))
    strip = sum(1 for r in resp for e in r.get("events", []) if e.get("e") == "strip_overused")
    print(f"반복 방지 개입: 중복 제거 {dedupe}회, 과다어 제거 {strip}회")
    print("== 느린 응답 TOP 5 ==")
    for r in sorted((r for r in resp if "total_sec" in r), key=lambda r: -r["total_sec"])[:5]:
        print(f"  [{r['id']}] {r['total_sec']:.1f}s  {r.get('target_label')}: {str(r.get('input'))[:30]}")
    print("\n자세히 보려면: python scripts/trace_report.py --dump <id>")


if __name__ == "__main__":
    main()
