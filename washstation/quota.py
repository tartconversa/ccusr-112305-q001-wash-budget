"""时段水额、占用与新班可用新水。

关键原则：

- 没有配额行的时段等于“物业未核额”，不能当作无限额来确认排程；
- 已确认排程与“处理中”料筐是硬占用，“未开洗”料筐可调整但在重排前仍在账上；
- 当前班水表断档时，缺口不能当作有水可用：对外给出配额上限与谨慎可排下限
  组成的区间，而不是一个精确可用数；
- 迟报段一旦按候选链接识别，只作为“待核实占用准备”，避免次日额度被花两次；
  它不直接变成已确认计量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .core import CURRENT_DAY, CURRENT_SLOT, PREVIOUS_DAY, Range, Repository
from .meters import register_anomalies


@dataclass
class Occupancy:
    day: str
    slot: int
    source: str                 # plan / schedule / operation / handover
    ref: str                    # 计划行或排程标识
    fresh: Range                # 新水需求/占用区间
    items: int = 0
    baskets: list[str] = field(default_factory=list)
    machines: list[str] = field(default_factory=list)
    firm: bool = True           # True=不可单方调整（处理中/已确认）
    state: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day, "slot": self.slot, "source": self.source, "ref": self.ref,
                "fresh": self.fresh.to_dict(), "items": self.items, "baskets": self.baskets,
                "machines": self.machines, "firm": self.firm, "state": self.state,
                "note": self.note}


def slot_quota(repo: Repository, day: str, slot: int) -> dict[str, Any] | None:
    return repo.quota_slot(day, slot)


def meter_encumbrance_candidates(repo: Repository, day: str = CURRENT_DAY,
                                 slot: int = CURRENT_SLOT) -> list[dict[str, Any]]:
    """可能已经占用某新班时段、但尚待核实的迟报计量。

    识别 :func:`washstation.meters.register_anomalies` 中的
    late_or_misdated_candidate：若候选前驱是上一经营日 slot3 的表尾，
    则第一个候选差分（如表尾 3160 -> NE10 3162 的 2L）归前一日交接，
    后续段内差分（NE10 -> NE11 的 13L）才是新班时段的可能占用。
    """
    anomalies = register_anomalies(repo)
    out: list[dict[str, Any]] = []
    for a in anomalies:
        if a["type"] != "late_or_misdated_candidate":
            continue
        pred_when = a.get("numeric_predecessor_when") or ""
        # 候选读数自身与其数值前驱同表；段内后续差分由 unanchored 段给出，
        # 这里只产出“新班时段可能已用”的候选（stamped slot 与目标 slot 相同）。
        stamped_slot = int(a["stamped_when"].split("#")[1])
        pred_day = pred_when.split("#")[0]
        if stamped_slot == slot and pred_day == PREVIOUS_DAY:
            # 表尾 -> 该首读数的差分属于跨日表底差，归前一日；
            # 真正的新班时段差分在同段相邻读数之间，另行从 meters 取。
            out.append({
                "meter": a["meter"], "first_reading": a["reading"],
                "base_gap_to_previous_day": a["hypothetical_delta"],
                "base_gap_attribution": PREVIOUS_DAY,
                "status": "prior_day_base_difference",
            })
    # 段内差分（NE10->NE11、NE20->NE21）作为新班时段候选占用
    from .meters import unanchored_segments
    for seg in unanchored_segments(repo):
        for w in seg["windows"]:
            if w["slot"] == slot and w["status"] == "measured" and w["litres"] is not None:
                out.append({
                    "meter": seg["meter"], "candidate_litres": w["litres"],
                    "window": w, "stamped_day": seg["day"],
                    "status": "unverified_new_shift_consumption",
                    "note": "迟报/错日段内实测差分；经营日归属待核实，"
                            "谨慎排程时先视作占用准备",
                })
    return out


def availability(repo: Repository, day: str = CURRENT_DAY, slot: int = CURRENT_SLOT,
                 occupancies: list[Occupancy] | None = None,
                 prudent_late_readings: bool = True) -> dict[str, Any]:
    """计算某时段新水可用量的完整口径。

    返回三个不同含义的数，避免把“配额”“已确认占用”“读表缺口”混为一谈：

    - ``quota_l``：物业核给该时段的新水额度；无配额行为 None（未核额）；
    - ``confirmed_occupancy_l``：硬占用的新水（区间，需求不确定时为区间）；
    - ``schedulable_l``：额度扣除硬占用后的可安排上限区间；
    - ``prudent_l``：再扣除尚未核实但可能已发生的迟报段水量后的谨慎下限口径；
    - ``certified_l``：有当前班实测表链背书的可用量；断档时为 None。
    """
    occupancies = occupancies or []
    q = repo.quota_slot(day, slot)
    if q is None:
        return {"day": day, "slot": slot, "quota_l": None,
                "schedulable": None, "prudent": None, "certified_l": None,
                "occupancies": [o.to_dict() for o in occupancies if o.day == day and o.slot == slot],
                "verdict": "该时段没有物业配额行：未核额，不能确认任何新水排程"}

    quota_l = float(q["new_water_l"])
    here = [o for o in occupancies if o.day == day and o.slot == slot]
    occ = Range.exact(0)
    for o in here:
        occ = occ + o.fresh
    schedulable = Range.exact(quota_l) - occ

    late = meter_encumbrance_candidates(repo, day, slot) if prudent_late_readings else []
    # 新水可用只受“新水表”候选段占用影响；回用水表的同形差分不扣新水配额。
    late_load = sum(x.get("candidate_litres", 0) for x in late
                    if str(x.get("meter", "")).startswith("new"))
    # 谨慎口径：把未核实的候选实际用水也先占用（避免花两次），但只作为下界。
    prudent_lo = max(0.0, schedulable.lo - late_load)
    prudent = Range(prudent_lo, schedulable.hi)

    # 当前班是否存在可背书的实测表链（配额日当天有 new 表读数）
    today_readings = [r for r in repo.meters if r["day"] == day
                      and r["meter"].startswith("new") and r["litres"] is not None]
    certified = None if not today_readings else schedulable.lo

    return {
        "day": day, "slot": slot, "quota_l": quota_l,
        "carry_from": q.get("carry_from"),
        "machines": q.get("machines", []),
        "basket_capacity": q.get("basket_capacity"),
        "confirmed_occupancy": occ.to_dict(),
        "occupancies": [o.to_dict() for o in here],
        "schedulable": schedulable.to_dict(),
        "late_reading_encumbrance_l": late_load,
        "late_reading_candidates": late,
        "prudent_available": prudent.to_dict(),
        "certified_available_l": certified,
        "meter_gap": not today_readings,
        "verdict": (
            "当前班没有新水实测表链：读表缺口不能当作有水可用；"
            f"配额 {quota_l:g}L，硬占用 {occ.format()}，"
            + (f"另有迟报候选 {late_load:g}L 待核实，谨慎可排 {prudent.format()}；"
               if late_load else "")
            + f"在迟报段核实前按 {prudent.lo:g}L 保守安排"
        ),
    }


def day_quota_total(repo: Repository, day: str) -> dict[str, Any]:
    rows = [r for r in repo.quota if r["day"] == day]
    if not rows:
        return {"day": day, "slots": 0, "new_water_l": None}
    return {"day": day, "slots": len(rows),
            "new_water_l": sum(r["new_water_l"] for r in rows),
            "slot_rows": rows}
