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

    def test_transfer_chain_inputs(self):
        sources = [read_table(name) for name in ['transfers', 'tank_ledger', 'utility_statements', 'handover']]
        self.assertEqual([x["litres"] for x in sources[0] if x["transfer"]=="TX1"], [11,11,9,10])
        self.assertTrue(any(x["grade"]=="待测" and len(x["parent_tanks"])==2 for x in sources[1]))
        self.assertEqual({x["version"] for x in sources[2] if x["statement"]=="UT1"}, {1,2})
        self.assertIn("已交接",{x["state"] for x in sources[3]})

