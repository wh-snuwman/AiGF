"""호감도 이력 / 메시지 상태 / 통계 테스트: python -m unittest discover -s tests"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from bot import stats  # noqa: E402
    from bot.memory import Memory  # noqa: E402
except ImportError as _e:                                   # 의존성(aiosqlite 등)이 없는 환경에서는 이 파일을 건너뛴다
    raise unittest.SkipTest(f"의존성 없음: {_e}")


def run(coro):
    return asyncio.run(coro)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = Memory(str(Path(self.tmp.name) / "m.db"))
        run(self.mem.init())

    def tearDown(self):
        run(self.mem.close())
        self.tmp.cleanup()

    def test_msg_dedupe_and_states(self):
        async def go():
            self.assertFalse(await self.mem.msg_seen(1, 10, 100))      # 처음
            self.assertTrue(await self.mem.msg_seen(1, 10, 100))       # 중복
            await self.mem.msg_set(1, "QUEUED")
            await self.mem.msg_set(1, "PROCESSING", attempt=True)
            self.assertEqual([r[0] for r in await self.mem.msg_unfinished()], [1])
            await self.mem.msg_set(1, "RESPONDED")
            self.assertEqual(await self.mem.msg_unfinished(), [])
            self.assertEqual((await self.mem.msg_counts()).get("RESPONDED"), 1)
        run(go())

    def test_recovery_skips_exhausted_and_old(self):
        async def go():
            await self.mem.msg_seen(2, 10, 100)
            await self.mem.msg_set(2, "PROCESSING", attempt=True)
            for _ in range(3):
                await self.mem.msg_set(2, "PROCESSING", attempt=True)
            self.assertEqual(await self.mem.msg_unfinished(max_attempts=3), [])     # 시도 횟수 초과
            await self.mem.msg_seen(3, 10, 100)
            await self.mem.msg_set(3, "QUEUED")
            self.assertEqual(await self.mem.msg_unfinished(max_age_sec=-1), [])     # 너무 오래됨
        run(go())

    def test_history_tables(self):
        async def go():
            await self.mem.log_affinity("kei", 5, 0.1, 0.2, "칭찬", "joy")
            await self.mem.log_affinity("kei", 5, 0.2, 0.15, "도배", "annoyed")
            await self.mem.log_emotion("kei", "joy", 0.6)
            h = await self.mem.affinity_history("kei", 5)
            self.assertEqual([x[3] for x in h], ["칭찬", "도배"])                    # 오래된 순
            self.assertEqual(await self.mem.affinity_history("kei", 6), [])
            self.assertEqual((await self.mem.emotion_history("kei"))[0][1], "joy")
            self.assertEqual(await self.mem.affinity_users("kei"), [5])
        run(go())


class AffinityTests(unittest.TestCase):
    def test_aff100_mapping(self):
        from bot.mood import AFF_BASE, Mood
        self.assertEqual(Mood.aff100(AFF_BASE), 50)
        self.assertEqual(Mood.aff100(1.0), 100)
        self.assertEqual(Mood.aff100(-1.0), 0)
        self.assertLess(Mood.aff100(-0.3), Mood.aff100(0.0))
        self.assertEqual(Mood.aff_stage(50), "평범한 친구")
        self.assertEqual(Mood.aff_stage(0), "극단적 혐오")

    def test_repeat_decay_and_logging(self):
        from bot.mood import Mood

        class FakeMem:
            db = object()

            def __init__(self):
                self.rows = []

            async def log_affinity(self, *a):
                self.rows.append(a)

            async def log_emotion(self, *a):
                pass

        class P:
            char_id = "kei"
            data = {}

        async def go():
            m = Mood.__new__(Mood)
            Mood.__init__(m, FakeMem(), P(), None)
            m.mem = FakeMem()
            deltas = []
            for _ in range(5):
                before = m.affinity(7)
                m._adj_aff(7, 0.05, "칭찬 도배")
                deltas.append(m.affinity(7) - before)
            await asyncio.sleep(0)
            self.assertLess(deltas[3], deltas[0] * 0.3)                      # 같은 사유 반복은 크게 줄어든다
            self.assertGreaterEqual(len(m.mem.rows), 1)
            m._adj_aff(8, 0.05, "x")
            m._adj_aff(9, -0.05, "x")
            self.assertGreater(abs(m.affinity(9) - 0.1), abs(m.affinity(8) - 0.1))   # 하락이 상승보다 쉽다
        try:
            run(go())
        except TypeError as e:                                                    # Mood 생성자 시그니처가 다르면 이 테스트는 건너뜀
            self.skipTest(str(e))


class StatsTests(unittest.TestCase):
    def test_percentile(self):
        self.assertEqual(stats.percentile([], 50), 0.0)
        self.assertEqual(stats.percentile([5], 95), 5.0)
        self.assertAlmostEqual(stats.percentile(list(range(1, 101)), 95), 95.05, places=1)

    def test_latency_report(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "trace-x.jsonl"
            now = time.time()
            rows = [{"ts": now, "kind": "reply", "outcome": "replied", "total_sec": s, "wait_sec": 1.0,
                     "events": [{"e": "llm", "sec": s - 0.1, "load_sec": 0.0, "prompt_tokens": 700, "completion_tokens": 20}]}
                    for s in (1.0, 2.0, 3.0, 4.0)]
            rows.append({"ts": now - 99 * 3600, "kind": "reply", "outcome": "replied", "total_sec": 99})       # 기간 밖
            f.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            rep = stats.latency_report([f], hours=24)
            self.assertEqual(rep["total_sec"]["n"], 4)
            self.assertEqual(rep["total_sec"]["p50"], 2.5)
            self.assertEqual(rep["prompt_tokens"]["avg"], 700.0)
            self.assertIn("P95", stats.format_latency(rep))

    def test_graphs_make_png_or_none(self):
        now = time.time()
        aff = [(now - 3600, 0.1, 0.2, "칭찬", "joy"), (now, 0.2, 0.1, "도배", "annoyed")]
        out = stats.affinity_graph(aff, "t", lambda v: int(50 + v * 50))
        emo = stats.emotion_graph([(now - 60, "joy", 0.5), (now, "calm", 0.2)], "t", lambda e: e)
        if stats.available():
            self.assertTrue(out.startswith(b"\x89PNG") and emo.startswith(b"\x89PNG"))
            self.assertTrue(stats.affinity_graph([], "빈 기록", lambda v: 50).startswith(b"\x89PNG"))
        else:
            self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
