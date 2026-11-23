"""캐릭터별 글 스타일 테스트: 말줄임표/느낌표/웃음/머뭇거림, 기호 규칙, 후처리"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from bot import chat as C
    from bot import textstyle as T
    from bot.persona import Persona
except ImportError as _e:
    raise unittest.SkipTest(f"의존성 없음: {_e}")

PLAIN = T.from_data(None)
TIMID = T.from_data({"ellipsis": True, "ellipsis_rate": 1.0, "laugh": "none", "hesitate": ["저기..."], "hesitate_rate": 1.0})
EXCL = T.from_data({"exclaim": True, "exclaim_rate": 1.0, "tilde": True})


class HumanizeTests(unittest.TestCase):
    def test_default_strips_symbols(self):
        self.assertEqual(C.humanize("아... 그래요!!"), "아 그래요")
        self.assertTrue(PLAIN.plain)

    def test_ellipsis_kept_for_timid(self):
        self.assertEqual(C.humanize("저기…죄송해요......", style=TIMID), "저기... 죄송해요...")
        self.assertEqual(C.humanize("네...?", style=TIMID), "네...?")

    def test_exclaim_and_tilde_for_excited(self):
        self.assertEqual(C.humanize("좋아요!!!!", style=EXCL), "좋아요!!")
        self.assertEqual(C.humanize("좋아요~~~~", style=EXCL), "좋아요~~")
        self.assertEqual(C.humanize("뭐?!", style=EXCL), "뭐?")
        self.assertEqual(C.humanize("좋아요!!!"), "좋아요")                # 스타일이 없으면 지운다

    def test_history_cleaning_does_not_destroy_style(self):
        self.assertIn("...", C.humanize("아... 그래요", style=TIMID))


class FlavorTests(unittest.TestCase):
    def test_hesitation_and_trailing_ellipsis(self):
        r = random.Random(1)
        out = T.flavor("죄송해요", TIMID, "shy", True, True, r)
        self.assertTrue(out.startswith("저기...") and out.endswith("..."))

    def test_no_double_marks(self):
        r = random.Random(1)
        self.assertEqual(T.flavor("뭐예요?", TIMID, "", False, True, r), "뭐예요?")
        self.assertEqual(T.flavor("아... 그래요...", TIMID, "", True, True, random.Random(2)).count("..."), 2)

    def test_exclaim_added_when_excited(self):
        self.assertEqual(T.flavor("좋아요", EXCL, "joy", True, True, random.Random(3)), "좋아요!")
        self.assertEqual(T.flavor("좋아요", PLAIN, "joy", True, True), "좋아요")      # 스타일 없으면 그대로

    def test_rates_are_probabilistic(self):
        s = T.from_data({"exclaim": True, "exclaim_rate": 0.3})
        r = random.Random(7)
        n = sum(T.flavor("좋아요", s, "", True, True, r).endswith("!") for _ in range(1000))
        self.assertTrue(200 < n < 400, n)

    def test_strip_laugh(self):
        self.assertEqual(T.strip_laugh("진짜 웃기다 ㅋㅋㅋ"), "진짜 웃기다")
        self.assertEqual(T.strip_laugh("ㅋㅋ"), "")


class PromptRuleTests(unittest.TestCase):
    def test_symbol_rule_matches_style(self):
        plain = T.symbol_rule(PLAIN)
        self.assertIn("느낌표", plain)
        self.assertIn("ㅋㅋ ㅇㅇ", plain)
        timid = T.symbol_rule(TIMID)
        self.assertIn("말줄임표", timid)
        self.assertIn("ㅋㅋ는 쓰지 않는다", timid)
        self.assertNotIn("느낌표 금지", T.symbol_rule(EXCL).replace("느낌표 금지(", "X"))

    def test_character_prompts_differ(self):
        yuzu = Persona("yuzu").static_prompt()
        eun = Persona("eunlang").static_prompt()
        self.assertIn("말줄임표(...)", yuzu)
        self.assertNotIn("말줄임표(...)", eun)
        self.assertNotIn("{symbol_rule}", yuzu)

    def test_shipped_styles_load(self):
        for cid in ("yuzu", "aris", "momoi", "midori"):
            self.assertFalse(T.from_data(Persona(cid).data.get("style")).plain, cid)


if __name__ == "__main__":
    unittest.main()
