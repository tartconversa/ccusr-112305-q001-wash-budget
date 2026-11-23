"""校验可供分析的资料关系。"""

import unittest

from scripts.inspect_sources import read_table


class SourceContractTest(unittest.TestCase):
    def test_source_observations_and_references(self):
        meter, baskets = read_table("meters"), read_table("baskets")
        cycles, checks = read_table("operations"), read_table("water_checks")
        basket_ids = {row["basket"] for row in baskets}
        self.assertTrue(all(set(row["baskets"]) <= basket_ids for row in cycles))
        self.assertGreaterEqual(len({row["day"] for row in meter if row["meter"] == "individual" and row["litres"] is not None}), 8)
        self.assertEqual({row["meter"] for row in meter if row["slot"] == 3} & {"new-a","new-b"}, {"new-a","new-b"})
        prior, later = [row for row in checks if row["check"] == "T13"]
        states = [row for row in read_table("booking_and_plan") if row["cycle"] == later["cycle"]]
        self.assertLess(prior["available_slot"], later["available_slot"])
        self.assertEqual({row["state"] for row in states}, {"处理中", "未开洗"})
        self.assertNotEqual(prior["allowed_use"], later["allowed_use"])
