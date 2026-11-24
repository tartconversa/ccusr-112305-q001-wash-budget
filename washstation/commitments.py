"""每站可发已洗量、租赁承诺、低等级放行证据与退回重洗替换。

资料中没有单独的租赁合同表，因此：

- 数据可追溯的硬承诺 = ``booking_and_plan`` 中 state=已确认 的领件预约，
  以及 handover 中已交接/待放行/已预排的实物状态；
- 租赁最低可发量由调用方（或报告中的显式参数）提供，引擎负责核验，
  不凭空捏造合同数字。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .core import Repository


def confirmed_pickup_commitments(repo: Repository) -> list[dict[str, Any]]:
    handed = {(h["basket"], h["state"]) for h in repo.handovers}
    handed_ids = {h["basket"] for h in repo.handovers if h["state"] == "已交接"}
    pickup_handovers = {p["basket"]: p for p in repo.plans if p["event"] == "pickup"}
    out = []
    for p in repo.plans:
        if p["event"] != "order" or p["state"] != "已确认":
            continue
        b = repo.basket(p["basket"])
        ph = pickup_handovers.get(p["basket"])
        out.append({
            "booking": p["booking"], "basket": p["basket"], "desk": b["desk"],
            "item": b["item"], "quantity": b["quantity"],
            "deadline_day": p["day"], "deadline_slot": p["slot"],
            "collect_slot_on_basket_day": b["collect_slot"],
            "fulfilled": p["basket"] in handed_ids
            or (ph is not None and ph["state"] == "已交接"),
            "evidence": ("柜台回单/交接记录" if p["basket"] in handed_ids
                         or (ph and ph["state"] == "已交接") else None),
        })
    return out


def station_dispatch(repo: Repository) -> dict[str, dict[str, Any]]:
    """按 handover 地名（站点）汇总各状态料筐件数。"""
    stations: dict[str, dict[str, Any]] = {}
    for h in repo.handovers:
        st = stations.setdefault(h["station"], {
            "station": h["station"], "handed_over_items": 0,
            "prearranged_items": 0, "release_pending_items": 0,
            "baskets": []})
        qty = repo.basket(h["basket"])["quantity"]
        key = {"已交接": "handed_over_items", "已预排": "prearranged_items",
               "待放行": "release_pending_items"}.get(h["state"])
        if key:
            st[key] += qty
        st["baskets"].append({"basket": h["basket"], "state": h["state"],
                              "when": f'{h["day"]}#{h["slot"]}', "tank": h["tank"],
                              "quantity": qty})
    return stations


@dataclass
class LeaseCommitment:
    station: str
    min_dispatchable_items: int       # 租赁原有承诺：每班/每日最低可发件数
    period: str = "next_shift"


def verify_lease_commitments(repo: Repository,
                            leases: list[LeaseCommitment]) -> list[dict[str, Any]]:
    dispatch = station_dispatch(repo)
    out = []
    for lease in leases:
        st = dispatch.get(lease.station, {})
        # 可发出 = 已交接 + 待放行中可放行的部分；保守起见这里只把已交接视为已满足，
        # 待放行单独计入“若检测通过可补”。
        handed = st.get("handed_over_items", 0)
        pending = st.get("release_pending_items", 0)
        pre = st.get("prearranged_items", 0)
        out.append({
            "station": lease.station, "period": lease.period,
            "lease_min_items": lease.min_dispatchable_items,
            "handed_over_items": handed,
            "release_pending_items": pending,
            "prearranged_items": pre,
            "meets_commitment_now": handed >= lease.min_dispatchable_items,
            "gap_items_now": max(0, lease.min_dispatchable_items - handed),
            "could_close_if_released": handed + pending + pre,
        })
    return out


def shipped_low_grade_evidence(repo: Repository) -> list[dict[str, Any]]:
    """已交接料筐的原始检测与使用证据（无论后来结论是否被推翻）。"""
    out = []
    for h in repo.handovers:
        if h["state"] != "已交接":
            continue
        b = repo.basket(h["basket"])
        cycle = next((c for c in repo.operations if h["basket"] in c["baskets"]), None)
        check_versions = []
        if cycle:
            cids = {c_row["check"] for c_row in repo.checks if c_row["cycle"] == cycle["cycle"]}
            for cid in cids:
                check_versions.extend(repo.checks_of(cid))
        out.append({
            "basket": h["basket"], "station": h["station"],
            "handed_at": f'{h["day"]}#{h["slot"]}', "tank_at_handover": h["tank"],
            "desk": b["desk"], "soil": b["soil"], "quantity": b["quantity"],
            "cycle": cycle["cycle"] if cycle else None,
            "program": cycle["program"] if cycle else None,
            "original_check_versions": check_versions,
            "use_evidence": {
                "recorded_reused_l": cycle.get("reused_l") if cycle else None,
                "recorded_recovered_l": cycle.get("recovered_l") if cycle else None,
                "handover_event": h["event"],
            },
            "retention_rule": "保留放行时所依据的检测版本与用水/交接证据，"
                              "后续重算不覆盖、不删除",
        })
    return out


def rewash_replacement_plan(repo: Repository, affected_baskets: list[str],
                            now_day: str, now_slot: int,
                            rewash_slots_needed: int = 2,
                            replacement_stock_available: bool = True
                            ) -> dict[str, Any]:
    """按退回重洗期限判断哪些领件必须在下次领件前替换。

    判定：从当前时点起需要 ``rewash_slots_needed`` 个时段才能完成重洗/重检，
    若完成时刻晚于领件截止，则该承诺必须用替换件先顶上。
    """
    commitments = {c["basket"]: c for c in confirmed_pickup_commitments(repo)}
    items = []
    for bid in affected_baskets:
        b = repo.basket(bid)
        c = commitments.get(bid)
        deadline_slot = c["deadline_slot"] if c else b["collect_slot"]
        deadline_day = c["deadline_day"] if c else b["day"]
        # 简化的同日时段推进（跨日由调用方把 now_day 与 deadline_day 对齐）
        finish_slot = now_slot + rewash_slots_needed
        can_rewash_before_deadline = (
            deadline_day > now_day or
            (deadline_day == now_day and finish_slot <= deadline_slot))
        needs_replacement = not can_rewash_before_deadline
        items.append({
            "basket": bid, "quantity": b["quantity"], "desk": b["desk"],
            "deadline": f"{deadline_day}#{deadline_slot}",
            "rewash_finish_earliest_slot": finish_slot,
            "can_rewash_before_deadline": can_rewash_before_deadline,
            "needs_replacement_pickup": needs_replacement,
            "replacement_available": replacement_stock_available if needs_replacement else None,
            "action": ("先用替换件满足领件，原件退回重洗并保留证据链"
                       if needs_replacement else "可安排退回重洗后在截止前再放行"),
        })
    return {"now": f"{now_day}#{now_slot}",
            "rewash_slots_needed": rewash_slots_needed,
            "items": items,
            "replacement_item_count": sum(i["quantity"] for i in items
                                          if i["needs_replacement_pickup"])}
