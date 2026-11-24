"""把柜台回执、水量报表对到同一洗护批：四类口径一次对齐。

每批给出：

- ``quota_occupation`` 配额占用（时段水额与本批计量/估算需求）；
- ``release_water`` 可用于放行的水（新鲜水 + 有许可回用水；无许可不计）；
- ``accrued`` 应计水量（柜台计量单最新版本，含待核对）；
- ``unsettled_difference`` 未结算实际差额（计量实际区间 − 已确认出账）。

已交接回单是实物交付的最终证据；任何重算都不能改写它。
"""

from __future__ import annotations

from typing import Any

from .core import Range, Repository
from .meters import meter_windows
from .quality import effective_check, use_permitted


def _latest_statements_for_cycle(repo: Repository, cycle_id: str) -> list[dict[str, Any]]:
    ids = {s["statement"] for s in repo.statements if s["cycle"] == cycle_id}
    out = []
    for sid in ids:
        out.append(repo.latest_version("utility_statements", "statement", sid))
    return out


def _receipts_for_baskets(repo: Repository, basket_ids: list[str]) -> list[dict[str, Any]]:
    receipts = []
    for bid in basket_ids:
        for h in repo.handovers:
            if h["basket"] == bid:
                receipts.append({"source": "handover", **h})
        for p in repo.plans:
            if p["basket"] == bid and p["event"] in {"pickup", "order"}:
                receipts.append({"source": "booking_and_plan", **p})
    return receipts


def reconcile_batch(repo: Repository, cycle_id: str) -> dict[str, Any]:
    cycle = repo.cycle(cycle_id)
    day, slot = cycle["day"], cycle["slot"]
    basket_ids = list(cycle["baskets"])
    baskets = [repo.basket(b) for b in basket_ids]
    desks = sorted({b["desk"] for b in baskets})

    # 1) 配额占用
    q = repo.quota_slot(day, slot)
    ws = {w.slot: w for w in meter_windows(repo, day, "new-a")}
    w = ws.get(slot)
    if w is None or w.status != "measured":
        actual_fresh = Range(0, q["new_water_l"] if q else None)
        fresh_status = getattr(w, "status", "no_window")
    else:
        actual_fresh = Range.exact(w.litres)
        fresh_status = "measured"

    # 2) 放行可用水：新鲜水实际 + 经检测许可覆盖本批等级的回收/回用水
    check_row = next((c for c in repo.checks
                      if c["cycle"] == cycle_id
                      and c["version"] == max(
                          x["version"] for x in repo.checks if x["check"] == c["check"])),
                     None)
    soils = {b["soil"] for b in baskets}
    reuse_permitted = bool(check_row and check_row["verdict"] == "通过"
                           and soils <= set(check_row.get("allowed_use") or []))
    recorded_reuse = cycle.get("reused_l", 0)
    certified_reuse = recorded_reuse if reuse_permitted else 0
    release_water = {
        "fresh_actual_range": actual_fresh.to_dict(),
        "recorded_reused_l": recorded_reuse,
        "check": check_row["check"] if check_row else None,
        "check_version": check_row["version"] if check_row else None,
        "check_verdict": check_row["verdict"] if check_row else None,
        "allowed_use": (check_row.get("allowed_use") if check_row else None),
        "reuse_permitted_for_batch": reuse_permitted,
        "certified_reuse_l": certified_reuse,
        "note": ("检测许可不覆盖本批等级：已记录回用水也不能作为放行许可依据"
                 if not reuse_permitted and recorded_reuse else None),
    }

    # 3) 应计/出账
    latest_stmts = _latest_statements_for_cycle(repo, cycle_id)
    confirmed_billed = sum(s["billed_l"] for s in latest_stmts
                           if s["state"] in {"已签收", "表底订正"})
    pending_billed = sum(s["billed_l"] for s in latest_stmts
                         if s["state"] == "待核对")
    accrued = {
        "statements": [{"statement": s["statement"], "version": s["version"],
                        "desk": s["desk"], "billed_l": s["billed_l"],
                        "state": s["state"]} for s in latest_stmts],
        "confirmed_billed_l": confirmed_billed,
        "pending_verification_l": pending_billed,
        "accrual_l": confirmed_billed + pending_billed,
    }

    # 4) 未结算实际差额：实际新水区间 - 已确认出账（柜台计量单口径可能含回用水/预冲洗，
    #    因此差额只作对账信号，不强行解释为损耗）
    diff = actual_fresh - Range.exact(confirmed_billed)
    unsettled = {
        "actual_fresh_range": actual_fresh.to_dict(),
        "confirmed_billed_l": confirmed_billed,
        "difference_range": diff.to_dict(),
        "pending_statements_l": pending_billed,
        "interpretation": (
            "差额为正且较大：可能含未出账的共享预冲洗/其他柜台份额/未归属用水；"
            "若为区间，换表或缺测会使差额无法定稿；待核对单核实前不得结算"),
    }

    receipts = _receipts_for_baskets(repo, basket_ids)
    handed = [r for r in receipts if r.get("state") == "已交接"]
    return {
        "cycle": cycle_id, "day": day, "slot": slot, "program": cycle["program"],
        "baskets": basket_ids, "desks": desks,
        "item_count": sum(b["quantity"] for b in baskets),
        "quota_occupation": {
            "quota_new_water_l": q["new_water_l"] if q else None,
            "quota_slot_exists": q is not None,
            "machines": q["machines"] if q else None,
            "basket_capacity": q["basket_capacity"] if q else None,
            "actual_fresh_range": actual_fresh.to_dict(),
            "fresh_status": fresh_status,
            "occupies_quota": True,
        },
        "release_water": release_water,
        "accrued": accrued,
        "unsettled_difference": unsettled,
        "receipts": receipts,
        "handed_over": handed,
        "delivery_locked": len(handed) > 0,
        "tie_checks": {
            "baskets_exist": all(repo.basket(b) for b in basket_ids),
            "quota_slot_present": q is not None,
            "has_receipt_or_open_order": len(receipts) > 0,
        },
    }


def reconcile_all(repo: Repository) -> list[dict[str, Any]]:
    return [reconcile_batch(repo, c["cycle"]) for c in repo.operations]


def counter_delivery_vs_quota(repo: Repository, day: str) -> dict[str, Any]:
    """物业/柜台核对视图：当日配额占用与可交付件数必须对得上。

    合洗批按料筐逐个核对：只有拿到“已交接”回单的料筐才计已交付，
    不因为同批另一柜台已交接就把整批件数都算交付。
    """
    batches = [reconcile_batch(repo, c["cycle"])
               for c in repo.cycles_for_day(day)]
    q_rows = [q for q in repo.quota if q["day"] == day]
    quota_total = sum(q["new_water_l"] for q in q_rows) if q_rows else None

    handed_baskets = {h["basket"] for h in repo.handovers if h["state"] == "已交接"}
    handed_baskets |= {p["basket"] for p in repo.plans
                       if p["event"] == "pickup" and p["state"] == "已交接"}

    delivered_items = deliverable_items = blocked_items = 0
    basket_rows = []
    for bch in batches:
        verdict = bch["release_water"]["check_verdict"]
        batch_blocked = verdict in {"撤销", "不通过"}
        for bid in bch["baskets"]:
            qty = repo.basket(bid)["quantity"]
            if bid in handed_baskets:
                delivered_items += qty
                state = "已交接"
            elif batch_blocked:
                blocked_items += qty
                state = "放行受阻"
            else:
                deliverable_items += qty
                state = "可放行待交接"
            basket_rows.append({"basket": bid, "quantity": qty, "state": state,
                                "cycle": bch["cycle"]})
    return {"day": day, "quota_new_water_total_l": quota_total,
            "batches": [b["cycle"] for b in batches],
            "basket_rows": basket_rows,
            "delivered_items": delivered_items,
            "deliverable_pending_handover_items": deliverable_items,
            "blocked_items": blocked_items,
            "note": "已交接以柜台回单为准；可交付件数与配额占用在同一批记录上对齐"}
