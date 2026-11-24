#!/usr/bin/env python3
"""生成雪具洗护站完整核算报告（JSON + Markdown）。

用法（在项目根目录）：
    python3 scripts/station_report.py [输出目录]

默认输出到 reports/。该脚本是 washstation 包的薄封装，
也可以用 ``python3 -m washstation write reports`` 达到同样效果。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from washstation.report import build_report, render_markdown, to_json  # noqa: E402
from washstation.station import Station  # noqa: E402


def main(argv=None) -> int:
    outdir = Path(argv[0] if argv else ROOT / "reports")
    outdir.mkdir(parents=True, exist_ok=True)
    station = Station(ROOT / "data")
    report = build_report(station)
    json_path = outdir / "station_report.json"
    md_path = outdir / "station_report.md"
    json_path.write_text(to_json(report), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"JSON: {json_path}")
    print(f"摘要: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
