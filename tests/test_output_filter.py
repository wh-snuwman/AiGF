"""출력 정리 테스트: 다른 언어 토큰 걷어내기"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "bot", "chat.py"), encoding="utf-8").read()
NS = {"re": re}
exec(SRC[SRC.index("_F = ("):SRC.index("def humanize(")], NS)   # chat.py 는 discord 등 의존성이 있어 함수만 꺼내 시험한다
strip_foreign = NS["strip_foreign"]


class ForeignTests(unittest.TestCase):
    def test_strips_cyrillic_and_cjk(self):
        self.assertEqual(strip_foreign("응 뭐 спец"), "응 뭐")
        self.assertEqual(strip_foreign("日本語だよ 진짜"), "진짜")
        self.assertEqual(strip_foreign("hello 世界 ㅋㅋ"), "hello ㅋㅋ")

    def test_keeps_korean_english(self):
        for t in ("게임 하자 GG", "ㅋㅋ ok", "롤 티어 몇이야?"):
            self.assertEqual(strip_foreign(t), t)


if __name__ == "__main__":
    unittest.main()
