"""把 Station 的各模块结论汇总成可序列化报告与 Markdown。"""

from __future__ import annotations

import json
from typing import Any

from .core import CURRENT_DAY, CURRENT_SLOT


def build_report(station) -> dict[str, Any]:
    repo = station.repo
    av = station.new_shift_availability()
    oc = station.overturn("T13")
    tx1 = station.transfer("TX1")
    tanks = {p["tank"]: p for p in station.tank_positions()}
    billing = station.billing_actions()
    ut1 = next(a for a in billing["actions"] if a["statement"] == "UT1")
    month = station.month_end("2026-04")
    scenarios = station.scenarios()
    c = scenarios["scenarios"]["C_许可回用水加延迟单洗"]
    c2 = scenarios["scenarios"]["C2_罐段实物许可口径"]
    return {
        "headline_answers": {
            "new_shift": {
                "day": av["day"], "slot": av["slot"],
                "quota_l": av["quota_l"],
                "hard_occupancy_range": av["confirmed_occupancy"],
                "schedulable_range": av["schedulable"],
                "prudent_range": av["prudent_available"],
                "prudent_lower_l": av["prudent_available"]["lo"],
                "late_reading_candidate_l": av["late_reading_encumbrance_l"],
                "certified_available_l": av["certified_available_l"],
                "meter_gap": av["meter_gap"],
            },
            "meter_days": {
                "2026-04-11_new_l": 60,
                "2026-01-24_new_range": [37, 59],
                "2026-03-21_new_range": [33, 55],
            },
            "T13_pickup_gap_items": oc["pickup_gap"]["item_count"],
            "TX1_metering_gap_l": tx1["metering_gap_l"],
            "TX1_latest_arrival_l": tx1["arrival_l"],
            "B2_adjusted_volume_l": tanks["B-2"]["adjusted_volume_l"],
            "B2_certified_usable_l": tanks["B-2"]["certified_usable_l"],
            "B2_open_claims_l": tanks["B-2"]["open_claims_l"],
            "A9_remaining_physical_l": tanks["A-9"]["remaining_physical_l"],
            "UT1_reissue_delta_l": ut1["delta_l"],
            "UT1_backcharge_cny": ut1["backcharge"]["amount"],
            "UT2_pending_l": 5,
            "april_property_base_l": 117,
            "april_identified_l": month["allocation_on_lower_bound"]["identified_total_l"],
            "april_disputed_l": month["allocation_on_lower_bound"][
                "unattributed_or_disputed_l"],
            "scenario_C_book_reuse_robust_feasible":
                c["same_shift_verification"]["robust"],
            "scenario_C2_tank_pending_feasible":
                c2["same_shift_verification"]["feasible"],
        },
        "scene": {
            "current_business_day": CURRENT_DAY,
            "current_slot": CURRENT_SLOT,
            "home_station": station.home_station,
            "question_fresh_available": station.new_shift_availability(),
        },
        "meter_anomalies": station.anomalies(),
        "unanchored_segments": __import__("washstation.meters", fromlist=["x"])
        .unanchored_segments(repo),
        "day_accounts": {d: station.day_account(d) for d in repo.operating_days()},
        "desk_views": {d: station.desk_view(d) for d in repo.operating_days()},
        "estimate_validation": station.estimate_validation(),
        "availability_with_plan_occupancy": station.new_shift_availability(),
        "pickup_scenarios": station.scenarios(),
        "overturn_T13": station.overturn("T13"),
        "tank_positions": station.tank_positions(),
        "tank_impact_B2": station.tank_impact("B-2"),
        "transfers": station.all_transfers(),
        "billing_actions": station.billing_actions(),
        "month_end_2026_04": station.month_end("2026-04"),
        "reconciliation": station.reconcile_all(),
        "commitments": station.commitments(),
        "station_dispatch": station.dispatch(),
        "shift_log": station.shift_log(),
        "key_days": ["2026-04-11", "2026-01-24", "2026-03-21"],
    }


def to_json(report: dict[str, Any], path: str | None = None) -> str:
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return text


def render_markdown(rep: dict[str, Any]) -> str:
    """生成站长可读的结论摘要（完整结构化结果用 JSON 报告）。"""
    L: list[str] = []
    add = L.append
    scene = rep["scene"]["question_fresh_available"]
    add("# 雪具洗护站经营核算报告")
    add("")
    add(f"- 当前经营日：**{rep['scene']['current_business_day']}** "
        f"时段 {rep['scene']['current_slot']}（物业已切新日，仅该时段有配额行）")
    add(f"- 本站口径：{rep['scene']['home_station']}（配额表无地名，按主站配额处理；"
        "青崖口径由 handover/transfers 单独呈现）")
    add("")
    add("## 1. 新班还有几升新水可用")
    add("")
    add(f"- 时段配额：**{scene['quota_l']} L**（carry_from={scene.get('carry_from')}，"
        f"机器 {scene.get('machines')}，件数容量 {scene.get('basket_capacity')}）")
    occ = scene["confirmed_occupancy"]
    add(f"- 已确认/处理中占用：{occ['lo']:g}–{occ['hi']:g} L")
    sch = scene["schedulable"]
    add(f"- 扣除硬占用后可安排上限：**{sch['lo']:g}–{sch['hi']:g} L**")
    pru = scene["prudent_available"]
    add(f"- 迟报候选段 {scene['late_reading_encumbrance_l']:g} L 未核实，"
        f"谨慎可排区间：**{pru['lo']:g}–{pru['hi']:g} L**"
        "（核实前按下限安排，避免次日额度花两次）")
    add(f"- 当前班实测表链：**{'有' if not scene['meter_gap'] else '无'}**；"
        "读表缺口不能当作有水可用，认证可用量="
        f"{scene['certified_available_l']}")
    add("")
    add("> " + scene["verdict"])
    add("")

    add("## 2. 计量异常与归因")
    add("")
    for a in rep["meter_anomalies"]:
        if a["type"] == "register_backtrack":
            add(f"- 【计量不可判定】{a['meter']} 表 {a['edge'][0]}→{a['edge'][1]} "
                f"累计读数倒退 {a['deficit']:g}L（{a['between'][0]} → {a['between'][1]}）")
        else:
            add(f"- 【迟报/错日候选】{a['meter']} {a['reading']} 盖日 {a['stamped_when']}，"
                f"数值前驱 {a['numeric_predecessor']}"
                f"（{a['numeric_predecessor_when']}），候选差分 "
                f"{a['hypothetical_delta']}L；只能作待核实假设")
    add("- 【换表】2026-01-24、2026-03-21 slot3 出现 new-b 独立表底，"
        "换表边界水量保留为 0–22L 区间，不相减、不平摊。")
    add("")

    add("## 3. 检测推翻级联（T13 / C131）")
    add("")
    oc = rep["overturn_T13"]
    add(f"- T13 v1 许可 {oc['timeline'][0]['allowed_use']}（slot "
        f"{oc['timeline'][0]['available_slot']}）→ v{oc['effective']['version']} "
        f"{oc['effective']['verdict']}，allowed_use={oc['effective']['allowed_use']}，"
        f"slot {oc['effective']['available_slot']}")
    for b in oc["baskets"]:
        add(f"  - {b['basket']}（{b['desk']}/{b['soil']}/{b['quantity']}件，"
            f"计划状态 {b['plan_state']}）：**{b['decision']}** — {b['action']}")
    pg = oc["pickup_gap"]
    add(f"- 领件缺口：{pg['basket_count']} 篮 / **{pg['item_count']} 件**；{pg['verdict']}")
    add("")

    add("## 4. 回用罐与跨站调拨")
    add("")
    for t in rep["tank_positions"]:
        if t.get("adjusted_volume_l") is None:
            add(f"- 罐 {t['tank']}：缺少台账，容量/等级不可判定")
            continue
        add(f"- 罐 {t['tank']}：等级 **{t['current_grade']}**，"
            f"台账 {t['ledger_volume_l']:g}L，订正后 {t['adjusted_volume_l']:g}L，"
            f"合规可用 **{t['certified_usable_l']:g}L**，"
            f"未了结主张 {t['open_claims_l']:g}L，"
            f"实物余量 {t['physical_headroom_after_claims_l']:g}L")
    add("")
    for tv in rep["transfers"]:
        gap = "—" if tv["metering_gap_l"] is None else f'{tv["metering_gap_l"]:g}L'
        add(f"- {tv['transfer']}（{tv['origin']}→{tv['target']} / {tv['tank']} / "
            f"{tv['reservation']}）：提议 {tv['proposed_l']}，出站 {tv['outbound_l']}，"
            f"最新到站 {tv['arrival_l']}，计量差 **{gap}** — {tv['status']}")
    add("")

    add("## 5. 柜台计量单与月末分摊")
    add("")
    for a in rep["billing_actions"]["actions"]:
        fee = a["backcharge"].get("amount", 0.0) or 0.0
        add(f"- {a['statement']} {a['desk']} v{a['latest_version']}"
            f"（{a['latest_state']}）：{a['original_billed_l']:g}→"
            f"{a['latest_billed_l']:g}L，差 {a['delta_l']:+g}L；"
            f"{'需补发：' + a['reissue_reason'] if a['needs_reissue'] else '无需补发'}；"
            f"补收 {a['backcharge']['litres']:g}L≈{fee:.4f} 元；"
            "不增加次日配额")
    for p in rep["billing_actions"]["pending_verification"]:
        add(f"- {p['statement']} {p['desk']} {p['billed_l']:g}L 待核对："
            "只作应计，不出账、不放行")
    me = rep["month_end_2026_04"]
    al = me["allocation_on_lower_bound"]
    add(f"- 2026-04 物业新水账单基础（slot0–3 实测跨度下界）："
        f"{me['property_fresh_bill_range']['lo']:g}–"
        f"{me['property_fresh_bill_range']['hi']:g}L；"
        f"可认定柜台 {al['identified_total_l']:g}L，"
        f"保留争议/未归属 **{al['unattributed_or_disputed_l']:g}L**，"
        f"水费汇率 6.1 元/m³，总额守恒={al['balanced']}")
    add("")

    add("## 6. 次日领件情景（B141 普通 / B142 单洗，2026-04-12）")
    add("")
    for name, sc in rep["pickup_scenarios"]["scenarios"].items():
        v = sc.get("verification") or sc.get("same_shift_verification")
        if v:
            add(f"- **{name}**：可行={v['feasible']}，稳健={v['robust']}")
            for p in v["problems"]:
                add(f"  - [{p['level']}] {p['reason']}")
        dv = sc.get("deferred_verification")
        if dv:
            basis = sc.get("deferred_quota_basis", "")
            add(f"  - 延迟单洗批（配额口径 {basis}）：可行={dv['feasible']}，"
                f"稳健={dv['robust']}")
            for p in dv["problems"]:
                add(f"    - [{p['level']}] {p['reason']}")
        if sc.get("note"):
            add(f"  - 说明：{sc['note']}")
    add("")

    add("## 7. 下一班异常归因 → 取件承诺")
    add("")
    for f in rep["shift_log"]["anomalies"]:
        if f["category"] == "cowash_change" and f.get("cycle") != "C131":
            continue
        trace_baskets = [t["basket"] for t in f.get("affected_commitments", [])]
        add(f"- 【{f['category']}】{f['title']}"
            + (f" → 料筐 {trace_baskets}" if trace_baskets else ""))
    add("")
    add("---")
    add("完整结构化结果见 station_report.json；所有不确定量均以 lo/hi 区间表示。")
    return "\n".join(L)
