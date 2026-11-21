"""웹 검색 프로바이더 체인/자동 전환 테스트: python -m unittest discover -s tests"""
import asyncio
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import web  # noqa: E402
from bot.web import HUB, Blocked, WebInfo  # noqa: E402


def make(script, upstream=None, **cfg):
    """script: {엔진: 결과 리스트 | 예외 | [예외/결과 순서]}. upstream: {엔진: 업스트림}. 호출 기록은 calls 에."""
    w = WebInfo({"backends": list(script), **cfg}, None)
    w._avail = {k: (upstream or {}).get(k, k) for k in script}
    calls: list[str] = []
    seqs = {k: iter(v) for k, v in script.items() if isinstance(v, tuple)}

    def fake(query, n, backend):
        calls.append(backend)
        v = next(seqs[backend]) if backend in seqs else script[backend]
        if isinstance(v, Exception):
            raise v
        return v

    w._search_one = fake
    return w, calls


def run(w, q="q", fast=False):
    return asyncio.run(w._runner(q, 5, fast)())


class EngineTests(unittest.TestCase):
    def setUp(self):
        HUB.reset()
        os.environ.pop("NAVER_CLIENT_ID", None)
        os.environ.pop("NAVER_CLIENT_SECRET", None)

    def test_failover_and_cooldown(self):
        w, calls = make({"a": Blocked("HTTP 403"), "b": [("t", "body")]})
        rows, eng, _ = run(w)
        self.assertEqual(eng, "b")
        self.assertGreater(HUB.engines["a"]["bad_until"], time.time())
        calls.clear()
        run(w, "q2")
        self.assertEqual(calls, ["b"])            # 쉬는 중인 a 는 건너뜀

    def test_shared_upstream_rests_together(self):
        # yahoo·duckduckgo 는 둘 다 bing: yahoo 가 막히면 duckduckgo 도 건너뛰고 brave 로
        w, calls = make({"yahoo": Blocked("HTTP 403"), "duckduckgo": [("x", "y")], "brave": [("t", "b")]},
                        upstream={"yahoo": "bing", "duckduckgo": "bing"})
        rows, eng, _ = run(w)
        self.assertEqual((eng, calls), ("brave", ["yahoo", "brave"]))
        self.assertIn("bing", HUB.engines)
        self.assertIn("yahoo[bing](쉬는 중", w.status())

    def test_cooldown_grows_and_recovers(self):
        HUB.engine_fail("a", "x")
        first = HUB.engines["a"]["bad_until"] - time.time()
        HUB.engine_fail("a", "x")
        second = HUB.engines["a"]["bad_until"] - time.time()
        self.assertGreater(second, first * 1.5)
        for _ in range(10):
            HUB.engine_fail("a", "x")
        self.assertLessEqual(HUB.engines["a"]["bad_until"] - time.time(), 3600 + 1)
        HUB.engine_ok("a")
        self.assertEqual(HUB.engine_order(["a", "b"]), ["a", "b"])

    def test_all_resting_still_tries(self):
        HUB.engine_fail("a", "x")
        HUB.engine_fail("b", "x")
        HUB.engine_fail("b", "x")
        self.assertEqual(HUB.engine_order(["b", "a"]), ["a", "b"])   # 먼저 풀리는 순서

    def test_enetunreach_retry_once(self):
        err = OSError("Network is unreachable (os error 101)")
        self.assertIn("ENETUNREACH", web.classify_error(err))
        w, calls = make({"a": (err, [("t", "b")])})
        rows, eng, _ = run(w)
        self.assertEqual((eng, calls), ("a", ["a", "a"]))
        self.assertEqual(HUB.engines["a"]["fails"], 0)

    def test_empty_penalized_only_if_later_engine_succeeds(self):
        w, _ = make({"a": [], "b": [("t", "b")]})
        run(w)
        self.assertGreater(HUB.engines["a"]["bad_until"], time.time())
        HUB.reset()
        w, _ = make({"a": [], "b": []})
        rows, eng, err = run(w)
        self.assertEqual(rows, [])
        self.assertNotIn("a", HUB.engines)         # 전부 비면 쉬게 하지 않는다

    def test_fast_tries_up_to_two(self):
        w, calls = make({"a": OSError("x"), "b": OSError("y"), "c": [("t", "b")]})
        rows, eng, _ = run(w, fast=True)
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(rows, [])

    def test_fast_skips_same_upstream(self):
        w, calls = make({"yahoo": OSError("x"), "duckduckgo": [("d", "d")], "brave": [("t", "b")]},
                        upstream={"yahoo": "bing", "duckduckgo": "bing"})
        rows, eng, _ = run(w, fast=True)
        self.assertEqual((eng, calls), ("brave", ["yahoo", "brave"]))

    def test_official_api_empty_not_penalized(self):
        w, _ = make({"naver": [], "b": [("t", "b")]})
        run(w)
        self.assertNotIn("naver", HUB.engines)

    def test_blocked_is_not_reported_as_empty(self):
        self.assertEqual(web.classify_error(Blocked("HTTP 403")), "403 차단")
        self.assertIn("차단", web.classify_error(Blocked("HTTP 503")))
        self.assertIn("401", web.classify_error(Blocked("HTTP 401")))


class ChainTests(unittest.TestCase):
    def setUp(self):
        HUB.reset()
        os.environ.pop("NAVER_CLIENT_ID", None)
        os.environ.pop("NAVER_CLIENT_SECRET", None)

    def chain_of(self, backends, avail, **cfg):
        w = WebInfo({"backends": backends, **cfg}, None)
        w._avail = avail
        return w.chain()

    def test_disabled_engine_is_skipped_not_auto(self):
        out, skipped = self.chain_of(["google", "yahoo"], {"yahoo": "bing", "brave": "brave"})
        self.assertEqual(out, [("yahoo", "bing")])
        self.assertTrue(any(s.startswith("google(") for s in skipped))

    def test_naver_and_searxng_need_config(self):
        out, skipped = self.chain_of(["naver", "searxng", "yahoo"], {"yahoo": "bing"})
        self.assertEqual([n for n, _ in out], ["yahoo"])
        self.assertIn("naver(키 없음)", skipped)
        with mock.patch.dict(os.environ, {"NAVER_CLIENT_ID": "id", "NAVER_CLIENT_SECRET": "sec"}):
            out, _ = self.chain_of(["naver", "searxng", "yahoo"], {"yahoo": "bing"}, searxng_url="http://x")
        self.assertEqual([n for n, _ in out], ["naver", "searxng", "yahoo"])

    def test_default_backends_without_ddgs(self):
        out, skipped = self.chain_of(None, None)
        self.assertEqual(out, [])
        self.assertTrue(any("ddgs 없음" in s for s in skipped))


class NaverTests(unittest.TestCase):
    def test_parse_and_strip_tags(self):
        body = json.dumps({"items": [{"title": "<b>럭키비키</b> 뜻", "description": "요즘 &quot;밈&quot; 정리"}]}).encode()

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body

        with mock.patch("urllib.request.OpenerDirector.open", return_value=Resp()):
            rows = web._backend_naver("럭키비키", 3, "id", "sec")
        self.assertEqual(rows, [("럭키비키 뜻", '요즘 "밈" 정리')])

    def test_http_error_becomes_blocked_without_keys(self):
        import urllib.error
        err = urllib.error.HTTPError("https://openapi.naver.com", 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=err):
            with self.assertRaises(Blocked) as cm:
                web._backend_naver("q", 3, "SECRET_ID", "SECRET_KEY")
        self.assertNotIn("SECRET", str(cm.exception))


class RoundTests(unittest.TestCase):
    def setUp(self):
        HUB.reset()

    def test_round_stops_after_consecutive_failures(self):
        w, calls = make({"a": Blocked("HTTP 403")}, max_queries_per_round=10, min_gap_sec=0,
                        slang_queries=["s1", "s2", "s3", "s4"], wiki_fallback=False, slang_file="")
        w.mem = None
        w._save = lambda: asyncio.sleep(0)
        asyncio.run(w.refresh([]))
        self.assertLessEqual(len(calls), 2)        # 2번 연속 실패 후 나머지 검색어는 두드리지 않는다
        self.assertIn("건너뜀", w.last_round)


if __name__ == "__main__":
    unittest.main()
