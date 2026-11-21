"""유행어 힌트 테스트: python -m unittest discover -s tests"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.slang import Slang  # noqa: E402


class SlangTests(unittest.TestCase):
    def test_dictionary_loads_about_300(self):
        s = Slang()
        s.load()
        self.assertGreaterEqual(len(s.entries), 300)
        words = [w for w, _m, _t in s.entries]
        self.assertEqual(len(words), len(set(words)))          # 중복 없음
        self.assertTrue(all(m for _w, m, _t in s.entries))     # 뜻이 다 있음

    def test_hint_is_rare(self):
        s = Slang({"chance": 0.22})
        rng = random.Random(1)
        n = sum(bool(s.hint("배고프다", "calm", [], rng=rng)) for _ in range(1000))
        self.assertTrue(120 < n < 330, n)                      # 대부분은 힌트 없음 (조금씩)

    def test_picks_fit_situation(self):
        s = Slang()
        s.load()
        rng = random.Random(3)
        picks = s.pick("배고파 야식 뭐 먹지", "calm", n=3, rng=rng)
        tags = {w: t for w, _m, t in s.entries}
        self.assertTrue(any("음식" in tags[w] for w, _m in picks))

    def test_cooldown_after_using_hinted_word(self):
        s = Slang({"chance": 1.0})
        rng = random.Random(5)
        line = s.hint("ㅋㅋㅋ 개웃기네", "joy", [], rng=rng)
        self.assertTrue(line)
        used = next(w for w in s._recent if len(w.replace("~", "").strip()) >= 2)
        self.assertEqual(s.hint("ㅋㅋ", "joy", [f"아 {used} 진짜"], rng=rng), "")

    def test_quiet_when_sad(self):
        s = Slang({"chance": 0.22})
        rng = random.Random(2)
        n = sum(bool(s.hint("우울해", "sad", [], rng=rng)) for _ in range(1000))
        self.assertLess(n, 100)

    def test_disabled(self):
        self.assertEqual(Slang({"enabled": False, "chance": 1.0}).hint("ㅋㅋ"), "")

    def test_no_recent_repeat(self):
        s = Slang({"per_hint": 3, "recent_hint_size": 40})
        rng = random.Random(7)
        a = [w for w, _ in s.pick("ㅋㅋ", "joy", rng=rng)]
        b = [w for w, _ in s.pick("ㅋㅋ", "joy", rng=rng)]
        self.assertFalse(set(a) & set(b))


if __name__ == "__main__":
    unittest.main()
