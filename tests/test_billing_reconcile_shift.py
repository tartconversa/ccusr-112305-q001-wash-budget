"""计费版本/分摊争议、批次四方核对与下一班归因测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest

from washstation.core import load_repository
from washstation.billing import (allocate_shared_bill,
                                 counter_billing_actions,
                                 month_end_property_bill,
                                 shared_pre_rinse_allocation)
from washstation.reconcile import reconcile_batch
from washstation.shift import classify_anomalies, receive_shift_log


class BillingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = load_repository()

    def test_ut1_correction_requires_reissue_and_backcharge_2l(self):
        actions = {a["statement"]: a for a in counter_billing_actions(self.repo)["actions"]}
        ut1 = actions["UT1"]
        self.assertEqual(ut1["original_billed_l"], 12)
        self.assertEqual(ut1["latest_billed_l"], 14)
        self.assertEqual(ut1["delta_l"], 2)
        self.assertTrue(ut1["needs_reissue"])
        self.assertEqual(ut1["backcharge"]["litres"], 2)
        self.assertAlmostEqual(ut1["backcharge"]["amount"], 0.0122, places=4)
        # 表底订正不增加次日配额
        self.assertFalse(ut1["quota_effect"]["changes_next_day_quota"])

    def test_ut2_pending_is_accrual_only(self):
        out = counter_billing_actions(self.repo)
        pending = {p["statement"] for p in out["pending_verification"]}
        self.assertIn("UT2", pending)

    def test_allocation_preserves_total_and_keeps_dispute(self):
        res = allocate_shared_bill(20, {"东柜": 14}, disputed_claims_l=5)
        self.assertTrue(res["balanced"])
        self.assertEqual(res["allocated_total_l"], 14)
        self.assertEqual(res["unattributed_or_disputed_l"], 6)
        self.assertEqual(res["open_difference_l"], 11)

    def test_allocation_over_identification_not_forced(self):
        res = allocate_shared_bill(10, {"东柜": 14})
        self.assertFalse(res["balanced"])
        self.assertIn("over_identified_l", res)

    def test_single_shared_prerinse_bill_keeps_residual_disputed(self):
        # 一张 20L 账单：东柜可直接认定 8L、西柜 6L；共享预冲洗 6L 不强摊
        res = shared_pre_rinse_allocation(20, {"东柜": 8, "西柜": 6})
        self.assertEqual(res["billed_now"], {"东柜": 8, "西柜": 6})
        self.assertEqual(res["shared_pre_rinse_disputed_l"], 6)
        self.assertTrue(res["balanced"])

    def test_month_end_april_keeps_property_total_and_unattributed(self):
        me = month_end_property_bill(self.repo, "2026-04")
        # 04-04=57, 04-11=60 -> 117 exact（这两天无换表边界）
        self.assertEqual(me["property_fresh_bill_range"]["lo"], 117)
        al = me["allocation_on_lower_bound"]
        self.assertEqual(al["identified_total_l"], 14)
        self.assertEqual(al["unattributed_or_disputed_l"], 103)
        self.assertEqual(me["pending_counter_claims_l"], 5)


class ReconcileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = load_repository()

    def test_c141_four_way_alignment(self):
        r = reconcile_batch(self.repo, "C141")
        self.assertEqual(r["quota_occupation"]["actual_fresh_range"]["value"], 20)
        self.assertEqual(r["accrued"]["confirmed_billed_l"], 14)
        self.assertEqual(r["accrued"]["pending_verification_l"], 5)
        self.assertEqual(r["unsettled_difference"]["difference_range"]["value"], 6)
        self.assertTrue(r["tie_checks"]["quota_slot_present"])

    def test_c131_revoked_check_blocks_release(self):
        r = reconcile_batch(self.repo, "C131")
        rw = r["release_water"]
        self.assertEqual(rw["check_verdict"], "撤销")
        self.assertFalse(rw["reuse_permitted_for_batch"])

    def test_c011_passing_check_permits_normal_reuse(self):
        r = reconcile_batch(self.repo, "C011")
        self.assertEqual(r["release_water"]["check_verdict"], "通过")
        self.assertTrue(r["release_water"]["reuse_permitted_for_batch"])


class ShiftAttributionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = load_repository()

    def test_three_anomaly_categories_present(self):
        fs = classify_anomalies(self.repo)
        cats = {f["category"] for f in fs}
        self.assertIn("meter_undecidable", cats)
        self.assertIn("reuse_failure", cats)
        self.assertIn("cowash_change", cats)

    def test_t13_reuse_failure_traces_to_r130_r131(self):
        log = receive_shift_log(self.repo)
        t13 = next(f for f in log["anomalies"] if f.get("check") == "T13")
        bookings = set()
        for t in t13["affected_commitments"]:
            bookings |= {o["booking"] for o in t["orders"]}
        self.assertEqual(bookings, {"R130", "R131"})

    def test_b2_pending_traces_to_b141_b142(self):
        log = receive_shift_log(self.repo)
        b2 = next(f for f in log["anomalies"] if f.get("tank") == "B-2")
        baskets = {t["basket"] for t in b2["affected_commitments"]}
        self.assertEqual(baskets, {"B141", "B142"})

    def test_handover_entries_accepted(self):
        log = receive_shift_log(self.repo)
        self.assertEqual(log["received_records"], 3)
        self.assertTrue(all(e["accepted"] for e in log["entries"]))

    def test_explain_slot_meter_undecidable_on_change_day(self):
        from washstation.shift import explain_slot_anomaly
        r = explain_slot_anomaly(self.repo, "2026-01-24", 2)
        self.assertEqual(r["primary_category"], "meter_undecidable")

    def test_explain_slot_reuse_failure_and_cowash_trace_commitments(self):
        from washstation.shift import explain_slot_anomaly
        # C131 实际在 slot1，T13 被撤销，且 B130/B131 合洗
        r = explain_slot_anomaly(self.repo, "2026-04-04", 1)
        self.assertIn("reuse_failure", r["contributing_categories"])
        self.assertIn("cowash_change", r["contributing_categories"])
        bookings = {o["booking"] for t in r["affected_commitments"]
                    for o in t["orders"]}
        self.assertEqual(bookings, {"R130", "R131"})

    def test_explain_slot_clean_shared_cycle_is_cowash_only(self):
        from washstation.shift import explain_slot_anomaly
        r = explain_slot_anomaly(self.repo, "2026-04-11", 1)
        self.assertEqual(r["measured_fresh_l"], 20)
        self.assertIn("cowash_change", r["contributing_categories"])
        self.assertNotIn("reuse_failure", r["contributing_categories"])


class LeaseAndDeliveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from washstation.station import Station
        cls.st = Station()

    def test_lease_commitment_gap_per_station(self):
        res = self.st.verify_leases([("松谷", 10), ("青崖", 20)])
        by = {r["station"]: r for r in res}
        self.assertTrue(by["松谷"]["meets_commitment_now"])   # 已交接 10
        self.assertFalse(by["青崖"]["meets_commitment_now"])  # 已交接 0
        self.assertEqual(by["青崖"]["gap_items_now"], 20)
        # 若 B141(11)+B142(12) 放行，可补到 23
        self.assertEqual(by["青崖"]["could_close_if_released"], 23)

    def test_delivery_counts_tie_to_baskets_not_whole_batch(self):
        v = self.st.delivery_vs_quota("2026-03-28")
        # 只有 B121 有“已交接” pickup 回单（9 件）；同批 B120 不被误计
        self.assertEqual(v["delivered_items"], 9)
        self.assertIsNotNone(v["quota_new_water_total_l"])
        states = {r["basket"]: r["state"] for r in v["basket_rows"]}
        self.assertEqual(states["B121"], "已交接")
        self.assertNotEqual(states["B120"], "已交接")


if __name__ == "__main__":
    unittest.main()
