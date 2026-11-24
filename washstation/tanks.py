"""回用罐段、混合等级传播与同罐预留充足性。

口径（data/README.md）：

- ``tank`` 是使用范围共享的储罐；
- ``parent_tanks`` 指已混合的入水来源，混合后原入水等级不能独立用于批准出罐；
- 掺入一股“待测”水后，整段混合水按待测处理，必须重新判断所有指向该罐段的
  当前单与后续预留，而不是只影响眼前那一张单；
- 罐里有实物水 ≠ 有对应用途的检测许可：``certified_usable_l`` 可能为 0。
"""

from __future__ import annotations

from typing import Any

from .core import Range, Repository
from .transfers import all_transfers


def _rows_for(repo: Repository, tank: str) -> list[dict[str, Any]]:
    return sorted((r for r in repo.tanks if r["tank"] == tank),
                  key=lambda r: (r["day"], r["slot"]))


def tank_demands(repo: Repository) -> dict[str, list[dict[str, Any]]]:
    """所有指向罐的预留/交接/调拨主张（升数可能未量化）。"""
    demands: dict[str, list[dict[str, Any]]] = {}
    for h in repo.handovers:
        demands.setdefault(h["tank"], []).append({
            "kind": "basket_link", "ref": h["event"], "basket": h["basket"],
            "state": h["state"], "when": f'{h["day"]}#{h["slot"]}',
            "litres": None,
            "note": "交接记录指向该罐段，水量需按洗护批估算，未直接给升数",
        })
    for t in repo.transfers:
        if t["kind"] in {"预留", "争用"}:
            demands.setdefault(t["tank"], []).append({
                "kind": "transfer_claim", "ref": t["reservation"],
                "transfer": t["transfer"], "state": t["kind"],
                "when": f'{t["day"]}#{t["slot"]}', "litres": t["litres"],
                "direction": f'{t["origin"]}->{t["target"]}',
            })
    return demands


def tank_positions(repo: Repository) -> list[dict[str, Any]]:
    transfers = {v.transfer: v for v in all_transfers(repo)}
    demands = tank_demands(repo)
    out: list[dict[str, Any]] = []

    for tank in sorted({r["tank"] for r in repo.tanks}):
        rows = _rows_for(repo, tank)
        inflow_l = sum(r["litres"] for r in rows if r["event"] == "配入")
        mixes = [r for r in rows if r["event"] == "混合"]
        last_mix = mixes[-1] if mixes else None
        # 混合事件给出的是混合后整段体积（= 各股入水之和），不是额外加入。
        physical_l = float(last_mix["litres"]) if last_mix else float(inflow_l)

        # 跨站到站更正对配入量的订正（TX1 S3=9 -> S5=10，影响 B-2 来源 A-9）
        corrections: list[dict[str, Any]] = []
        adjusted_l = physical_l
        for tv in transfers.values():
            if tv.arrival_history and len(tv.arrival_history) > 1:
                first, latest = tv.arrival_history[0], tv.arrival_history[-1]
                delta = latest["litres"] - first["litres"]
                # 台账中以 parent 身份记录该调拨来源的罐
                linked = [r for r in rows
                          if tv.tank in (r.get("parent_tanks") or [])
                          or (r["event"] == "配入" and r.get("parent_tanks") == [tv.tank])]
                if linked and delta:
                    adjusted_l += delta
                    corrections.append({"transfer": tv.transfer, "delta_l": delta,
                                        "reason": "到站更正替换同端读数，混合段体积同步订正",
                                        "linked_rows": [r.get("tank") for r in linked]})

        # 等级与许可：以最后一次混合/配入形成的当前段等级为准。
        current_grade = last_mix["grade"] if last_mix else rows[-1]["grade"]
        parent_lineage = sorted({p for r in rows for p in (r.get("parent_tanks") or [])})
        pending_fraction = any(r.get("grade") == "待测" for r in rows) or current_grade == "待测"

        # 已出站确认的调出量（从本罐实物中离开）
        outbound_l = sum(tv.origin_locked_l for tv in transfers.values()
                         if tv.tank == tank and tv.outbound_l is not None
                         and any(e["tank"] == tank and e["kind"] == "出站" for e in tv.events))
        remaining_after_outbound = max(0.0, adjusted_l - outbound_l) if not mixes \
            else adjusted_l  # 混合罐的出站主张通常尚未执行，不在此扣减来源罐

        tank_demands_here = demands.get(tank, [])
        # 未了结的量化主张：已出站的预留不再重复计为索取（已走出站扣减）；
        # 只有“争用”或尚未出站的预留才算 open claim。
        open_claim_l = 0.0
        for tv in transfers.values():
            if tv.tank != tank:
                continue
            if tv.outbound_l is None and tv.proposed_l is not None:
                open_claim_l += tv.proposed_l
            for e in tv.events:
                if e["kind"] == "争用":
                    open_claim_l += e["litres"]
        quantified_claims = open_claim_l

        certified_usable = 0.0 if current_grade in {"待测", "停用", "不合格"} else adjusted_l
        out.append({
            "tank": tank,
            "current_grade": current_grade,
            "parent_lineage": parent_lineage,
            "has_pending_test_water": pending_fraction,
            "ledger_volume_l": physical_l,
            "adjusted_volume_l": adjusted_l,
            "outbound_confirmed_l": outbound_l,
            "remaining_physical_l": remaining_after_outbound,
            "certified_usable_l": certified_usable,
            "open_claims_l": quantified_claims,
            "quantified_claims_l": quantified_claims,
            "physical_headroom_after_claims_l": remaining_after_outbound - quantified_claims,
            "compliance_headroom_l": certified_usable - quantified_claims,
            "corrections": corrections,
            "demands": tank_demands_here,
            "grade_scope_note": (
                "当前段含待测水/等级待测：整段不得凭原入水普通等级批准出罐，"
                "在对应用途检测通过前 certified_usable_l=0"
                if pending_fraction else "等级明确，可按检测许可用途使用"),
        })

    # 仅出现在调拨/交接、没有 tank_ledger 行的罐也列出来（证据不完整）
    known = {p["tank"] for p in out}
    for tank, ds in demands.items():
        if tank not in known:
            out.append({"tank": tank, "current_grade": None, "parent_lineage": [],
                        "has_pending_test_water": None, "ledger_volume_l": None,
                        "adjusted_volume_l": None, "certified_usable_l": None,
                        "demands": ds,
                        "note": "只有调拨/交接指向，缺少罐台账，容量与等级不可判定"})
    return out


def grade_change_impact(repo: Repository, tank_id: str) -> dict[str, Any]:
    """等级变为待测/许可收窄时，列出所有受影响的当前单与后续预留。"""
    pos = next((p for p in tank_positions(repo) if p["tank"] == tank_id), None)
    if pos is None:
        raise KeyError(f"未知罐 {tank_id}")
    blocked = []
    for d in pos.get("demands", []):
        blocked.append({**d, "impact": "不得使用该罐水节省新水/用于放行，须改用新水或待检测通过"})
    return {
        "tank": tank_id,
        "current_grade": pos.get("current_grade"),
        "physical_l": pos.get("adjusted_volume_l"),
        "certified_usable_l": pos.get("certified_usable_l"),
        "affected_claims": blocked,
        "sufficiency": {
            "quantified_claims_l": pos.get("quantified_claims_l"),
            "physical_headroom_l": pos.get("physical_headroom_after_claims_l"),
            "compliance_headroom_l": pos.get("compliance_headroom_l"),
            "verdict": (
                "实物量即便足够，合规可用量为 0：所有料筐主张都不能据此省新水；"
                "量化调拨主张与未量化料筐主张同时竞争同一段，检测通过后仍需重算充足性"
                if pos.get("certified_usable_l") == 0 else "按许可用途重算预留充足性"),
        },
    }
