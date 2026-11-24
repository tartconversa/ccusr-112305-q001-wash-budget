"""累计水表的差分、换表/缺测处理与迟报冲突识别。

口径（见 data/README.md）：

- 水表记录是累计升数；
- 表名变化表示独立表底，两个计数不能直接相减；
- ``litres is None`` 是缺测，不是 0；
- 同一表名在时间顺序上出现读数倒退，说明表底/日期/迟报存在冲突，
  该段水量只能列为“计量不可判定”，不能强行接链。

读数 ``r`` 位于时段 ``s`` 初，则 ``slot=s -> slot=s+1`` 的同表差分
就是时段 ``s`` 的计量水量。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .core import Range, Repository


@dataclass
class MeterWindow:
    day: str
    slot: int                 # 用水时段（差分起点 slot）
    meter: str
    litres: float | None      # 实测差分；None 表示该窗口不可判定
    status: str               # measured | missing | meter_change | nonconsecutive
    start_reading: str | None = None
    end_reading: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day, "slot": self.slot, "meter": self.meter,
            "litres": self.litres, "status": self.status,
            "start_reading": self.start_reading, "end_reading": self.end_reading,
            "detail": self.detail,
        }


def _numeric_readings(repo: Repository, day: str, meter: str) -> list[dict[str, Any]]:
    rows = [r for r in repo.meters if r["day"] == day and r["meter"] == meter]
    return sorted((r for r in rows if r["litres"] is not None), key=lambda r: r["slot"])


def meter_windows(repo: Repository, day: str, meter: str) -> list[MeterWindow]:
    """某一天某块表所有可计算的相邻时段差分。

    缺测读数会把链断开：只对 slot 严格连续且都有实测值的一对读数出差分。
    """
    rows = sorted(
        (r for r in repo.meters if r["day"] == day and r["meter"] == meter),
        key=lambda r: r["slot"],
    )
    windows: list[MeterWindow] = []
    by_slot = {r["slot"]: r for r in rows}
    slots = sorted(by_slot)
    for s in range(min(slots), max(slots)):
        a, b = by_slot.get(s), by_slot.get(s + 1)
        if a is None or b is None:
            windows.append(MeterWindow(day, s, meter, None, "missing",
                                       detail="相邻时段读数缺失"))
            continue
        if a["litres"] is None or b["litres"] is None:
            windows.append(MeterWindow(day, s, meter, None, "missing",
                                       a["reading"], b["reading"], "读数为 null（缺测）"))
            continue
        if b["litres"] < a["litres"]:
            windows.append(MeterWindow(day, s, meter, None, "conflict",
                                       a["reading"], b["reading"], "累计读数倒退"))
            continue
        windows.append(MeterWindow(day, s, meter, b["litres"] - a["litres"], "measured",
                                   a["reading"], b["reading"]))
    return windows


def fresh_day_range(repo: Repository, day: str, expected_slots: range = range(3)
                    ) -> dict[str, Any]:
    """一个经营日新水（new-a/new-b）0->3 读表跨度内的水量区间。

    处理两类边界：

    - 换表：slot3 出现 new-b 独立表底，slot2->3 不能相减，用水量
      下界 0、上界取该时段物业水额（保守允许上限），形成区间；
    - 缺测：没有任何可用同表链时同样给区间，而不是把缺口当 0。
    """
    detail_windows: list[dict[str, Any]] = []
    measured = 0.0
    uncertain: list[dict[str, Any]] = []

    new_meters = sorted({r["meter"] for r in repo.meters
                         if r["day"] == day and r["meter"].startswith("new")})
    # 逐时段判断：优先同表连续差分；若该 slot 两端表名不同则换表边界。
    all_rows = [r for r in repo.meters if r["day"] == day and r["meter"].startswith("new")]
    at_slot: dict[int, list[dict[str, Any]]] = {}
    for r in all_rows:
        at_slot.setdefault(r["slot"], []).append(r)

    for s in expected_slots:
        rs_a = at_slot.get(s, [])
        rs_b = at_slot.get(s + 1, [])
        same = None
        for a in rs_a:
            for b in rs_b:
                if a["meter"] == b["meter"] and a["litres"] is not None \
                        and b["litres"] is not None and b["slot"] == a["slot"] + 1:
                    same = (a, b)
        quota_row = repo.quota_slot(day, s)
        cap = float(quota_row["new_water_l"]) if quota_row else None
        if same:
            a, b = same
            lit = b["litres"] - a["litres"]
            measured += lit
            w = MeterWindow(day, s, a["meter"], lit, "measured",
                            a["reading"], b["reading"]).to_dict()
            detail_windows.append(w)
            continue
        # 无法同表差分：区分换表与缺测
        meter_names_a = {r["meter"] for r in rs_a}
        meter_names_b = {r["meter"] for r in rs_b}
        kind = "meter_change" if meter_names_a and meter_names_b \
            and meter_names_a != meter_names_b else "missing"
        reason = ("换表：两端表名不同，独立表底不能相减" if kind == "meter_change"
                  else "缺测或时段不连续，无法差分")
        hi = cap  # 无配额行时为 None -> 不设有限上界
        uncertain.append({"slot": s, "status": kind, "lo": 0, "hi": hi,
                          "reason": reason,
                          "meters": sorted(meter_names_a | meter_names_b)})
        detail_windows.append({"day": day, "slot": s, "meter": "/".join(
            sorted(meter_names_a | meter_names_b)), "litres": None,
            "status": kind, "detail": reason,
            "quota_cap": cap})

    lo = measured
    hi = measured + sum((u["hi"] if u["hi"] is not None else 0) for u in uncertain)
    unbounded = any(u["hi"] is None for u in uncertain)
    rng = Range(lo, None if unbounded else hi)
    return {
        "day": day, "meter_group": "new", "range": rng.to_dict(),
        "measured_exact_l": measured,
        "uncertain_windows": uncertain,
        "windows": detail_windows,
        "reason": ("; ".join(sorted({u["reason"] for u in uncertain}))
                   if uncertain else "同表连续实测，全日水量可确定"),
    }


def reuse_day_total(repo: Repository, day: str) -> dict[str, Any]:
    """回用水表全日可测总量（reuse 表 slots0-3 通常连续）。"""
    ws = meter_windows(repo, day, "reuse")
    ok = [w for w in ws if w.status == "measured"]
    bad = [w.to_dict() for w in ws if w.status != "measured"]
    total = sum(w.litres for w in ok if w.litres is not None)
    complete = not bad and len(ok) >= 1
    return {"day": day, "meter": "reuse", "litres": total if complete else None,
            "measured_windows": [w.to_dict() for w in ok],
            "problems": bad,
            "range": Range.exact(total).to_dict() if complete else None}


def individual_meter_days(repo: Repository) -> dict[str, dict[str, Any]]:
    """独立支路表按日汇总；只有存在实测值的日子才给出计量值。"""
    out: dict[str, dict[str, Any]] = {}
    days = sorted({r["day"] for r in repo.meters if r["meter"] == "individual"})
    for day in days:
        vals = [r["litres"] for r in repo.meters
                if r["meter"] == "individual" and r["day"] == day]
        valid = [v for v in vals if v is not None]
        if not valid:
            out[day] = {"day": day, "litres": None, "measured": False,
                        "reason": "独立支路全天缺测"}
            continue
        rows = _numeric_readings(repo, day, "individual")
        total = max(valid) - min(valid) if len(valid) >= 2 else None
        out[day] = {"day": day, "litres": total, "measured": total is not None,
                    "readings": len(rows),
                    "slot_deltas": [w.to_dict() for w in meter_windows(repo, day, "individual")
                                    if w.status == "measured"]}
    return out


def register_anomalies(repo: Repository) -> list[dict[str, Any]]:
    """识别跨日期的表链接冲突与“迟报/错日”候选。

    典型 fixture：NE10/NE11（day=2026-04-05）表码 3162/3175，
    按日期排在 04-04 与 04-11 之间，却让同表链出现倒退；而按累计
    数值它们恰好接在 M1330=3160 之后。两种解释都保留，交业务判定，
    不把其中一种直接记成已归属水量。
    """
    anomalies: list[dict[str, Any]] = []
    for meter in sorted({r["meter"] for r in repo.meters}):
        rows = [r for r in repo.meters if r["meter"] == meter and r["litres"] is not None]
        chrono = sorted(rows, key=lambda r: (r["day"], r["slot"], r["reading"]))
        for prev, cur in zip(chrono, chrono[1:]):
            if cur["litres"] < prev["litres"]:
                anomalies.append({
                    "type": "register_backtrack",
                    "meter": meter,
                    "edge": [prev["reading"], cur["reading"]],
                    "between": [f'{prev["day"]}#{prev["slot"]}',
                                f'{cur["day"]}#{cur["slot"]}'],
                    "readings": [prev["litres"], cur["litres"]],
                    "deficit": prev["litres"] - cur["litres"],
                    "verdict": "计量不可判定：同表累计读数倒退，日期/表底/迟报存在冲突",
                })
        # 错日/迟报候选：非标准经营日（配额表无该日）的记录，且其表码
        # 能在数值顺序上接到全表最大链尾之后。
        quota_days = {r["day"] for r in repo.quota}
        odd = [r for r in rows if r["day"] not in quota_days]
        if odd:
            anchor = max(rows, key=lambda r: r["litres"])
            for r in sorted(odd, key=lambda x: x["slot"]):
                candidates = [q for q in rows
                              if q["litres"] <= r["litres"] and q["reading"] != r["reading"]]
                pred = max(candidates, key=lambda q: q["litres"]) if candidates else None
                anomalies.append({
                    "type": "late_or_misdated_candidate",
                    "meter": meter,
                    "reading": r["reading"],
                    "stamped_when": f'{r["day"]}#{r["slot"]}',
                    "litres": r["litres"],
                    "numeric_predecessor": pred["reading"] if pred else None,
                    "numeric_predecessor_when": (f'{pred["day"]}#{pred["slot"]}'
                                                 if pred else None),
                    "hypothetical_delta": (r["litres"] - pred["litres"] if pred else None),
                    "verdict": ("日期戳与累计表码顺序不一致；数值上可形成候选链接，"
                                "但只能作为待核实假设，不能据此扣减次日额度"),
                })
    return anomalies


def unanchored_segments(repo: Repository) -> list[dict[str, Any]]:
    """配额日历之外日期（如跨经营日交接/迟报 NE 记录）的同段差分。"""
    quota_days = {r["day"] for r in repo.quota}
    out: list[dict[str, Any]] = []
    for day in sorted({r["day"] for r in repo.meters if r["day"] not in quota_days}):
        for meter in sorted({r["meter"] for r in repo.meters if r["day"] == day}):
            ws = [w.to_dict() for w in meter_windows(repo, day, meter)]
            if ws:
                out.append({"day": day, "meter": meter, "windows": ws,
                            "note": "配额日历外的交接/迟报记录，差分为段内实测，"
                                    "但经营日归属不可直接认定"})
    return out
