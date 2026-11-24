"""计量差分、换表/缺测/迟报冲突与水量归属的规则测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest

from washstation.core import Range, load_repository
from washstation.meters import (fresh_day_range, individual_meter_days,
                                meter_windows, register_anomalies,
                                unanchored_segments)
from washstation.water import attribute_day, estimate_and_validate


class MeterChainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = load_repository()

    def test_normal_day_exact_delta(self):
        fr = fresh_day_range(self.repo, "2026-04-11")
        self.assertTrue(fr["range"]["exact"])
        self.assertEqual(fr["range"]["value"], 60)
        # 三个时段差分 19/20/21
        measured = [w["litres"] for w in fr["windows"] if w["status"] == "measured"]
        self.assertEqual(measured, [19, 20, 21])

    def test_meter_name_change_is_independent_base(self):
        for day, partial in [("2026-01-24", 37), ("2026-03-21", 33)]:
            fr = fresh_day_range(self.repo, day)
            rng = fr["range"]
            self.assertFalse(rng["exact"], f"{day} 换表日必须是区间")
            self.assertEqual(rng["lo"], partial)
            # 边界 slot2 不能相减，上界取该时段配额 22
            self.assertEqual(rng["hi"], partial + 22)
            self.assertTrue(any(u["status"] == "meter_change"
                                for u in fr["uncertain_windows"]))

    def test_null_readings_are_gaps_not_zero(self):
        ind = individual_meter_days(self.repo)
        for day in ("2026-01-31", "2026-03-14", "2026-04-11"):
            self.assertFalse(ind[day]["measured"])
            self.assertIsNone(ind[day]["litres"])
        measured_days = [d for d, v in ind.items() if v["measured"]]
        self.assertGreaterEqual(len(measured_days), 8)

    def test_register_backtrack_and_late_candidate_detected(self):
        anomalies = register_anomalies(self.repo)
        backtracks = [a for a in anomalies if a["type"] == "register_backtrack"]
        self.assertTrue(any(a["edge"] == ["NE11", "M1300"] for a in backtracks))
        late = [a for a in anomalies if a["type"] == "late_or_misdated_candidate"]
        ne10 = next(a for a in late if a["reading"] == "NE10")
        ne11 = next(a for a in late if a["reading"] == "NE11")
        # 数值上 NE10 接 M1330（+2），NE11 再接 +13
        self.assertEqual(ne10["numeric_predecessor"], "M1330")
        self.assertEqual(ne10["hypothetical_delta"], 2)
        self.assertEqual(ne11["hypothetical_delta"], 13)

    def test_unanchored_segment_internal_delta(self):
        segs = unanchored_segments(self.repo)
        day0405 = [s for s in segs if s["day"] == "2026-04-05"]
        meters = {s["meter"]: s for s in day0405}
        self.assertEqual(
            [w["litres"] for w in meters["new-a"]["windows"] if w["status"] == "measured"],
            [13])

    def test_range_arithmetic_keeps_uncertainty(self):
        r = Range(37, 59) + Range.exact(18)
        self.assertEqual((r.lo, r.hi), (55, 77))
        sub = Range.exact(14) - Range(10, 14)
        self.assertEqual((sub.lo, sub.hi), (0, 4))


class AttributionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = load_repository()

    def test_attribution_ties_to_meter_exact_day(self):
        acc = attribute_day(self.repo, "2026-04-11")
        self.assertTrue(acc["attribution_ties_to_meter"])
        # slot0 19L 无洗护批 -> 未归属；slot1=20(C141), slot2=21(C142)
        self.assertEqual(acc["fresh_unattributed"]["value"], 19)
        slots = {c["slot"]: c for c in acc["cycles"]}
        self.assertEqual(slots[1]["fresh_l"]["value"], 20)
        self.assertEqual(slots[2]["fresh_l"]["value"], 21)

    def test_attribution_ties_on_meter_change_day(self):
        acc = attribute_day(self.repo, "2026-01-24")
        self.assertTrue(acc["attribution_ties_to_meter"])
        self.assertFalse(acc["fresh_meter_total"]["exact"])

    def test_shared_cycle_desk_is_range_not_fake_split(self):
        acc = attribute_day(self.repo, "2026-04-11")
        c141 = next(c for c in acc["cycles"] if c["cycle"] == "C141")
        self.assertEqual(set(c141["desks"]), {"东柜", "西柜"})
        for desk, info in c141["desk_actual_ranges"].items():
            # 共享批总量 20L 可确定，但单个柜台只能给 0..20 的归属区间
            self.assertEqual(info["actual_range"]["lo"], 0)
            self.assertEqual(info["actual_range"]["hi"], 20)
            self.assertFalse(info["actual_range"]["exact"])
            self.assertEqual(info["basis"], "shared_cycle_no_submeter")
        # 数量比例估算存在但明确带警告，且不等于“实际”
        self.assertIsNotNone(c141["quantity_proportional_estimate_l"])
        self.assertIn("估算", c141["estimate_warning"])

    def test_reuse_gap_is_unattributed(self):
        acc = attribute_day(self.repo, "2026-04-11")
        self.assertEqual(acc["reuse_meter_total_l"], 21)
        self.assertEqual(acc["reuse_recorded_on_cycles_l"], 3)
        self.assertEqual(acc["reuse_unattributed_l"], 18)

    def test_distribution_by_category(self):
        acc = attribute_day(self.repo, "2026-04-11")
        dist = acc["distribution"]
        self.assertEqual(dist["item_count"], 33)
        self.assertEqual(dist["by_soil"]["单洗"]["items"], 12)
        self.assertEqual(dist["by_desk"]["东柜"]["items"], 10)

    def test_estimate_validation_reports_errors_and_factors(self):
        ev = estimate_and_validate(self.repo)
        self.assertIsNotNone(ev["mae_l"])
        self.assertIsNotNone(ev["independent_meter_mae_l"])
        text = " ".join(ev["interpretation"])
        for kw in ("季节", "程序更新", "批量", "独立支路"):
            self.assertIn(kw, text)
        self.assertGreaterEqual(len(ev["independent_meter_validation"]), 8)


if __name__ == "__main__":
    unittest.main()
