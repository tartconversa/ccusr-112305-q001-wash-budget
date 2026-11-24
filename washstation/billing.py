"""物业用量单与柜台洗护计量单：版本订正、可认定分摊、争议保留。

铁律：

- 同一单号按 version 订正，历史已签收版本仍是证据；
- 表底订正引起的是**原服务日**补收/退补，和“次日可用额度”是两件事，
  绝不能把 +2L 订正当成次日多出的配额；
- 共享预冲洗只有一张可确认计费单时，先出能认定的部分，
  其余保留为争议，不强行分摊；
- 分摊必须保留物业原始计费总额与未归属量；水费 6.1 元/m³。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .core import WATER_PRICE_PER_M3, Range, money


def statement_versions(repo, statement_id: str) -> list[dict[str, Any]]:
    return sorted((s for s in repo.statements if s["statement"] == statement_id),
                  key=lambda s: s["version"])


def counter_billing_actions(repo) -> dict[str, Any]:
    """回答“已经送到柜台的计量单要不要补发”。"""
    ids = sorted({s["statement"] for s in repo.statements})
    actions = []
    for sid in ids:
        versions = statement_versions(repo, sid)
        latest = versions[-1]
        first = versions[0]
        delta = latest["billed_l"] - first["billed_l"]
        signed = [v for v in versions if v["state"] == "已签收"]
        needs_reissue = latest["version"] > 1
        backcharge_l = max(0, delta)
        refund_l = max(0, -delta)
        actions.append({
            "statement": sid, "desk": latest["desk"], "cycle": latest["cycle"],
            "service_day": latest["service_day"],
            "latest_version": latest["version"], "latest_state": latest["state"],
            "original_billed_l": first["billed_l"],
            "latest_billed_l": latest["billed_l"],
            "delta_l": delta,
            "already_signed_versions": [v["version"] for v in signed],
            "needs_reissue": needs_reissue,
            "reissue_reason": ("表底订正：应向柜台补发订正单并补收差额"
                               if needs_reissue and delta > 0 else
                               "版本订正：应补发订正/退补单" if needs_reissue else None),
            "backcharge": {"litres": backcharge_l, **money(backcharge_l)},
            "refund": {"litres": refund_l, **money(refund_l)},
            "quota_effect": {
                "changes_next_day_quota": False,
                "note": "表底更正只订正原服务日应计/补收；次日配额由物业配额表另行切日，"
                        "更正升数不增加次日可用额度",
            },
        })
    pending = [s for s in repo.statements if s["state"] == "待核对"
               and s["version"] == max(
                   v["version"] for v in repo.statements
                   if v["statement"] == s["statement"])]
    return {
        "actions": actions,
        "pending_verification": [{"statement": s["statement"], "desk": s["desk"],
                                  "billed_l": s["billed_l"], "cycle": s["cycle"]}
                                 for s in pending],
        "principle": "待核对单只能作应计提示，不能作为已确认出账或放行/配额依据",
    }


def allocate_shared_bill(property_bill_l: float,
                         identifiable: dict[str, float],
                         disputed_claims_l: float = 0.0) -> dict[str, Any]:
    """把一张物业/共享计费总额分摊到柜台。

    - ``identifiable``：已有可认定依据（签收/订正单、独立计量）的柜台升数；
    - 可认定部分先出单；
    - 超过可认定合计的原始账单余额保留为未归属/争议，不按数量平摊；
    - 若柜台申报（争议单）与可认定部分重叠，单独列示，不重复计入分配。
    """
    identified_total = sum(identifiable.values())
    if identified_total > property_bill_l + 1e-9:
        # 可认定量超过物业账单：可能是口径/表底问题，全部挂差异
        return {
            "property_bill_l": property_bill_l,
            "identified": dict(identifiable),
            "identified_total_l": identified_total,
            "allocated": {},
            "allocated_total_l": 0,
            "unattributed_or_disputed_l": property_bill_l,
            "over_identified_l": identified_total - property_bill_l,
            "balanced": False,
            "note": "可认定量大于物业计费总额，先查表底/口径，不得强摊",
        }
    residual = property_bill_l - identified_total
    allocated = dict(identifiable)
    return {
        "property_bill_l": property_bill_l,
        "identified": dict(identifiable),
        "identified_total_l": identified_total,
        "allocated": allocated,
        "allocated_total_l": identified_total,
        "billed_now": dict(identifiable),
        "unattributed_or_disputed_l": residual,
        "counter_disputed_claim_l": disputed_claims_l,
        "settled_l": identified_total,
        "open_difference_l": residual + disputed_claims_l,
        "balanced": abs(identified_total + residual - property_bill_l) < 1e-9,
        "money_total": money(property_bill_l),
        "money_billed_now": money(identified_total),
        "money_disputed": money(residual),
        "rate_cny_per_m3": WATER_PRICE_PER_M3,
        "note": "余额保留为争议/未归属；水费按 6.1 元/m³，分摊前后总金额不变",
    }


def shared_pre_rinse_allocation(single_bill_l: float,
                                direct_identifiable_l: dict[str, float]
                                ) -> dict[str, Any]:
    """两家柜台共享预冲洗且只有一张计费单时的出单规则。

    ``single_bill_l`` 是那张唯一计费单的总量；``direct_identifiable_l`` 是
    各柜台**能直接认定**的水量（如各自支路/签收分量）。先就可认定部分出单，
    账单余额（真正共享且无法拆分的预冲洗等）整体保留为争议，
    绝不按件数或均摊把它伪装成各柜台精确水量。
    """
    res = allocate_shared_bill(single_bill_l, direct_identifiable_l)
    res["rule"] = "bill_identifiable_first_keep_shared_residual_disputed"
    res["shared_pre_rinse_disputed_l"] = res["unattributed_or_disputed_l"]
    return res


def month_end_property_bill(repo, month_prefix: str = "2026-04") -> dict[str, Any]:
    """按已确认的新水来源（new-a/new-b 累计表链）汇总物业用量单基础。

    只汇总服务日落在 ``month_prefix`` 月份的经营日 slot0->slot3 实测跨度；
    换表边界给区间。跨周表码推进代表两次抄表之间的真实用水，另行列示，
    不重复计入日跨度合计。
    """
    from .meters import fresh_day_range
    days = [d for d in repo.operating_days() if d.startswith(month_prefix)]
    exact_total = 0.0
    ranges_lo, ranges_hi = 0.0, 0.0
    day_rows = []
    for d in days:
        fr = fresh_day_range(repo, d)
        rng = Range(fr["range"]["lo"], fr["range"]["hi"])
        ranges_lo += rng.lo
        ranges_hi += rng.hi if rng.hi is not None else rng.lo
        if rng.is_exact:
            exact_total += rng.lo
        day_rows.append({"day": d, "range": rng.to_dict(),
                         "reason": fr["reason"]})

    # 柜台可认定：最新有效且状态为已签收/表底订正的计量单
    identifiable: dict[str, float] = defaultdict(float)
    disputed_claims = 0.0
    seen_latest = {}
    for s in repo.statements:
        cur = seen_latest.get(s["statement"])
        if cur is None or s["version"] > cur["version"]:
            seen_latest[s["statement"]] = s
    for s in seen_latest.values():
        if not s["service_day"].startswith(month_prefix):
            continue
        if s["state"] in {"已签收", "表底订正"}:
            identifiable[s["desk"]] += s["billed_l"]
        elif s["state"] == "待核对":
            disputed_claims += s["billed_l"]

    prop_range = Range(ranges_lo, ranges_hi)
    allocation = allocate_shared_bill(
        # 有不确定边界时，物业账单基础用区间；分摊以保守下界演示并同时给上界
        ranges_lo, dict(identifiable), disputed_claims)
    return {
        "month": month_prefix,
        "service_days": days,
        "property_fresh_bill_range": prop_range.to_dict(),
        "property_fresh_bill_exact_l": exact_total or None,
        "day_rows": day_rows,
        "confirmed_counter_identifiable_l": dict(identifiable),
        "pending_counter_claims_l": disputed_claims,
        "allocation_on_lower_bound": allocation,
        "money_range": money(prop_range),
        "rate_cny_per_m3": WATER_PRICE_PER_M3,
        "note": ("物业按已确认新水来源出账；换表/缺测边界使用区间，"
                 "最终物业总额以物业抄表累计差为准，本站分摊保留未归属"),
    }
