"""서버 이모지 빈도 제한 테스트 (chat.py 는 의존성이 많아 필요한 부분만 꺼내 시험한다)"""
import os
import random
import re
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "bot", "chat.py"), encoding="utf-8").read()
NS = {"re": re, "random": random, "time": time}
exec(SRC[SRC.index("CUSTOM_EMOJI_RE = "):SRC.index("def strip_foreign(")], NS)
a = SRC.index("    def _limit_custom_emoji(")
b = SRC.index("\n    # ----", a)
exec("class E:\n" + SRC[a:b], NS)
E = NS["E"]


def make(rate=1.0, gap=300):
    e = E()
    e.s = {"emoji": {"text_rate": rate, "min_gap_sec": gap}}
    e._custom_emoji_ts = 0.0
    return e


class EmojiLimitTests(unittest.TestCase):
    def test_at_most_one_per_reply(self):
        out = make()._limit_custom_emoji(["ㅋㅋ :smile:", "좋아 :heart: :fire:"])
        self.assertEqual(sum(len(NS["CUSTOM_EMOJI_RE"].findall(p)) for p in out), 1)

    def test_gap_blocks_next(self):
        e = make()
        e._limit_custom_emoji(["ㅋㅋ :smile:"])
        self.assertEqual(e._limit_custom_emoji(["또 :smile:"]), ["또"])

    def test_rate_zero_strips_all(self):
        self.assertEqual(make(rate=0)._limit_custom_emoji([":smile:", "응 <:cat:12345>"]), ["", "응"])

    def test_time_not_touched(self):
        self.assertEqual(make(rate=0)._limit_custom_emoji(["12:30:45 에 보자"]), ["12:30:45 에 보자"])


if __name__ == "__main__":
    unittest.main()
