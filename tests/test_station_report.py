"""Station 门面与完整报告的端到端冒烟/守恒测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import unittest

from washstation.station import Station
from washstation.report import build_report, render_markdown


class StationReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.st = Station()
        cls.rep = build_report(cls.st)

    def test_report_is_json_serializable(self):
        text = json.dumps(self.rep, ensure_ascii=False, default=str)
        self.assertGreater(len(text), 1000)
        again = json.loads(text)
        self.assertIn("scene", again)
        self.assertIn("month_end_2026_04", again)

    def test_markdown_renders_all_sections(self):
        md = render_markdown(self.rep)
        for section in ("新班还有几升新水可用", "计量异常", "检测推翻",
                        "回用罐与跨站调拨", "柜台计量单", "次日领件情景",
                        "异常归因"):
            self.assertIn(section, md)

    def test_report_contains_seeded_identities(self):
        blob = json.dumps(self.rep, ensure_ascii=False, default=str)
        for token in ("T13", "TX1", "TX2", "UT1", "UT2", "B-2", "A-9",
                      "R130", "R131", "Q42", "Q43", "NE11", "new-b"):
            self.assertIn(token, blob)

    def test_existing_data_contract_still_green(self):
        # 门面读取不应改动任何原始资料记录数
        self.assertEqual(len(self.st.repo.baskets), 42)
        self.assertEqual(len(self.st.repo.meters), 172)


if __name__ == "__main__":
    unittest.main()
