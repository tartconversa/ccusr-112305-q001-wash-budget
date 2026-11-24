"""检测版本推翻、回用罐等级传播、跨站调拨与承诺替换测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest

from washstation.core import load_repository
from washstation.quality import overturn_cascade, use_permitted
from washstation.tanks import grade_change_impact, tank_positions
from washstation.transfers import transfer_view, propose_transfer
from washstation.commitments import (rewash_replacement_plan,
                                     shipped_low_grade_evidence,
                                     confirmed_pickup_commitments)


class CheckOverturnTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = load_repository()

    def test_t13_versions_and_overturn(self):
        oc = overturn_cascade(self.repo, "T13")
        self.assertTrue(oc["overturned"])
        self.assertEqual(len(oc["timeline"]), 2)
        self.assertEqual(oc["effective"]["verdict"], "撤销")
        self.assertEqual(oc["effective"]["allowed_use"], [])
        self.assertEqual(oc["effective"]["available_slot"], 4)

    def test_overturn_distinguishes_in_process_vs_adjustable(self):
        oc = overturn_cascade(self.repo, "T13")
        by_b = {b["basket"]: b for b in oc["baskets"]}
        self.assertEqual(by_b["B130"]["plan_state"], "处理中")
        self.assertIn("暂停放行", by_b["B130"]["decision"])
        self.assertEqual(by_b["B131"]["plan_state"], "未开洗")
        self.assertIn("尚可调整", by_b["B131"]["decision"])
        self.assertFalse(by_b["B130"]["shippable_now"])

    def test_overturn_pickup_gap_is_19_items(self):
        oc = overturn_cascade(self.repo, "T13")
        gap = oc["pickup_gap"]
        self.assertEqual(gap["basket_count"], 2)
        self.assertEqual(gap["item_count"], 19)
        self.assertEqual({o["booking"] for o in gap["confirmed_orders"]},
                         {"R130", "R131"})

    def test_empty_allowed_use_never_permits(self):
        self.assertFalse(use_permitted({"verdict": "撤销", "allowed_use": []}, "普通"))
        self.assertFalse(use_permitted(
            {"verdict": "通过", "allowed_use": ["普通"]}, "单洗"))
        self.assertTrue(use_permitted(
            {"verdict": "通过", "allowed_use": ["普通", "单洗"]}, "单洗"))


class TankAndTransferTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = load_repository()
        cls.pos = {p["tank"]: p for p in tank_positions(cls.repo)}

    def test_b2_mixed_with_pending_water_loses_certification(self):
        b2 = self.pos["B-2"]
        self.assertEqual(b2["current_grade"], "待测")
        self.assertTrue(b2["has_pending_test_water"])
        # 9(A-9)+8(U-8)=17 台账；到站更正 9->10 后订正为 18
        self.assertEqual(b2["ledger_volume_l"], 17)
        self.assertEqual(b2["adjusted_volume_l"], 18)
        self.assertEqual(b2["certified_usable_l"], 0)

    def test_b2_keeps_all_subsequent_claims(self):
        impact = grade_change_impact(self.repo, "B-2")
        refs = {c["ref"] for c in impact["affected_claims"]}
        # 当前料筐 H2/H3 与后续争用预留 Q43 都要重新判断
        self.assertIn("H2", refs)
        self.assertIn("H3", refs)
        self.assertIn("Q43", refs)
        self.assertEqual(self.pos["B-2"]["open_claims_l"], 6)
        self.assertEqual(self.pos["B-2"]["physical_headroom_after_claims_l"], 12)

    def test_a9_outbound_locks_lent_volume(self):
        a9 = self.pos["A-9"]
        self.assertEqual(a9["outbound_confirmed_l"], 11)
        self.assertEqual(a9["remaining_physical_l"], 4)
        # TX1 预留已出站，不再算作未了结索取
        self.assertEqual(a9["open_claims_l"], 0)

    def test_tx1_lifecycle_and_meter_gap(self):
        v = transfer_view(self.repo, "TX1")
        self.assertEqual(v.proposed_l, 11)
        self.assertEqual(v.outbound_l, 11)
        self.assertEqual(v.arrival_l, 10)      # 最新更正
        self.assertEqual(v.metering_gap_l, 1)  # 11 - 10
        self.assertEqual([h["litres"] for h in v.arrival_history], [9, 10])

    def test_propose_transfer_checks_both_sides(self):
        ok = propose_transfer(self.repo, "A-9", 4, origin_free_l=4,
                              target_intake_capacity_l=20, certified_usable_l=4)
        self.assertTrue(ok["feasible"])
        bad = propose_transfer(self.repo, "B-2", 6, origin_free_l=18,
                               certified_usable_l=0)
        self.assertFalse(bad["feasible"])
        self.assertTrue(any("检测许可" in r for r in bad["reasons"]))


class CommitmentAndEvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = load_repository()

    def test_handed_over_evidence_retained(self):
        ev = shipped_low_grade_evidence(self.repo)
        baskets = {e["basket"]: e for e in ev}
        self.assertIn("B140", baskets)
        self.assertEqual(baskets["B140"]["cycle"], "C141")
        check_ids = {c["check"] for c in baskets["B140"]["original_check_versions"]}
        self.assertIn("T14", check_ids)
        self.assertEqual(baskets["B140"]["handed_at"], "2026-04-11#5")
        self.assertTrue(baskets["B140"]["retention_rule"])

    def test_rewash_deadline_marks_replacement(self):
        # 当前 slot4，重洗需 2 段 -> 最早 slot6；R130 领件 slot5 赶不上 -> 替换
        plan = rewash_replacement_plan(self.repo, ["B130", "B131"],
                                       "2026-04-04", 4, rewash_slots_needed=2)
        by = {i["basket"]: i for i in plan["items"]}
        self.assertTrue(by["B130"]["needs_replacement_pickup"])
        # 完成 slot6，R131 截止也是 slot6，<= 成立，可赶在截止前
        self.assertTrue(by["B131"]["can_rewash_before_deadline"])
        self.assertEqual(plan["replacement_item_count"], 9)

    def test_confirmed_commitments_list(self):
        cs = confirmed_pickup_commitments(self.repo)
        ids = {c["booking"] for c in cs}
        self.assertEqual(ids, {"R130", "R131"})


if __name__ == "__main__":
    unittest.main()
