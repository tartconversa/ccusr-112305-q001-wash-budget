"""合洗约定、时段约束核验、确认排程账簿与次日领件情景比较。

一次安排同时核验四件事：

1. 时段新水额（含已确认占用，不重复花额度）；
2. 机器（单洗独用一台机器一个时段）；
3. 料筐时间（时段件数容量、不得晚于领件时段）；
4. 回用水许可（没有对应用途检测许可时，罐里有水也不能省新水）。

不确定的用水需求用区间表达，同时给“名义可行”和“稳健可行”两种结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .core import Range
from .quota import Occupancy

# 洗护约定：哪些 program 允许哪些等级合洗；单洗永远独占。
COWASH_PROGRAMS = {"常用", "节水"}
EXCLUSIVE_SOIL = {"单洗"}
SOIL_RANK = {"普通": 1, "污染": 2, "重污": 3, "单洗": 9}


def co_wash_allowed(baskets: list[dict[str, Any]], program: str | None = None
                    ) -> dict[str, Any]:
    """按洗护约定判断一篮/多篮能否同批。

    返回 ``allowed``、原因，以及合并后需要满足的放行条件。污染等级不同的
    一次合并可能改变释放条件：按最严等级重新判定许可与检测要求。
    """
    soils = {b["soil"] for b in baskets}
    if len(baskets) <= 1:
        return {"allowed": True, "reason": "单篮成批",
                "release_condition": "按该篮等级正常检测放行"}
    if soils & EXCLUSIVE_SOIL:
        return {"allowed": False,
                "reason": "洗护约定规定“单洗”料筐必须单独用机器，不能与其他料筐合洗",
                "release_condition": None}
    if program is not None and program not in COWASH_PROGRAMS:
        return {"allowed": False, "reason": f"程序 {program} 不支持多篮合洗",
                "release_condition": None}
    if len(soils) > 1:
        strictest = max(soils, key=lambda s: SOIL_RANK.get(s, 1))
        return {"allowed": True, "conditional": True,
                "reason": f"不同污染等级合并，按最严等级“{strictest}”执行",
                "release_condition": (
                    f"整批释放条件提升到 {strictest}：回用水许可与检测均须覆盖该等级，"
                    "否则整批改用新鲜水并重新检测")}
    return {"allowed": True, "reason": "同属普通等级，常用/节水程序允许并洗",
            "release_condition": "普通等级检测许可"}


@dataclass
class DemandStandard:
    """从历史同时段表窗得到的透明需水区间（升/批）。"""

    cowash_fresh: Range = field(default_factory=lambda: Range(17, 21))
    single_fresh: Range = field(default_factory=lambda: Range(18, 22))

    def for_program(self, program: str) -> Range:
        return self.single_fresh if program == "单洗" else self.cowash_fresh


def calibrate_demand(repo) -> DemandStandard:
    """用历史 slot1（合洗）/slot2（单洗）实测窗口校准需水区间。"""
    from .meters import meter_windows
    co, single = [], []
    for c in repo.operations:
        ws = {w.slot: w for w in meter_windows(repo, c["day"], "new-a")}
        w = ws.get(c["slot"])
        if w and w.status == "measured":
            (single if c["program"] == "单洗" else co).append(w.litres)
    return DemandStandard(
        cowash_fresh=Range(min(co), max(co)) if co else Range(17, 21),
        single_fresh=Range(min(single), max(single)) if single else Range(18, 22),
    )


def fresh_demand(repo, program: str, certified_reuse_l: float = 0.0,
                 standard: DemandStandard | None = None) -> Range:
    std = standard or calibrate_demand(repo)
    gross = std.for_program(program)
    return Range(max(0.0, gross.lo - certified_reuse_l),
                 max(0.0, gross.hi - certified_reuse_l))


@dataclass
class Assignment:
    """一个洗护批：同一时段、同一机器上的一个程序，可含多个合洗篮。"""

    baskets: list[str]
    day: str
    slot: int
    program: str
    machine: str = "A"
    reuse_l: float = 0.0
    reuse_source: str | None = None
    ref: str = ""
    state: str = "已确认"          # 已确认 / 处理中 / 未开洗 / 建议
    locked: bool = False           # 已交接或处理中 -> 不可被重算覆盖

    @property
    def basket(self) -> str:
        return self.baskets[0]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def fresh_demand_from_assignment(a: Assignment) -> Range:
    gross = Range(18, 22) if a.program == "单洗" else Range(17, 21)
    return Range(max(0, gross.lo - a.reuse_l), max(0, gross.hi - a.reuse_l))


class ScheduleBook:
    """内存中的确认排程与不可变交接锁。"""

    def __init__(self, protected_baskets: set[str] | None = None) -> None:
        self.assignments: list[Assignment] = []
        # 已交接实物料筐：任何重算排程都不得再排/覆盖
        self.protected_baskets: set[str] = set(protected_baskets or [])

    def add(self, a: Assignment) -> None:
        self.assignments.append(a)

    def protect(self, basket_ids: set[str]) -> None:
        self.protected_baskets |= set(basket_ids)
        self.lock_handed_over(self.protected_baskets)

    def confirm(self, repo, a: Assignment) -> dict[str, Any]:
        clash = [b for b in a.baskets if b in self.protected_baskets]
        if clash:
            return {"confirmed": False,
                    "reason": f"料筐 {clash} 已有确认交接/受保护事实，"
                              "重算排程不得覆盖；如需更正请走退回/替换流程"}
        verdict = verify_plan(repo, [a], existing=self.assignments)
        if not verdict["feasible"]:
            return {"confirmed": False, "verdict": verdict}
        a.state = "已确认"
        self.assignments.append(a)
        return {"confirmed": True, "assignment": a.to_dict(), "verdict": verdict}

    def lock_handed_over(self, basket_ids: set[str]) -> None:
        for a in self.assignments:
            if set(a.baskets) & basket_ids:
                a.locked = True

    def occupancy(self) -> list[Occupancy]:
        out = []
        for a in self.assignments:
            if a.state not in {"已确认", "处理中"}:
                continue
            out.append(Occupancy(
                a.day, a.slot, "schedule", a.ref or "/".join(a.baskets),
                fresh_demand_from_assignment(a), baskets=list(a.baskets),
                machines=[a.machine],
                firm=a.locked or a.state == "处理中", state=a.state))
        return out


def verify_plan(repo, assignments: list[Assignment],
                existing: list[Assignment] | None = None,
                quota_fallback: dict[tuple[str, int], dict[str, Any]] | None = None
                ) -> dict[str, Any]:
    """核验若干洗护批；existing 为账簿中已存在的批（机器冲突/占用去重）。

    ``quota_fallback`` 用于“若物业按常规模板核额”的假设性延迟方案：
    键为 (day, slot)，值为 quota 行；使用兜底时报告明确标注
    ``quota_basis='assumed_template'``，结论不能当作已确认可排。
    """
    standard = calibrate_demand(repo)
    existing = existing or []
    quota_fallback = quota_fallback or {}
    problems: list[dict[str, Any]] = []
    slots = sorted({(a.day, a.slot) for a in assignments})
    slot_reports = []

    for day, s in slots:
        q = repo.quota_slot(day, s)
        basis = "property_quota"
        if q is None and (day, s) in quota_fallback:
            q = quota_fallback[(day, s)]
            basis = "assumed_template"
        new_here = [a for a in assignments if a.day == day and a.slot == s]
        old_here = [a for a in existing if a.day == day and a.slot == s
                    and a not in assignments]
        rep: dict[str, Any] = {"day": day, "slot": s,
                               "assignments": [a.to_dict() for a in new_here]}

        # 1) 批内合洗约定
        for a in new_here:
            baskets = [repo.basket(b) for b in a.baskets]
            cow = co_wash_allowed(baskets, a.program)
            rep.setdefault("batch_checks", []).append(
                {"ref": a.ref, "baskets": a.baskets, "co_wash": cow})
            if not cow["allowed"]:
                problems.append({"level": "agreement", **cow,
                                 "day": day, "slot": s, "baskets": a.baskets})

        # 2) 机器
        q_machines = set(q["machines"]) if q else set()
        machine_batches: dict[str, list[Assignment]] = {}
        for a in new_here + old_here:
            machine_batches.setdefault(a.machine, []).append(a)
        for m, bs in machine_batches.items():
            if len(bs) > 1:
                problems.append({"level": "machine", "day": day, "slot": s, "machine": m,
                                "batches": [b.ref or "/".join(b.baskets) for b in bs],
                                "reason": "同一机器同一时段被两个洗护批占用"})
            if q and m not in q_machines:
                problems.append({"level": "machine", "day": day, "slot": s, "machine": m,
                                "available": sorted(q_machines),
                                "reason": "该时段物业配额机器清单不含此机器"})

        # 3) 水额
        if q is None:
            rep["quota"] = None
            problems.append({"level": "quota", "day": day, "slot": s,
                            "reason": "该时段没有物业配额行：未核额，不能确认排程"})
        else:
            existing_demand = Range.exact(0)
            for a in old_here:
                existing_demand = existing_demand + fresh_demand(
                    repo, a.program, a.reuse_l, standard)
            plan_demand = Range.exact(0)
            for a in new_here:
                plan_demand = plan_demand + fresh_demand(
                    repo, a.program, a.reuse_l, standard)
            total = existing_demand + plan_demand
            nominal_ok = total.lo <= q["new_water_l"] + 1e-9
            robust_ok = total.hi <= q["new_water_l"] + 1e-9
            rep["quota"] = {"quota_l": q["new_water_l"], "quota_basis": basis,
                            "existing_demand": existing_demand.to_dict(),
                            "new_demand": plan_demand.to_dict(),
                            "total_demand": total.to_dict(),
                            "nominal_feasible": nominal_ok,
                            "robust_feasible": robust_ok}
            if basis == "assumed_template":
                rep["quota"]["warning"] = "常规模板假设额度，尚非物业已核额，不能据此确认"
            if not nominal_ok:
                problems.append({"level": "water", "day": day, "slot": s,
                                "need": total.to_dict(), "quota_l": q["new_water_l"],
                                "reason": "即使按最低需水估计也超过时段新水额"})
            elif not robust_ok:
                problems.append({"level": "water_uncertain", "day": day, "slot": s,
                                "need": total.to_dict(), "quota_l": q["new_water_l"],
                                "reason": "需水区间上沿超过水额：名义可排但不稳健，"
                                          "须先核实读表/回用水许可"})

        # 4) 件数容量与领件时刻
        items = sum(repo.basket(b)["quantity"] for a in new_here for b in a.baskets)
        rep["item_count"] = items
        if q and items > q["basket_capacity"]:
            problems.append({"level": "basket_time", "day": day, "slot": s,
                            "items": items, "capacity": q["basket_capacity"],
                            "reason": "时段洗护件数超过 basket_capacity"})
        for a in new_here:
            for bid in a.baskets:
                b = repo.basket(bid)
                if a.slot >= b["collect_slot"] and a.day <= b["day"]:
                    problems.append({"level": "pickup_timing", "basket": bid,
                                    "wash_slot": a.slot,
                                    "collect_slot": b["collect_slot"],
                                    "reason": "洗护时段不早于领件时段，无法在领件前放行"})
        slot_reports.append(rep)

    hard_levels = {"agreement", "machine", "quota", "water",
                   "basket_time", "pickup_timing"}
    hard_fail = any(p["level"] in hard_levels for p in problems)
    return {"feasible": not hard_fail,
            "robust": not any(p["level"] in hard_levels | {"water_uncertain"}
                              for p in problems),
            "problems": problems, "slots": slot_reports}


def certified_reuse_for(repo, basket_ids: list[str],
                        checks: dict | None = None) -> dict[str, Any]:
    """汇总历史批回收水中可用于这些料筐等级的已检测水量。

    没有有效通过检测 / allowed_use 不覆盖等级的来源，即使记录了回收量也给 0。
    """
    effective = checks or repo.effective_checks()
    wanted_soils = {repo.basket(b)["soil"] for b in basket_ids}
    result = []
    total = 0.0
    for c in repo.operations:
        chk = next((v for v in effective.values() if v["cycle"] == c["cycle"]), None)
        amount = c.get("recovered_l", 0)
        permitted = set(chk.get("allowed_use", [])) if chk else set()
        ok = bool(chk and chk.get("verdict") == "通过" and wanted_soils <= permitted)
        total += amount if ok else 0
        result.append({
            "source_cycle": c["cycle"], "check": chk["check"] if chk else None,
            "recovered_l": amount, "permitted": ok,
            "allowed_use": sorted(permitted), "usable_l": amount if ok else 0,
            "reason": ("许可覆盖全部料筐等级" if ok else
                       "缺少覆盖该料筐等级的有效检测许可，有水也不能用于省新水")})
    return {"usable_l": total, "sources": result}


def compare_pickup_scenarios(repo, day: str = "2026-04-12",
                             candidate_baskets: list[str] | None = None,
                             certified_reuse_l: float = 0.0,
                             reuse_note: str = "",
                             physical_certified_reuse_l: float | None = None,
                             standard_slot_template: dict[str, Any] | None = None
                             ) -> dict[str, Any]:
    """比较合洗 / 延迟非急件 / 改用可确认回用水三类动作的影响。"""
    if candidate_baskets is None:
        candidate_baskets = [h["basket"] for h in repo.handovers
                             if h["day"] == day and h["state"] != "已交接"]
    baskets = [repo.basket(b) for b in candidate_baskets]
    standard = calibrate_demand(repo)
    scenarios: dict[str, Any] = {}

    # A：按交接记录的预排时段原样核验（每篮自成一批）
    raw = [Assignment([h["basket"]], day, h["slot"],
                      "单洗" if repo.basket(h["basket"])["soil"] == "单洗" else "节水",
                      ref=h["event"], state="建议")
           for h in repo.handovers
           if h["basket"] in candidate_baskets and h["day"] == day]
    scenarios["A_按预排时段"] = {"assignments": [a.to_dict() for a in raw],
                                "verification": verify_plan(repo, raw)}

    # B：试图在唯一已核额 slot0 把所有篮合洗（单洗篮应被约定拦截）
    b_assign = Assignment(list(candidate_baskets), day, 0, "节水",
                          ref="merge_attempt", state="建议")
    scenarios["B_合洗尝试_slot0"] = {
        "assignments": [b_assign.to_dict()],
        "verification": verify_plan(repo, [b_assign])}

    # C：普通篮 slot0（投用“账面有许可”回用水），单洗篮延迟到常规 slot2
    normal = [b["basket"] for b in baskets if b["soil"] != "单洗"]
    exclusive = [b["basket"] for b in baskets if b["soil"] == "单洗"]
    c_batch = Assignment(normal, day, 0, "节水", reuse_l=certified_reuse_l,
                         reuse_source="certified" if certified_reuse_l else None,
                         ref="reuse_cowash", state="建议") if normal else None
    next_day = _next_quota_day(repo, day)
    deferred_day, deferred_slot = ((next_day, 1) if next_day else (day, 2))
    deferred = Assignment(exclusive, deferred_day, deferred_slot, "单洗",
                          ref="deferred_single", state="建议") if exclusive else None
    # 常规经营日 slot2 模板：22L / 机器 A / 18 件（资料中所有周六均如此）
    template = standard_slot_template or {"new_water_l": 22, "machines": ["A"],
                                         "basket_capacity": 18}
    fallback = {(deferred_day, deferred_slot): template}
    c_verify = verify_plan(repo, [c_batch]) if c_batch else None
    d_verify = (verify_plan(repo, [deferred], quota_fallback=fallback)
                if deferred else None)
    scenarios["C_许可回用水加延迟单洗"] = {
        "same_shift": c_batch.to_dict() if c_batch else None,
        "same_shift_verification": c_verify,
        "deferred": deferred.to_dict() if deferred else None,
        "deferred_verification": d_verify,
        "deferred_quota_basis": ("property_quota"
                                 if repo.quota_slot(deferred_day, deferred_slot)
                                 else "assumed_standard_template"),
        "next_quota_day": next_day,
        "certified_reuse_l": certified_reuse_l, "reuse_note": reuse_note,
        "deferred_baskets": exclusive}

    # C2：罐段实物口径——若指向罐的当前段没有对应用途许可（如 B-2 待测），
    # 可抵扣回用水为 0：即使罐里有 18L 实物也不能给料筐省新水。
    phys = (physical_certified_reuse_l if physical_certified_reuse_l is not None
            else 0.0)
    c2_batch = Assignment(normal, day, 0, "节水", reuse_l=phys,
                          reuse_source="tank_certified", ref="tank_reuse",
                          state="建议") if normal else None
    scenarios["C2_罐段实物许可口径"] = {
        "same_shift": c2_batch.to_dict() if c2_batch else None,
        "same_shift_verification": verify_plan(repo, [c2_batch]) if c2_batch else None,
        "tank_certified_reuse_l": phys,
        "note": "账面回收检测许可不等于当前罐段可用：混有待测水后整段许可失效"}

    # D：全新鲜水、只排容量内容得下的篮，其余暴露为缺口
    cap = (repo.quota_slot(day, 0) or {}).get("basket_capacity", 12)
    chosen, used = [], 0
    for b in baskets:
        if used + b["quantity"] <= cap:
            chosen.append(b["basket"])
            used += b["quantity"]
    d_batch = Assignment(chosen, day, 0,
                         "单洗" if len(chosen) == 1
                         and repo.basket(chosen[0])["soil"] == "单洗" else "节水",
                         ref="fresh_capacity", state="建议") if chosen else None
    scenarios["D_全新鲜水_slot0容量内"] = {
        "assignment": d_batch.to_dict() if d_batch else None,
        "verification": verify_plan(repo, [d_batch]) if d_batch else None,
        "unscheduled": [b["basket"] for b in baskets if b["basket"] not in chosen]}

    return {"day": day, "candidate_baskets": candidate_baskets,
            "demand_standard": {"cowash_fresh": standard.cowash_fresh.to_dict(),
                                "single_fresh": standard.single_fresh.to_dict()},
            "scenarios": scenarios}


def _next_quota_day(repo, after_day: str) -> str | None:
    days = sorted({r["day"] for r in repo.quota if r["day"] > after_day})
    return days[0] if days else None
