"""按洗护时段、柜台、料筐类别的水量核对与不确定归属。

设计底线：

- 实测表链能确定的总量给精确值；
- 换表边界、缺测窗口给 :class:`~washstation.core.Range` 和原因；
- 合洗批（如东柜+西柜共用程序/预冲洗）只给“批级精确、柜台级区间”，
  绝不按数量平摊成看似精确的柜台水量；数量比例值只作为“估算参考”，
  与实际归属分栏列示；
- 对不上洗护批/回收/使用/计费记录的计量水量列为“未归属”，并保留原因。
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from .core import Range, Repository
from .meters import fresh_day_range, meter_windows, reuse_day_total


# ---------------------------------------------------------------------------
# 时段水量台账
# ---------------------------------------------------------------------------

def slot_water_ledger(repo: Repository, day: str) -> list[dict[str, Any]]:
    """每个洗护时段一行：新水、回用水、洗护批、配额、计费单并列。"""
    fresh = fresh_day_range(repo, day)
    fresh_windows = {w["slot"]: w for w in fresh["windows"]}
    reuse_windows = {w.slot: w.to_dict() for w in meter_windows(repo, day, "reuse")}
    cycles_by_slot: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for c in repo.cycles_for_day(day):
        cycles_by_slot[c["slot"]].append(c)
    statements = [s for s in repo.statements if s["service_day"] == day]
    stmt_by_slot: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for s in statements:
        stmt_by_slot[s["slot"]].append(s)

    slots = sorted(set(fresh_windows) | set(reuse_windows) | set(cycles_by_slot)
                   | {r["slot"] for r in repo.quota if r["day"] == day}
                   | set(stmt_by_slot))
    rows = []
    for s in slots:
        fw = fresh_windows.get(s)
        rw = reuse_windows.get(s)
        q = repo.quota_slot(day, s)
        rows.append({
            "day": day, "slot": s,
            "fresh_window": fw,
            "reuse_window": rw,
            "cycles": [c["cycle"] for c in cycles_by_slot.get(s, [])],
            "quota_new_water_l": q["new_water_l"] if q else None,
            "machines": q["machines"] if q else None,
            "basket_capacity": q["basket_capacity"] if q else None,
            "statements": [{"statement": x["statement"], "version": x["version"],
                            "desk": x["desk"], "billed_l": x["billed_l"],
                            "state": x["state"]} for x in stmt_by_slot.get(s, [])],
        })
    return rows


# ---------------------------------------------------------------------------
# 料筐类别分布（实物事实，精确）
# ---------------------------------------------------------------------------

def basket_distribution(repo: Repository, day: str) -> dict[str, Any]:
    baskets = [b for b in repo.baskets if b["day"] == day]
    by_desk: dict[str, dict[str, Any]] = defaultdict(lambda: {"baskets": 0, "items": 0})
    by_soil: dict[str, dict[str, Any]] = defaultdict(lambda: {"baskets": 0, "items": 0})
    by_item: dict[str, dict[str, Any]] = defaultdict(lambda: {"baskets": 0, "items": 0})
    for b in baskets:
        for agg, key in ((by_desk, b["desk"]), (by_soil, b["soil"]), (by_item, b["item"])):
            agg[key]["baskets"] += 1
            agg[key]["items"] += b["quantity"]
    return {
        "day": day, "basket_count": len(baskets),
        "item_count": sum(b["quantity"] for b in baskets),
        "by_desk": dict(by_desk), "by_soil": dict(by_soil), "by_item": dict(by_item),
    }


# ---------------------------------------------------------------------------
# 归属
# ---------------------------------------------------------------------------

def _desk_share_estimate(desks_qty: dict[str, int], total_l: float) -> dict[str, float]:
    tot = sum(desks_qty.values()) or 1
    return {d: round(total_l * q / tot, 2) for d, q in desks_qty.items()}


def attribute_day(repo: Repository, day: str) -> dict[str, Any]:
    """一日水量归属主表。"""
    fresh = fresh_day_range(repo, day)
    reuse = reuse_day_total(repo, day)
    cycles = repo.cycles_for_day(day)

    cycle_rows: list[dict[str, Any]] = []
    attributed_fresh = Range.exact(0)
    unattributed_fresh = Range.exact(0)
    reasons: list[dict[str, Any]] = []

    fw_by_slot = {w["slot"]: w for w in fresh["windows"]}
    rw_by_slot = {w.slot: w for w in meter_windows(repo, day, "reuse")}

    matched_slots: set[int] = set()
    for c in sorted(cycles, key=lambda x: x["slot"]):
        s = c["slot"]
        matched_slots.add(s)
        fw = fw_by_slot.get(s)
        rw = rw_by_slot.get(s)
        basket_rows = [repo.basket(b) for b in c["baskets"]]
        desks = sorted({b["desk"] for b in basket_rows})
        desks_qty: dict[str, int] = defaultdict(int)
        for b in basket_rows:
            desks_qty[b["desk"]] += b["quantity"]

        # 新水：优先使用该时段实测窗口；窗口不确定则继承区间
        if fw is None:
            fresh_range = Range(0, None)
            fresh_status = "no_meter_window"
        elif fw["litres"] is None:
            cap = fw.get("quota_cap")
            fresh_range = Range(0, cap)
            fresh_status = fw["status"]
        else:
            fresh_range = Range.exact(fw["litres"])
            fresh_status = "measured"
        attributed_fresh = attributed_fresh + fresh_range

        shared = len(desks) > 1
        desk_attribution: dict[str, dict[str, Any]] = {}
        for d in desks:
            if shared:
                # 没有支路分表：柜台实际归属只能给 0..批总量 的区间。
                # 批总量本身不确定 [lo,hi] 时，柜台区间为 [0,hi]。
                desk_range = Range(0, fresh_range.hi)
                desk_attribution[d] = {
                    "actual_range": desk_range.to_dict(),
                    "cycle_total_range": fresh_range.to_dict(),
                    "basis": "shared_cycle_no_submeter",
                    "note": "合洗/共享预冲洗且无柜台分表，只能给 0..批总量 的归属区间；"
                            "批总量为其上限，不按数量平摊",
                }
            else:
                desk_attribution[d] = {"actual_range": fresh_range.to_dict(),
                                       "basis": "dedicated_cycle_slot_window"}
        estimate = None
        if fresh_range.is_exact:
            estimate = _desk_share_estimate(desks_qty, fresh_range.lo)

        reuse_measured = rw.litres if rw and rw.status == "measured" else None
        cycle_rows.append({
            "cycle": c["cycle"], "slot": s, "program": c["program"],
            "baskets": c["baskets"], "desks": desks,
            "soil_classes": sorted({b["soil"] for b in basket_rows}),
            "item_count": sum(b["quantity"] for b in basket_rows),
            "fresh_attribution_status": fresh_status,
            "fresh_l": fresh_range.to_dict(),
            "reuse_meter_window_l": reuse_measured,
            "recorded_reused_l": c.get("reused_l", 0),
            "recorded_recovered_l": c.get("recovered_l", 0),
            "desk_actual_ranges": desk_attribution,
            "quantity_proportional_estimate_l": estimate,
            "estimate_warning": "仅为数量比例估算，已经独立计量日核验误差，不能记作实际归属"
                                if shared else None,
        })

    # 未归属新水：有实测窗口但该时段没有洗护批
    for s, w in fw_by_slot.items():
        if s in matched_slots:
            continue
        if w["litres"] is not None:
            unattributed_fresh = unattributed_fresh + Range.exact(w["litres"])
            reasons.append({"kind": "unmatched_meter_window", "day": day, "slot": s,
                            "meter": w["meter"], "litres": w["litres"],
                            "reason": "该时段有新水计量但无洗护批记录"
                                      "（预注水/管路/清洁或记录缺失），保留为未归属"})
        else:
            cap = w.get("quota_cap")
            unattributed_fresh = unattributed_fresh + Range(0, cap)
            reasons.append({"kind": w["status"], "day": day, "slot": s,
                            "litres_range": Range(0, cap).to_dict(),
                            "reason": w.get("detail") or w["status"]})

    # 换表/缺测原因也提升到顶层
    for u in fresh["uncertain_windows"]:
        reasons.append({"kind": u["status"], "day": day, "slot": u["slot"],
                        "litres_range": {"lo": 0, "hi": u["hi"]}, "reason": u["reason"]})

    # 回用水核对：表计时段总量 vs 批记录 reused_l
    reuse_recorded = sum(c.get("reused_l", 0) for c in cycles)
    reuse_metered = reuse.get("litres")
    reuse_gap = None if reuse_metered is None else reuse_metered - reuse_recorded
    if reuse_gap and reuse_gap > 0:
        reasons.append({"kind": "reuse_not_on_cycle_records", "day": day,
                        "litres": reuse_gap,
                        "reason": "回用水表计量大于洗护批填报的 reused_l，"
                                  "差额可能用于未登记冲洗/罐操作，列为未归属回用水"})

    total_fresh_range = Range(fresh["range"]["lo"], fresh["range"]["hi"])
    # 计量总量与已归属+未归属应当对得上（区间可加）
    accounted = attributed_fresh + unattributed_fresh

    return {
        "day": day,
        "fresh_meter_total": fresh["range"],
        "fresh_attributed_to_cycles": attributed_fresh.to_dict(),
        "fresh_unattributed": unattributed_fresh.to_dict(),
        "fresh_accounted_sum": accounted.to_dict(),
        "attribution_ties_to_meter": (
            abs(accounted.lo - total_fresh_range.lo) < 1e-6
            and ((accounted.hi is None and total_fresh_range.hi is None)
                 or (accounted.hi is not None and total_fresh_range.hi is not None
                     and abs(accounted.hi - total_fresh_range.hi) < 1e-6))),
        "reuse_meter_total_l": reuse_metered,
        "reuse_recorded_on_cycles_l": reuse_recorded,
        "reuse_unattributed_l": reuse_gap,
        "reuse_problems": reuse.get("problems", []),
        "cycles": cycle_rows,
        "distribution": basket_distribution(repo, day),
        "unattributed_reasons": reasons,
    }


# ---------------------------------------------------------------------------
# 柜台视角（跨日/单日）
# ---------------------------------------------------------------------------

def desk_water_view(repo: Repository, day: str) -> dict[str, Any]:
    """柜台维度：合洗柜台只能拿到区间实际量，计费单按版本有效性单列。"""
    acc = attribute_day(repo, day)
    desks: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"item_count": 0, "cycles": [], "actual_fresh_ranges": [],
                 "billed_statements": []})
    for c in acc["cycles"]:
        for d, info in c["desk_actual_ranges"].items():
            desks[d]["cycles"].append(c["cycle"])
            desks[d]["actual_fresh_ranges"].append(info["actual_range"])
    dist = acc["distribution"]["by_desk"]
    for d, agg in dist.items():
        desks[d]["item_count"] = agg["items"]
        desks[d]["basket_count"] = agg["baskets"]

    for s in repo.statements:
        if s["service_day"] != day:
            continue
        rows = [x for x in repo.statements if x["statement"] == s["statement"]]
        latest = max(rows, key=lambda x: x["version"])
        # 每个单号只在其最新版本上形成当前柜台视图；历史版本在 billing 模块保留
        if s is latest:
            desks[s["desk"]]["billed_statements"].append({
                "statement": s["statement"], "version": s["version"],
                "billed_l": s["billed_l"], "state": s["state"],
                "cycle": s["cycle"], "received_day": s["received_day"],
                "confirmed": s["state"] in {"已签收", "表底订正"},
            })

    out = {}
    for d, v in desks.items():
        ranges = [Range(r["lo"], r["hi"]) for r in v["actual_fresh_ranges"]]
        merged = Range.exact(0)
        for r in ranges:
            merged = merged + r
        out[d] = {**v, "actual_fresh_total_range": merged.to_dict()}
    return {"day": day, "desks": out}


# ---------------------------------------------------------------------------
# 估算与独立计量核验
# ---------------------------------------------------------------------------

def _least_squares(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    var = sum((x - mx) ** 2 for x in xs) or 1.0
    slope = cov / var
    return slope, my - slope * mx


def estimate_and_validate(repo: Repository) -> dict[str, Any]:
    """用历史日拟合“新水 ~ 总件数 + 程序更新”的透明估算并核验。

    同时在有 ``individual`` 独立支路实测的日子给出独立核验误差。
    拟合/核验全部基于标准库最小二乘与中位数，结论保留误差，不制造精确假象。
    """
    days = repo.operating_days()
    sample = []
    for i, d in enumerate(days):
        fr = fresh_day_range(repo, d)
        dist = basket_distribution(repo, d)
        prog_rows = {c["program"] for c in repo.cycles_for_day(d)}
        water = fr["range"]["lo"] if fr["range"]["exact"] else None
        sample.append({
            "day": d, "week_index": i, "items": dist["item_count"],
            "program": "节水" if "节水" in prog_rows else "常用",
            "fresh_exact_l": water,
            "fresh_range": fr["range"],
        })

    fit_days = [s for s in sample if s["fresh_exact_l"] is not None]
    xs = [s["items"] for s in fit_days]
    ys = [s["fresh_exact_l"] for s in fit_days]
    slope, intercept = _least_squares(xs, ys)

    # 程序更新（03-07 起常用 -> 节水）前后分组均值，解释程序差异
    groups = defaultdict(list)
    for s in fit_days:
        groups[s["program"]].append(s["fresh_exact_l"] / s["items"])
    program_effect = {p: {"litres_per_item_median": round(median(v), 3),
                          "days": len(v)} for p, v in groups.items()}

    backtest = []
    abs_errs, pct_errs = [], []
    for s in sample:
        pred = intercept + slope * s["items"]
        row = {**s, "estimated_fresh_l": round(pred, 2)}
        if s["fresh_exact_l"] is not None:
            err = pred - s["fresh_exact_l"]
            row["error_l"] = round(err, 2)
            row["abs_error_l"] = round(abs(err), 2)
            row["pct_error"] = round(100 * err / s["fresh_exact_l"], 1)
            abs_errs.append(abs(err))
            pct_errs.append(abs(row["pct_error"]))
        else:
            # 换表日：估算值是否落入实测区间，本身就是核验
            rng = Range(s["fresh_range"]["lo"], s["fresh_range"]["hi"])
            row["estimate_inside_measured_range"] = rng.contains(pred)
        backtest.append(row)

    # 独立支路核验：在“有 individual 实测”的日子里，用同尺度模型
    # individual_l ~ intercept + slope * items 拟合，再回报拟合/核验误差。
    ind_days = {d: v for d, v in
                __import__("washstation.meters", fromlist=["individual_meter_days"])
                .individual_meter_days(repo).items() if v["measured"]}
    ind_samples = []
    for d, v in sorted(ind_days.items()):
        dist = basket_distribution(repo, d)
        ind_samples.append((dist["item_count"], v["litres"], d))
    ind_xs = [x for x, _, _ in ind_samples]
    ind_ys = [y for _, y, _ in ind_samples]
    ind_slope, ind_intercept = _least_squares(ind_xs, ind_ys)
    independent = []
    ind_errs = []
    for x, y, d in ind_samples:
        pred = ind_intercept + ind_slope * x
        err = pred - y
        ind_errs.append(abs(err))
        independent.append({"day": d, "individual_meter_l": y,
                            "items": x,
                            "model_estimate_l": round(pred, 2),
                            "difference_l": round(err, 2)})

    # 季节/周相解释：新水日总量严格按 5 周相位起伏（资料的时间结构）
    phase = defaultdict(list)
    for i, s in enumerate(sample):
        if s["fresh_exact_l"] is not None:
            phase[i % 5].append(s["fresh_exact_l"])
    seasonal = {f"week_phase_{k}": round(median(v), 1) for k, v in sorted(phase.items()) if v}

    return {
        "model": "fresh_day_l = intercept + slope * item_count（标准库 OLS，透明可复核）",
        "slope_l_per_item": round(slope, 3),
        "intercept_l": round(intercept, 3),
        "backtest": backtest,
        "mae_l": round(sum(abs_errs) / len(abs_errs), 2) if abs_errs else None,
        "mape_pct": round(sum(pct_errs) / len(pct_errs), 1) if pct_errs else None,
        "program_groups": program_effect,
        "seasonal_week_phase_medians": seasonal,
        "independent_meter_validation": independent,
        "independent_model": "individual_l = %.3f + %.3f * items（仅用有实测日拟合）"
                             % (ind_intercept, ind_slope),
        "independent_meter_mae_l": round(sum(ind_errs) / len(ind_errs), 2) if ind_errs else None,
        "interpretation": [
            "季节/周相：日新水总量在资料中呈 5 周相位（51/54/57/60/63L 一档）的周期性，"
            "残差主要随周相而非件数变化，跨季估算必须保留季节项与更宽误差带；",
            "程序更新：2026-03-07 起合洗批由常用转节水，分组单升件数中位数变化见 program_groups，"
            "但与件数/季节共线，不能把同期下降全部记为节水程序的效果；",
            "批量差异：件数在 13–33 件间摆动且与周相周期重叠，OLS 斜率只给平均倾向；"
            "换表日（01-24、03-21）只有区间实测，用估算是否落入区间核验；",
            "独立支路 individual 仅覆盖有实测值的日子且资料未声明其柜台/机台范围，"
            "故只做通道级独立核验，不把它强行等同于某类料筐的分表；",
            "缺测日（01-31、03-14、04-11 individual=null）不得按 0 或按满额确认，"
            "估算值只能用于排程情景，不能替代计量归属。",
        ],
    }
