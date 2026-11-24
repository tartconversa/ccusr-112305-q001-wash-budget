"""配额可用量、防重复占用、合洗约定与排程约束测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest

from washstation.core import CURRENT_DAY, load_repository
from washstation.planning import (Assignment, co_wash_allowed,
                                  compare_pickup_scenarios, verify_plan)
from washstation.quota import availability


class AvailabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = load_repository()

    def test_new_shift_quota_is_14(self):
        av = availability(self.repo)
        self.assertEqual(av["quota_l"], 14)
        self.assertEqual(av["basket_capacity"], 12)
        self.assertTrue(av["meter_gap"])
        self.assertIsNone(av["certified_available_l"])

    def test_late_reading_creates_prudent_range_not_double_spend(self):
        av = availability(self.repo)
        # 只把新水表候选段 13L 作为待核实占用准备，回用水表同形差分不计入
        self.assertEqual(av["late_reading_encumbrance_l"], 13)
        self.assertEqual(av["schedulable"]["value"], 14)
        self.assertEqual((av["prudent_available"]["lo"],
                          av["prudent_available"]["hi"]), (1, 14))

    def test_confirmed_assignment_occupies_quota_once(self):
        from washstation.station import Station
        st = Station()
        a = Assignment(["B141"], CURRENT_DAY, 0, "节水", reuse_l=7, ref="T1")
        result = st.book.confirm(st.repo, a)
        self.assertTrue(result["confirmed"])
        av = st.new_shift_availability()
        # 需水区间 10..14 被占用 -> 剩余 0..4，不会再被第二张批重复使用
        self.assertEqual((av["schedulable"]["lo"], av["schedulable"]["hi"]), (0, 4))
        again = st.book.confirm(
            st.repo, Assignment(["B142"], CURRENT_DAY, 0, "单洗", ref="T2"))
        self.assertFalse(again["confirmed"])


class CoWashAgreementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = load_repository()

    def test_normal_baskets_may_cowash(self):
        bs = [self.repo.basket("B140"), self.repo.basket("B141")]
        self.assertTrue(co_wash_allowed(bs, "节水")["allowed"])

    def test_single_wash_basket_cannot_merge(self):
        bs = [self.repo.basket("B141"), self.repo.basket("B142")]
        verdict = co_wash_allowed(bs, "节水")
        self.assertFalse(verdict["allowed"])
        self.assertIn("单洗", verdict["reason"])

    def test_different_soil_level_changes_release_condition(self):
        baskets = [{"soil": "普通"}, {"soil": "重污"}]
        v = co_wash_allowed(baskets, "常用")
        self.assertTrue(v["allowed"])
        self.assertTrue(v.get("conditional"))
        self.assertIn("重污", v["release_condition"])


class PlanVerificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = load_repository()

    def test_slot_without_quota_cannot_be_confirmed(self):
        a = Assignment(["B141"], "2026-04-12", 2, "节水")
        v = verify_plan(self.repo, [a])
        self.assertFalse(v["feasible"])
        self.assertTrue(any(p["level"] == "quota" for p in v["problems"]))

    def test_capacity_violation_detected(self):
        # B141(11) + B142(12) = 23 > slot0 cap 12
        a = Assignment(["B141", "B142"], "2026-04-12", 0, "节水")
        v = verify_plan(self.repo, [a])
        self.assertTrue(any(p["level"] == "basket_time" for p in v["problems"]))

    def test_machine_double_booking_detected(self):
        a1 = Assignment(["B141"], "2026-04-12", 0, "节水", "A")
        a2 = Assignment(["B142"], "2026-04-12", 0, "单洗", "A")
        v = verify_plan(self.repo, [a1, a2])
        self.assertTrue(any(p["level"] == "machine" for p in v["problems"]))

    def test_reuse_makes_normal_batch_robustly_fit(self):
        a = Assignment(["B141"], "2026-04-12", 0, "节水", reuse_l=7)
        v = verify_plan(self.repo, [a])
        slot_q = v["slots"][0]["quota"]
        self.assertEqual((slot_q["total_demand"]["lo"],
                          slot_q["total_demand"]["hi"]), (10, 14))
        self.assertTrue(v["feasible"])
        self.assertTrue(v["robust"])

    def test_without_reuse_slot0_not_even_nominal(self):
        a = Assignment(["B141"], "2026-04-12", 0, "节水")
        v = verify_plan(self.repo, [a])
        self.assertFalse(v["feasible"])
        self.assertTrue(any(p["level"] == "water" for p in v["problems"]))

    def test_scenario_comparison_shape(self):
        sc = compare_pickup_scenarios(self.repo, certified_reuse_l=7,
                                      physical_certified_reuse_l=0)
        names = set(sc["scenarios"])
        self.assertIn("B_合洗尝试_slot0", names)
        self.assertIn("C_许可回用水加延迟单洗", names)
        self.assertIn("C2_罐段实物许可口径", names)
        # 强行合洗普通+单洗必然被约定拦截
        b = sc["scenarios"]["B_合洗尝试_slot0"]["verification"]
        self.assertTrue(any(p["level"] == "agreement" for p in b["problems"]))
        # 账面许可回用水让普通篮可行；罐段待测口径不可行
        c = sc["scenarios"]["C_许可回用水加延迟单洗"]["same_shift_verification"]
        c2 = sc["scenarios"]["C2_罐段实物许可口径"]["same_shift_verification"]
        self.assertTrue(c["robust"])
        self.assertFalse(c2["feasible"])


class ImmutableHandoverTest(unittest.TestCase):
    def test_handed_over_basket_cannot_be_rescheduled(self):
        from washstation.station import Station
        st = Station()
        self.assertIn("B140", st.locked_baskets)
        result = st.book.confirm(
            st.repo, Assignment(["B140"], "2026-04-12", 0, "节水", ref="override"))
        self.assertFalse(result["confirmed"])
        self.assertIn("交接", result["reason"])


if __name__ == "__main__":
    unittest.main()
