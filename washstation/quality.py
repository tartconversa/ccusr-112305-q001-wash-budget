"""取样检测单的版本订正、许可用途与“推翻”级联重判。

规则：

- 同一 ``check`` 单号按 ``version`` 订正，旧版本与当时证据保留，不覆盖；
- ``allowed_use`` 是回用水可用于的料筐等级；空列表表示该水不得用于任何等级；
- 一张检测单被撤销/推翻时，必须同时重判三件事：
  1. 尚未使用的相关回用水（冻结/降级，不能继续按旧许可占用）；
  2. 关联洗护批中已洗料筐能否放行（未交接的重新判定，已交接的凭回单固定）；
  3. 下一班/当日取件承诺缺口；
- 已确认交接是沉淀事实，重算排程不得覆盖它。
"""

from __future__ import annotations

from typing import Any

from .core import Repository


def check_timeline(repo: Repository, check_id: str) -> list[dict[str, Any]]:
    return repo.checks_of(check_id)


def effective_check(repo: Repository, check_id: str) -> dict[str, Any]:
    return repo.latest_version("water_checks", "check", check_id)


def use_permitted(check: dict[str, Any] | None, basket_soil: str) -> bool:
    """某检测结论是否允许把该回用水用于给定污染等级料筐。

    - 没有检测单或结论非“通过” -> 不允许；
    - ``allowed_use`` 为空 -> 对任何等级都不允许；
    - “单洗”等级永远需要明确列入许可（普通许可不能覆盖单洗）。
    """
    if not check or check.get("verdict") != "通过":
        return False
    allowed = check.get("allowed_use") or []
    return basket_soil in allowed


def _plans_for_cycle(repo: Repository, cycle_id: str) -> list[dict[str, Any]]:
    return [p for p in repo.plans if p.get("cycle") == cycle_id and p["event"] == "plan"]


def _orders_for_baskets(repo: Repository, basket_ids: list[str]) -> list[dict[str, Any]]:
    return [p for p in repo.plans if p["event"] == "order" and p["basket"] in basket_ids]


def _handed_over(repo: Repository, basket_ids: list[str]) -> list[dict[str, Any]]:
    out = []
    for b in basket_ids:
        for h in repo.handovers:
            if h["basket"] == b and h["state"] == "已交接":
                out.append(h)
        for p in repo.plans:
            if p["event"] == "pickup" and p["basket"] == b and p["state"] == "已交接":
                out.append(p)
    return out


def overturn_cascade(repo: Repository, check_id: str) -> dict[str, Any]:
    """对一张被推翻/撤销的检测单做完整级联重判（不修改原始资料）。"""
    timeline = check_timeline(repo, check_id)
    if not timeline:
        raise KeyError(f"没有检测单 {check_id}")
    old, new = timeline[0], timeline[-1]
    overturned = new["version"] > old["version"] and new.get("verdict") in {"撤销", "不通过"}
    cycle = repo.cycle(new["cycle"])
    basket_ids = list(cycle["baskets"])
    baskets = [repo.basket(b) for b in basket_ids]

    # 1) 未用回用水：批记录回收量 - 同日有记录的再用量，只是可追溯下界。
    day = cycle["day"]
    day_cycles = repo.cycles_for_day(day)
    recovered_total = sum(c.get("recovered_l", 0) for c in day_cycles)
    reused_total = sum(c.get("reused_l", 0) for c in day_cycles)
    cycle_recovered = cycle.get("recovered_l", 0)
    unused_balance = max(0, recovered_total - reused_total)
    water_judgement = {
        "check": check_id,
        "overturned": overturned,
        "old_allowed_use": old.get("allowed_use", []),
        "new_allowed_use": new.get("allowed_use", []),
        "new_verdict": new.get("verdict"),
        "new_available_slot": new.get("available_slot"),
        "cycle_recovered_l": cycle_recovered,
        "day_recovered_l": recovered_total,
        "day_reused_l": reused_total,
        "traceable_unused_balance_l": unused_balance,
        "action": ("冻结并降级所有仍在罐/预留中的同范围回用水，"
                   "旧版本仅作为当时善意使用证据；不得再据此节省新水"
                   if overturned else "检测结论未被推翻"),
    }

    # 2) 已洗料筐能否发出：已交接固定；其余按计划状态区分。
    plan_rows = _plans_for_cycle(repo, cycle["cycle"])
    plan_by_basket = {p["basket"]: p for p in plan_rows}
    handed = _handed_over(repo, basket_ids)
    handed_baskets = {h["basket"] for h in handed}
    basket_judgements = []
    for b in baskets:
        bid = b["basket"]
        plan = plan_by_basket.get(bid)
        if bid in handed_baskets:
            decision = "已交接-事实固定"
            shippable = True
            action = "保留原检测版本与交接回单，重算排程不得覆盖；如后续追溯降级，" \
                     "另走退回重洗/替换流程"
        elif plan is not None and plan["state"] == "处理中":
            decision = "处理中-暂停放行"
            shippable = False
            action = "批已在处理，保留过程记录；未经重新检测/重洗不得放行"
        elif plan is not None and plan["state"] == "未开洗":
            decision = "未开洗-尚可调整"
            shippable = False
            action = "可改约合洗/改时段/改用有许可回用水或全新鲜水重排"
        else:
            decision = "已洗待判"
            shippable = False
            action = "缺少已交接回单，按未放行处理，需重新检测或重洗"
        basket_judgements.append({
            "basket": bid, "desk": b["desk"], "item": b["item"],
            "soil": b["soil"], "quantity": b["quantity"],
            "plan_state": plan["state"] if plan else None,
            "plan_slot": plan["slot"] if plan else None,
            "decision": decision, "shippable_now": shippable, "action": action,
        })

    # 3) 取件承诺缺口
    orders = _orders_for_baskets(repo, basket_ids)
    open_orders = [o for o in orders if o["state"] == "已确认"
                   and o["basket"] not in handed_baskets]
    gap_items = sum(repo.basket(o["basket"])["quantity"] for o in open_orders)
    pickup_gap = {
        "confirmed_orders": [{"booking": o["booking"], "basket": o["basket"],
                             "pickup_slot": o["slot"], "day": o["day"]} for o in open_orders],
        "basket_count": len(open_orders),
        "item_count": gap_items,
        "verdict": ("需在取件时段前安排重洗/替换件，否则形成领件缺口"
                    if open_orders else "没有未交接的已确认领件"),
    }

    return {
        "check": check_id, "cycle": cycle["cycle"], "day": day,
        "timeline": timeline,
        "effective": new,
        "overturned": overturned,
        "water": water_judgement,
        "baskets": basket_judgements,
        "pickup_gap": pickup_gap,
        "immutable_handovers": handed,
    }


def all_overturns(repo: Repository) -> list[dict[str, Any]]:
    ids = sorted({r["check"] for r in repo.checks})
    out = []
    for cid in ids:
        tl = repo.checks_of(cid)
        if len(tl) > 1 and tl[-1].get("verdict") in {"撤销", "不通过"}:
            out.append(overturn_cascade(repo, cid))
    return out
