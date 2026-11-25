"""스케줄 / 호감도 단계 / 이미지 프롬프트 테스트"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import yaml
    from bot.schedule import Schedule
    from bot.visual import build
    from bot.mood import EMO, Mood
    from bot.persona import Persona
except ImportError as _e:
    raise unittest.SkipTest(f"의존성 없음: {_e}")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(cid):
    with open(os.path.join(ROOT, "config", "characters", f"{cid}.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


class ScheduleTests(unittest.TestCase):
    def test_midnight_wrap_and_sleep(self):
        s = Schedule(load("eunlang")["schedule"])
        self.assertTrue(s.asleep(datetime(2026, 11, 23, 9, 0)))          # 06~14 자는 중
        self.assertFalse(s.asleep(datetime(2026, 11, 23, 23, 30)))
        self.assertEqual(s.current(datetime(2026, 11, 23, 2, 0)).place, "hideout")    # 23~06 자정 넘김 구간
        self.assertIn("아지트", s.line(datetime(2026, 11, 23, 20, 0)))

    def test_weekday_weekend(self):
        s = Schedule(load("kei")["schedule"])
        mon, sun = datetime(2026, 11, 23, 10, 0), datetime(2026, 11, 22, 10, 0)    # 월 / 일
        self.assertEqual(s.current(mon).place, "school")
        self.assertEqual(s.current(sun).place, "home")

    def test_characters_differ(self):
        t = datetime(2026, 11, 23, 9, 30)
        self.assertNotEqual(Schedule(load("eunlang")["schedule"]).asleep(t), Schedule(load("senko")["schedule"]).asleep(t))

    def test_defaults_and_bad_blocks(self):
        s = Schedule(None)
        self.assertTrue(s.asleep(datetime(2026, 1, 1, 4, 0)))
        bad = Schedule({"blocks": [{"at": "bad"}, {"at": "10:00-12:00", "do": "x"}]})   # 잘못된 블록은 건너뜀
        self.assertEqual(len(bad.blocks), 1)
        self.assertEqual(Schedule({"blocks": [{"at": "x"}]}).blocks[0].do, "자는 중")     # 전부 잘못이면 기본값

    def test_effects_clamped(self):
        s = Schedule({"blocks": [{"at": "00:00-24:00", "chat": 99, "length": 0, "energy": 9}]})
        e, c, l = s.effects(datetime(2026, 1, 1, 1, 0))
        self.assertEqual((e, c, l), (0.5, 2.0, 0.3))

    def test_all_shipped_character_schedules_parse(self):
        for cid in ("eunlang", "kei", "senko"):
            sc = load(cid)["schedule"]
            self.assertEqual(len(Schedule(sc).blocks), len(sc["blocks"]), cid)


class AffinityStageTests(unittest.TestCase):
    def test_seven_stages(self):
        names = [Mood.aff_stage(x) for x in (0, 10, 30, 50, 70, 90, 100)]
        self.assertEqual(len(set(names)), 7)
        self.assertEqual(names[0], "극단적 혐오")
        self.assertEqual(Mood.aff_label(1.0)[0], "서로 좋아함")
        self.assertEqual(Mood.aff_label(0.1)[0], "평범한 친구")            # 기본값은 보통 관계


class CharacterRelationshipTests(unittest.TestCase):
    def test_relationship_is_reciprocal_and_matched_by_id(self):
        kei, aris = Persona("kei"), Persona("aris")
        self.assertIn("아리스", kei.peer_relationship("aris"))
        self.assertIn("케이", aris.peer_relationship("kei"))
        self.assertEqual(kei.peer_relationship("midori"), "")
        self.assertEqual(Persona("midori").peer_relationship("kei"), "")

    def test_kei_mood_displays_declared_register(self):
        mood = Mood(None, Persona("kei"), None)
        self.assertIn("존댓말", mood.knobs())
        self.assertNotIn("반말", mood.knobs())


class VisualTests(unittest.TestCase):
    def test_thirty_expressions(self):
        import yaml as y
        with open(os.path.join(ROOT, "config", "visual_common.yaml"), encoding="utf-8") as f:
            c = y.safe_load(f)
        self.assertEqual(set(c["expressions"]), set(EMO))                 # 감정 30종 전부 표정이 있다

    def test_compose(self):
        pos, neg = build(load("senko"), "shy", "shrine", "cook", None, 8)
        for t in ("fox ears", "blush", "shinto shrine", "steam", "morning"):
            self.assertIn(t, pos)
        self.assertIn("nsfw", neg)

    def test_character_places_are_not_shared(self):
        pos, _ = build(load("eunlang"), "calm", "shrine")                  # 은랑 세계관에 없는 신사는 추가되지 않는다
        self.assertNotIn("shinto", pos)

    def test_fallback_without_visual(self):
        pos, _ = build({"profile": "테스트 캐릭터"}, "joy")
        self.assertIn("테스트 캐릭터", pos)


if __name__ == "__main__":
    unittest.main()
