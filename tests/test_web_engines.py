"""웹 검색 엔진 자동 전환 테스트: python -m unittest discover -s tests"""
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import web  # noqa: E402
from bot.web import HUB, WebInfo  # noqa: E402


def make(script):
    """script: {엔진: 결과 리스트 | 예외}. 호출 기록을 calls 에 남긴다."""
    w = WebInfo({"backends": list(script)}, None)
    calls: list[str] = []

    def fake(query, n, backend):
        calls.append(backend)
        v = script[backend]
        if isinstance(v, list) and v and isinstance(v[0], Exception):
            v = v.pop(0)
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

    def test_failover_and_cooldown(self):
        w, calls = make({"a": OSError("boom"), "b": [("t", "body")]})
        rows, eng, _ = run(w)
        self.assertEqual(eng, "b")
        self.assertGreater(HUB.engines["a"]["bad_until"], time.time())
        calls.clear()
        run(w, "q2")
        self.assertEqual(calls, ["b"])            # 쉬는 중인 a 는 건너뜀

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
        w, calls = make({"a": []})
        seq = iter([err, [("t", "b")]])
        calls.clear()

        def fake(q, n, b):
            calls.append(b)
            v = next(seq)
            if isinstance(v, Exception):
                raise v
            return v

        w._search_one = fake
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


if __name__ == "__main__":
    unittest.main()
