"""接收下一班记录，把时段用水异常归因到三类原因，并追到取件承诺。

三类互斥主因（可叠加证据时给出主次）：

- ``meter_undecidable`` 计量不可判定：换表独立表底、缺测、迟报/错日链冲突；
- ``reuse_failure`` 回用失效：检测撤销/无许可、待测水混罐，回用水不能省新水；
- ``cowash_change`` 一次真实的合洗/程序变更：实物洗护记录显示并洗或程序改变。

每一类都尽量追到受影响的领件承诺（booking 已确认单 / handover 实物状态）。
"""

from __future__ import annotations

from typing import Any

from .core import Repository
from .meters import individual_meter_days, register_anomalies
from .quality import all_overturns
from .tanks import tank_positions


def _commitment_trace(repo: Repository, basket_ids: list[str]) -> list[dict[str, Any]]:
    trace = []
    for bid in basket_ids:
        orders = [{"booking": p["booking"], "state": p["state"],
                   "deadline": f'{p["day"]}#{p["slot"]}'}
                  for p in repo.plans if p["event"] == "order" and p["basket"] == bid]
        hos = [{"event": h["event"], "state": h["state"],
                "when": f'{h["day"]}#{h["slot"]}', "station": h["station"]}
               for h in repo.handovers if h["basket"] == bid]
        trace.append({"basket": bid, "orders": orders, "handovers": hos})
    return trace


def classify_anomalies(repo: Repository) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    # 1) 计量不可判定
    for a in register_anomalies(repo):
        title = ("累计读数倒退/表链冲突" if a["type"] == "register_backtrack"
                 else "迟报/错日候选读数")
        findings.append({
            "category": "meter_undecidable",
            "title": title,
            "anomaly_type": a["type"],
            "evidence": a,
            "affected_commitments": [],
            "explanation": a["verdict"],
        })
    ind = individual_meter_days(repo)
    missing_ind = [d for d, v in ind.items() if not v["measured"]]
    if missing_ind:
        findings.append({
            "category": "meter_undecidable",
            "title": "独立支路缺测日",
            "days": missing_ind,
            "explanation": "individual 在这些经营日全天 null：不能按 0 处理，"
                           "也不能把读表缺口当作有水；只能用估算区间并保留核验缺口",
            "affected_commitments": [],
        })
    change_days = sorted({r["day"] for r in repo.meters if r["meter"] == "new-b"})
    if change_days:
        findings.append({
            "category": "meter_undecidable",
            "title": "换表独立表底",
            "days": change_days,
            "explanation": "new-a -> new-b 表名变化，两表底数不能相减，"
                           "换表边界时段水量只能给 0..时段水额 的区间",
            "affected_commitments": [],
        })

    # 2) 回用失效（检测推翻 + 待测混罐）
    for oc in all_overturns(repo):
        basket_ids = [b["basket"] for b in oc["baskets"]]
        findings.append({
            "category": "reuse_failure",
            "title": f'检测单 {oc["check"]} 被{oc["effective"]["verdict"]}',
            "check": oc["check"], "cycle": oc["cycle"],
            "evidence": {"water": oc["water"], "pickup_gap": oc["pickup_gap"]},
            "affected_commitments": _commitment_trace(repo, basket_ids),
            "explanation": ("allowed_use 变为空：未用回用水冻结，未交接已洗篮暂停放行，"
                            "已确认领件出现缺口；已交接部分凭回单固定"),
        })
    for pos in tank_positions(repo):
        if pos.get("current_grade") == "待测":
            basket_ids = [d["basket"] for d in pos.get("demands", [])
                          if d["kind"] == "basket_link"]
            findings.append({
                "category": "reuse_failure",
                "title": f'罐 {pos["tank"]} 段为待测/含待检水',
                "tank": pos["tank"],
                "evidence": {"adjusted_volume_l": pos.get("adjusted_volume_l"),
                             "certified_usable_l": pos.get("certified_usable_l"),
                             "demands": pos.get("demands")},
                "affected_commitments": _commitment_trace(repo, basket_ids),
                "explanation": "整段按待测处理，没有对应用途许可前 certified_usable=0："
                               "罐里有水也不能给这些料筐省新水，且影响所有后续预留",
            })

    # 3) 真实合洗/程序变更
    # 3a 程序更新日（常用 -> 节水）
    prev_prog = None
    for d in repo.operating_days():
        progs = {c["program"] for c in repo.cycles_for_day(d) if c["program"] != "单洗"}
        prog = sorted(progs)[0] if progs else None
        if prog and prev_prog and prog != prev_prog:
            findings.append({
                "category": "cowash_change",
                "title": f"{d} 合洗程序由 {prev_prog} 切换为 {prog}",
                "day": d, "from": prev_prog, "to": prog,
                "explanation": "这是真实程序变更，解释前后时段水量/件数口径变化时"
                                "必须与季节周相、批量差异分开陈述",
                "affected_commitments": [],
            })
        if prog:
            prev_prog = prog
    # 3b 同一批实物合洗多篮（真实并洗），且排程预留状态与实物经历不同
    for c in repo.operations:
        if len(c["baskets"]) > 1:
            plan_states = [p for p in repo.plans if p.get("cycle") == c["cycle"]]
            if plan_states and len({p["state"] for p in plan_states}) > 1:
                basket_ids = list(c["baskets"])
                findings.append({
                    "category": "cowash_change",
                    "title": f'批 {c["cycle"]} 实物合洗但排程状态分化',
                    "cycle": c["cycle"],
                    "evidence": {"baskets": basket_ids, "program": c["program"],
                                 "plan_states": plan_states},
                    "affected_commitments": _commitment_trace(repo, basket_ids),
                    "explanation": ("实物记录显示多篮真实并洗；排程中同时存在处理中与未开洗，"
                                    "用水异常应归因于这次合洗/时段调整，而非平摊或误记表损"),
                })
    return findings


def explain_slot_anomaly(repo: Repository, day: str, slot: int) -> dict[str, Any]:
    """解释某时段用水异常的来源，并追到取件承诺。

    返回主因 ``primary_category``（三类之一或 none）与并存因素。判定顺序：
    计量窗口不可测 -> meter_undecidable；否则看回用水许可是否失效
    -> reuse_failure；再看该时段是否存在真实合洗/程序/排程分化
    -> cowash_change。后两者可作为并存因素同时返回。
    """
    from .meters import meter_windows

    ws = {w.slot: w for w in meter_windows(repo, day, "new-a")}
    w = ws.get(slot)
    cycles = [c for c in repo.cycles_for_day(day) if c["slot"] == slot]
    basket_ids = [b for c in cycles for b in c["baskets"]]

    result: dict[str, Any] = {"day": day, "slot": slot,
                              "cycles": [c["cycle"] for c in cycles]}
    # 计量层
    if w is None or w.status != "measured":
        result["primary_category"] = "meter_undecidable"
        result["meter"] = (w.to_dict() if w else
                           {"status": "missing", "detail": "该时段没有可差分的新水表窗"})
        result["explanation"] = "换表/缺测/表链冲突使该时段水量不可判定，只能给区间"
    else:
        result["measured_fresh_l"] = w.litres
        result["primary_category"] = "none"
        result["explanation"] = "该时段新水表链可实测"

    factors: list[str] = []
    # 回用层：关联批的有效检测是否失效
    checks = repo.effective_checks()
    for c in cycles:
        chk = next((v for v in checks.values() if v["cycle"] == c["cycle"]), None)
        if chk is not None and (chk.get("verdict") in {"撤销", "不通过"}
                                or not chk.get("allowed_use")):
            factors.append("reuse_failure")
            result.setdefault("reuse_failures", []).append(
                {"cycle": c["cycle"], "check": chk["check"],
                 "verdict": chk["verdict"], "allowed_use": chk.get("allowed_use")})
    # 合洗/程序层
    for c in cycles:
        if len(c["baskets"]) > 1:
            factors.append("cowash_change")
            result.setdefault("cowash", []).append(
                {"cycle": c["cycle"], "program": c["program"],
                 "baskets": c["baskets"],
                 "note": "多篮真实合洗，时段水量由整批共享，不能按柜台精确拆分"})
        plan_rows = [p for p in repo.plans if p.get("cycle") == c["cycle"]]
        if len({p["state"] for p in plan_rows}) > 1:
            factors.append("cowash_change")
            result.setdefault("plan_state_split", plan_rows)
    result["contributing_categories"] = sorted(set(factors))
    if result["primary_category"] == "none" and factors:
        # 计量可测但异常观感来自回用失效或合洗变更
        result["primary_category"] = factors[0]
        result["explanation"] = {
            "reuse_failure": "表计可测，但回用水许可失效使本可抵扣的新水无法抵扣",
            "cowash_change": "表计可测，水量变化来自一次真实合洗/程序或排程状态变更",
        }.get(factors[0], "")
    result["affected_commitments"] = _commitment_trace(repo, basket_ids)
    return result


def receive_shift_log(repo: Repository,
                      incoming: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """站长接收下一班记录。

    ``incoming`` 可传入下一班交来的记录数组；缺省使用 handover 表全部记录。
    每条记录做身份/状态核对，再附上全局异常归因与承诺追踪。
    """
    incoming = repo.handovers if incoming is None else incoming
    findings = classify_anomalies(repo)
    by_basket: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        for t in f.get("affected_commitments", []):
            by_basket.setdefault(t["basket"], []).append(f["category"])

    entries = []
    for rec in incoming:
        bid = rec["basket"]
        entries.append({
            "record": rec,
            "anomaly_categories_for_basket": sorted(set(by_basket.get(bid, []))),
            "commitment_trace": _commitment_trace(repo, [bid])[0],
            "accepted": rec["state"] in {"已交接", "已预排", "待放行"},
        })

    category_counts: dict[str, int] = {}
    for f in findings:
        category_counts[f["category"]] = category_counts.get(f["category"], 0) + 1
    return {
        "received_records": len(entries),
        "entries": entries,
        "anomaly_counts": category_counts,
        "anomalies": findings,
        "handoff_rule": "已交接实物以柜台回单为准并锁定；下一班重算排程不得覆盖",
    }
