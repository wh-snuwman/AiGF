"""디버깅/개선용 상세 기록 (JSONL).

받은 메시지, 대답 대상을 고른 과정(후보와 점수), 프롬프트에 들어간 정보(기분/기억/인식/힌트), LLM 원문 출력과
후처리 과정(과다어 제거, 중복 제거, 방어 검사), 최종 말풍선, 전송 결과까지 한 줄에 한 사건(JSON)으로 저장한다.
파일: data/logs/trace-YYYY-MM-DD.jsonl  (data/ 는 git 에 올라가지 않는다. 사용자 메시지가 들어 있으니 공유 주의)

kind 종류
- recv     : 받은 모든 메시지 (처리 결과: recorded / dropped:muted / dropped:rate / dropped:channel / dm ...)
- decision : 대답하지 않기로 한 결정 (확률 탈락, 쿨다운, 사소한 말 이모지 등)
- reply / initiate / proactive : 한 번의 응답 생성 전체 (대상, 후보 점수, 프롬프트, LLM 호출들, 후처리 이벤트, 최종 결과)
- send     : 실제로 디스코드에 보낸 내용 (trace_id 로 위 응답과 연결)
- react    : 이모지 리액션
- event    : 기타 (싸움 시작/종료, 인젝션 차단 등)
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("trace")

_cur: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar("trace_cur", default=None)
_meta: contextvars.ContextVar[dict | None] = contextvars.ContextVar("trace_meta", default=None)

# guard.redact 와 같은 모양의 토큰/키 패턴 (guard 를 import 하지 않아 순환을 피한다)
_TOKEN_RE = re.compile(
    r"[MNO][A-Za-z\d_-]{23,25}\.[\w-]{6}\.[\w-]{27,}|\b(?:sk|ghp|gho|ghs|xox[bpa])[-_][A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|AKIA[0-9A-Z]{16}")


def _clean(v: Any, limit: int) -> Any:
    if isinstance(v, str):
        v = _TOKEN_RE.sub("[가림]", v)
        return v if len(v) <= limit else v[:limit] + f"…(+{len(v) - limit}자)"
    if isinstance(v, dict):
        return {str(k): _clean(x, limit) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_clean(x, limit) for x in list(v)[:200]]
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, (int, bool)) or v is None:
        return v
    return _clean(str(v), limit)


class Trace:
    """한 번의 응답 생성 기록. engine 이 begin() 으로 만들고 finish() 로 저장한다."""

    def __init__(self, tracer: "Tracer", kind: str, fields: dict) -> None:
        self.tracer = tracer
        self.id = uuid.uuid4().hex[:8]
        self.data: dict[str, Any] = {"id": self.id, "kind": kind, **fields}
        self.events: list[dict] = []
        self.t0 = time.time()
        self._done = False

    def set(self, **kv: Any) -> None:
        self.data.update(kv)

    def event(self, name: str, **kv: Any) -> None:
        self.events.append({"t": round(time.time() - self.t0, 3), "e": name, **kv})

    def finish(self, **kv: Any) -> None:
        if self._done:
            return
        self._done = True
        self.data.update(kv)
        self.data["events"] = self.events
        self.data["total_sec"] = round(time.time() - self.t0, 3)
        self.tracer._write(self.data)
        if _cur.get() is self:
            _cur.set(None)


class _Null:
    """기록이 꺼져 있거나 진행 중인 기록이 없을 때 쓰는 빈 객체 (호출해도 아무 일 없음)."""
    id = ""

    def set(self, **kv: Any) -> None: ...
    def event(self, name: str, **kv: Any) -> None: ...
    def finish(self, **kv: Any) -> None: ...


NULL = _Null()


def cur() -> Trace | _Null:
    return _cur.get() or NULL


def set_meta(**kv: Any) -> None:
    """다음 begin() 이 가져갈 부가 정보 (예: 대상 선정 과정). 같은 비동기 작업 안에서만 전달된다."""
    m = dict(_meta.get() or {})
    m.update(kv)
    _meta.set(m)


class Tracer:
    def __init__(self, cfg: dict | None = None, base_dir: Path | None = None) -> None:
        self.cfg = {"enabled": True, "dir": "data/logs", "include_prompt": True, "retention_days": 30,
                    "max_text": 4000, **(cfg or {})}
        root = base_dir or Path(__file__).resolve().parent.parent
        d = Path(self.cfg["dir"])
        self.dir = d if d.is_absolute() else root / d
        self.char_id = ""
        self._last_cleanup = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    # ---------- 쓰기 ----------
    def _path(self, day: str | None = None) -> Path:
        return self.dir / f"trace-{day or datetime.now().strftime('%Y-%m-%d')}.jsonl"

    def _write(self, rec: dict) -> None:
        if not self.enabled:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now()
            rec = {"time": now.strftime("%Y-%m-%d %H:%M:%S"), "ts": round(now.timestamp(), 3),
                   "char": self.char_id, **rec}
            limit = int(self.cfg["max_text"])
            line = json.dumps(_clean(rec, limit), ensure_ascii=False, separators=(",", ":"))
            with open(self._path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if time.time() - self._last_cleanup > 3600:
                self.cleanup()
        except Exception:  # noqa: BLE001  (기록 실패가 봇 동작을 막으면 안 된다)
            log.exception("trace 기록 실패")

    def log(self, kind: str, **fields: Any) -> str:
        """한 줄짜리 사건 기록. 반환: 기록 id"""
        if not self.enabled:
            return ""
        tid = uuid.uuid4().hex[:8]
        self._write({"id": tid, "kind": kind, **fields})
        return tid

    def begin(self, kind: str, **fields: Any) -> Trace | _Null:
        """응답 생성 기록 시작. 이후 같은 비동기 작업 안의 LLM 호출/후처리가 cur() 로 이벤트를 덧붙인다."""
        if not self.enabled:
            _cur.set(None)
            return NULL
        meta = _meta.get() or {}
        _meta.set(None)
        tr = Trace(self, kind, {**meta, **fields})
        _cur.set(tr)
        return tr

    # ---------- 보관 정리 ----------
    def cleanup(self) -> int:
        self._last_cleanup = time.time()
        days = int(self.cfg.get("retention_days", 30))
        if days <= 0 or not self.dir.exists():
            return 0
        limit = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        n = 0
        for p in self.dir.glob("trace-*.jsonl"):
            if p.stem[6:] < limit:
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
        return n

    # ---------- 읽기 ----------
    def files(self) -> list[Path]:
        return sorted(self.dir.glob("trace-*.jsonl")) if self.dir.exists() else []

    def read(self, limit: int = 20, kind: str | None = None, user: int | None = None,
             trace_id: str | None = None, since_ts: float = 0.0) -> list[dict]:
        """최근 기록을 새것부터 limit 개. 최신 파일부터 거꾸로 읽는다."""
        out: list[dict] = []
        for p in reversed(self.files()):
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for ln in reversed(lines):
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if since_ts and r.get("ts", 0) < since_ts:
                    return out
                if trace_id and r.get("id") != trace_id:
                    continue
                if kind and r.get("kind") != kind:
                    continue
                if user and r.get("user_id") != user and r.get("target_id") != user:
                    continue
                out.append(r)
                if len(out) >= limit:
                    return out
        return out

    def export(self, hours: float = 24.0, max_bytes: int = 7_500_000) -> tuple[bytes, int]:
        """최근 hours 시간의 기록을 JSONL 바이트로 (용량 제한 시 오래된 것부터 잘라냄). 반환: (데이터, 줄 수)"""
        since = time.time() - hours * 3600
        rows: list[str] = []
        for p in self.files():
            try:
                for ln in p.read_text(encoding="utf-8").splitlines():
                    try:
                        if json.loads(ln).get("ts", 0) >= since:
                            rows.append(ln)
                    except ValueError:
                        continue
            except OSError:
                continue
        data = "\n".join(rows)
        while len(data.encode("utf-8")) > max_bytes and len(rows) > 1:   # 최신 1건은 항상 남긴다
            rows = rows[len(rows) // 10 + 1:]
            data = "\n".join(rows)
        return (data + "\n").encode("utf-8"), len(rows)


def summarize(r: dict, width: int = 70) -> str:
    """한 기록을 한 줄 요약 (/로그 보기)"""
    def cut(s: Any, n: int) -> str:
        s = re.sub(r"\s+", " ", str(s or ""))
        return s if len(s) <= n else s[:n] + "…"
    k = r.get("kind", "?")
    t = str(r.get("time", ""))[5:19]
    if k == "recv":
        return f"`{t}` 📥 {cut(r.get('label') or r.get('user_name'), 10)}: {cut(r.get('text'), width)} → {r.get('outcome', '')}"
    if k in ("reply", "initiate", "proactive"):
        out = " / ".join(r.get("bubbles") or []) or f"({r.get('outcome', '무응답')})"
        who = cut(r.get("target_label") or r.get("target_name") or "먼저말걸기", 10)
        return (f"`{t}` 💬 [{r.get('id')}] {who}: {cut(r.get('input'), 30)} → {cut(out, width)} "
                f"({r.get('emotion', '')}, {r.get('total_sec', 0):.1f}s)")
    if k == "decision":
        return f"`{t}` ⏭ {r.get('action')}: {cut(r.get('detail'), width)}"
    if k == "send":
        return f"`{t}` 📤 [{r.get('trace_id', '')}] {cut(' / '.join(r.get('bubbles') or []), width)}"
    return f"`{t}` {k} {cut(r.get('detail') or r.get('what'), width)}"
