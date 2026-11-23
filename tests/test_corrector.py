"""문법 교정 보조 모델 테스트 (실제 모델 없이 가짜 클라이언트로)"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import corrector as c  # noqa: E402


class FakeClient:
    def __init__(self, reply=None, exc=None, delay=0.0):
        self.reply, self.exc, self.delay, self.calls = reply, exc, delay, 0

    async def chat(self, **kw):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return {"message": {"content": self.reply}}


class NotFound(Exception):
    status_code = 404


def make(client, **cfg):
    x = c.Corrector({"timeout_sec": 0.3, **cfg})
    x._client = client
    return x


def run(coro):
    return asyncio.run(coro)


class AcceptTests(unittest.TestCase):
    def test_accepts_small_fix(self):
        self.assertTrue(c.accept("오늘 기분이 좋아 졌어", "오늘 기분이 좋아졌어"))

    def test_rejects_unchanged_and_empty(self):
        self.assertFalse(c.accept("안녕 뭐해", "안녕 뭐해"))
        self.assertFalse(c.accept("안녕 뭐해", ""))

    def test_rejects_meaning_change_or_length(self):
        self.assertFalse(c.accept("게임 하자", "오늘 저녁에는 친구들과 함께 영화를 보러 가는 것이 좋겠습니다"))
        self.assertFalse(c.accept("그래 알겠어 그럼 내일 봐", "응"))

    def test_rejects_foreign_script_and_meta(self):
        self.assertFalse(c.accept("뭐 해", "뭐 спец"))
        self.assertFalse(c.accept("뭐 해 지금", "수정한 문장: 뭐 해 지금?"))

    def test_rejects_line_count_change(self):
        self.assertFalse(c.accept("뭐 해\n지금 어디야", "뭐 해 지금 어디야"))

    def test_register_detection(self):
        self.assertEqual(c.declared_register("- 건조한 반말. 힘을 뺀 말투"), "반말")
        self.assertEqual(c.declared_register("- 기본은 사무적인 해요체 존댓말"), "존댓말")
        self.assertEqual(c.declared_register("- 존댓말은 절대 쓰지 않는다 반말"), "반말")


class FixTests(unittest.TestCase):
    def test_applies_good_fix(self):
        x = make(FakeClient("오늘 기분이 좋아졌어"))
        self.assertEqual(run(x.fix("오늘 기분이 좋아 졌어", "반말")), "오늘 기분이 좋아졌어")
        self.assertEqual(x.stats["changed"], 1)

    def test_strips_prefix_and_quotes(self):
        x = make(FakeClient('수정: "오늘 기분이 좋아졌어"'))
        self.assertEqual(run(x.fix("오늘 기분이 좋아 졌어")), "오늘 기분이 좋아졌어")

    def test_bad_output_falls_back_to_original(self):
        x = make(FakeClient("전혀 다른 긴 문장을 만들어서 내보냅니다 이건 아주 길어요 정말로"))
        self.assertEqual(run(x.fix("오늘 기분이 좋아 졌어")), "오늘 기분이 좋아 졌어")
        self.assertEqual(x.stats["rejected"], 1)

    def test_timeout_falls_back_and_pauses_after_streak(self):
        x = make(FakeClient("고침", delay=1.0), timeout_sec=0.05)
        for _ in range(5):
            self.assertEqual(run(x.fix("오늘 기분이 좋아 졌어")), "오늘 기분이 좋아 졌어")
        self.assertFalse(x.enabled)                              # 연속 시간 초과면 잠시 쉰다
        cli = x._client
        calls = cli.calls
        run(x.fix("오늘 기분이 좋아 졌어"))
        self.assertEqual(cli.calls, calls)                       # 쉬는 동안은 호출하지 않는다

    def test_missing_model_disables_quietly(self):
        x = make(FakeClient(exc=NotFound("model not found")))
        self.assertEqual(run(x.fix("오늘 기분이 좋아 졌어")), "오늘 기분이 좋아 졌어")
        self.assertFalse(x.enabled)

    def test_skips_short_skip_foreign_and_disabled(self):
        cli = FakeClient("x")
        x = make(cli)
        for t in ("응", "SKIP", "hello world", ""):
            run(x.fix(t))
        self.assertEqual(cli.calls, 0)
        y = make(cli, enabled=False)
        run(y.fix("오늘 기분이 좋아 졌어"))
        self.assertEqual(cli.calls, 0)
        self.assertIn("꺼짐", y.summary())


if __name__ == "__main__":
    unittest.main()
