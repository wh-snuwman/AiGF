"""토큰 예산 / 벤치 계산 / TTFT 통계 테스트"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bot import budget  # noqa: E402

try:
    from bot import stats  # noqa: E402
except ImportError:
    stats = None


def load_bench():
    spec = importlib.util.spec_from_file_location("bench_mod", os.path.join(ROOT, "scripts", "bench.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class BudgetTests(unittest.TestCase):
    def setUp(self):
        budget.reset_calibration()

    def test_estimate_by_script(self):
        c = {"hangul": 1.0, "ascii": 0.25, "other": 0.5, "per_message": 0}
        self.assertEqual(budget.estimate("가나다라", c), 4)
        self.assertEqual(budget.estimate("abcd", c), 1)
        self.assertEqual(budget.estimate("", c), 0)

    def test_fit_drops_oldest_keeps_current(self):
        hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": "가" * 200} for i in range(11)]
        out, info = budget.fit("시스템", [], hist, budget=600, keep_last=4)
        self.assertLess(len(out), len(hist))
        self.assertEqual(out[-1], hist[-1])                          # 현재(마지막) 메시지는 남는다
        self.assertGreaterEqual(len(out), 4)                         # 최근 keep_last 개 보호
        self.assertTrue(out[0]["role"] == "user" or len(out) == 4)   # 보호 구간 밖이면 맨 앞이 assistant 로 시작하지 않는다
        self.assertEqual(info["dropped"], len(hist) - len(out))
        self.assertLess(info["after"], info["before"])

    def test_fit_noop_when_under_budget_or_off(self):
        hist = [{"role": "user", "content": "안녕"}]
        self.assertEqual(budget.fit("s", [], hist, 5000)[0], hist)
        long = [{"role": "user", "content": "가" * 500}] * 6
        self.assertEqual(len(budget.fit("s", [], long, 0)[0]), 6)    # 예산 0 = 끔

    def test_calibration_file_overrides_default(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "calib.json"
            f.write_text(json.dumps({"hangul": 1.5, "bogus": 9}), encoding="utf-8")
            old = budget.CALIB_FILE
            budget.CALIB_FILE = f
            try:
                budget.reset_calibration()
                self.assertEqual(budget.calibration()["hangul"], 1.5)
                self.assertNotIn("bogus", budget.calibration())
            finally:
                budget.CALIB_FILE = old
                budget.reset_calibration()


class BenchTests(unittest.TestCase):
    def test_slope_ignores_fixed_overhead(self):
        bench = load_bench()
        pts = [(100, 30 + 70), (300, 30 + 210), (600, 30 + 420)]      # 글자당 0.7 + 고정 30
        self.assertAlmostEqual(bench.fit_slope(pts), 0.7, places=3)
        self.assertEqual(bench.fit_slope([(1, 1)]), 0.0)

    def test_dist_and_compare(self):
        bench = load_bench()
        a = {"ttft": bench.dist([2.0, 2.0, 2.0])}
        b = {"ttft": bench.dist([1.0, 1.0, 1.0])}
        self.assertEqual(a["ttft"]["p95"], 2.0)
        self.assertTrue(any("-50.0%" in ln for ln in bench.compare(b, a)))


@unittest.skipIf(stats is None, "의존성 없음")
class TtftTests(unittest.TestCase):
    def test_ttft_in_latency_report(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "t.jsonl"
            r = {"ts": time.time(), "kind": "reply", "outcome": "replied", "total_sec": 1.5,
                 "events": [{"e": "llm", "sec": 1.4, "load_sec": 0.2, "prompt_sec": 0.3, "prompt_tokens": 800, "completion_tokens": 20}]}
            f.write_text(json.dumps(r), encoding="utf-8")
            rep = stats.latency_report([f], 24)
            self.assertEqual(rep["ttft_sec"]["avg"], 0.5)
            self.assertIn("TTFT", stats.format_latency(rep))


if __name__ == "__main__":
    unittest.main()
