"""命令行入口：``python -m washstation [report|availability|scenarios|...]``。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import CURRENT_DAY, CURRENT_SLOT
from .report import build_report, render_markdown, to_json
from .station import Station


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="washstation",
                                    description="雪具洗护站用水/交接核算")
    parser.add_argument("--data-dir", help="资料目录（默认项目 data/）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("report", help="输出完整 JSON 报告")
    p_md = sub.add_parser("markdown", help="输出 Markdown 摘要")
    p_md.add_argument("-o", "--output", help="写入 .md 文件")
    p_json = sub.add_parser("write", help="同时写 JSON 与 Markdown 报告到目录")
    p_json.add_argument("outdir", nargs="?", default="reports")

    sub.add_parser("availability", help="新班新水可用量")
    sub.add_parser("scenarios", help="次日领件情景比较")
    sub.add_parser("anomalies", help="计量异常清单")
    sub.add_parser("shift", help="下一班交接归因")
    sub.add_parser("billing", help="柜台计量单/补发动作")
    p_day = sub.add_parser("day", help="某经营日水量归属 JSON")
    p_day.add_argument("day")
    p_rec = sub.add_parser("reconcile", help="某洗护批四方核对 JSON")
    p_rec.add_argument("cycle")
    args = parser.parse_args(argv)

    st = Station(args.data_dir) if args.data_dir else Station()

    if args.cmd == "report":
        print(to_json(build_report(st)))
    elif args.cmd == "markdown":
        md = render_markdown(build_report(st))
        if args.output:
            Path(args.output).write_text(md, encoding="utf-8")
        else:
            print(md)
    elif args.cmd == "write":
        out = Path(args.outdir)
        out.mkdir(parents=True, exist_ok=True)
        rep = build_report(st)
        (out / "station_report.json").write_text(to_json(rep), encoding="utf-8")
        (out / "station_report.md").write_text(render_markdown(rep), encoding="utf-8")
        print(f"已写出 {out / 'station_report.json'} 和 {out / 'station_report.md'}")
    elif args.cmd == "availability":
        print(json.dumps(st.new_shift_availability(CURRENT_DAY, CURRENT_SLOT),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "scenarios":
        print(json.dumps(st.scenarios(), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "anomalies":
        print(json.dumps(st.anomalies(), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "shift":
        print(json.dumps(st.shift_log(), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "billing":
        print(json.dumps(st.billing_actions(), ensure_ascii=False, indent=2))
    elif args.cmd == "day":
        print(json.dumps(st.day_account(args.day), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "reconcile":
        print(json.dumps(st.reconcile_cycle(args.cycle), ensure_ascii=False, indent=2,
                         default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
