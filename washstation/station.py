"""对外门面 :class:`Station`：一个入口串起全部核算与排程能力。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (billing, commitments, meters, planning, quality, quota,
               reconcile, shift, tanks, transfers, water)
from .core import (CURRENT_DAY, CURRENT_SLOT, DEFAULT_DATA, PREVIOUS_DAY,
                   Repository, load_repository)


class Station:
    def __init__(self, data_dir: str | Path | None = None,
                 home_station: str = "松谷") -> None:
        self.repo: Repository = load_repository(data_dir or DEFAULT_DATA)
        self.home_station = home_station
        self.book = planning.ScheduleBook()
        handed = {h["basket"] for h in self.repo.handovers if h["state"] == "已交接"}
        handed |= {p["basket"] for p in self.repo.plans
                   if p.get("event") == "pickup" and p.get("state") == "已交接"}
        self.locked_baskets = handed
        # 已交接实物自动加保护，任何排程重算都不能覆盖
        self.book.protect(handed)

    # ---- 资料 --------------------------------------------------------
    @property
    def r(self) -> Repository:
        return self.repo

    # ---- 计量与水 ----------------------------------------------------
    def fresh_position(self, day: str):
        return meters.fresh_day_range(self.repo, day)

    def anomalies(self):
        return meters.register_anomalies(self.repo)

    def day_account(self, day: str):
        return water.attribute_day(self.repo, day)

    def desk_view(self, day: str):
        return water.desk_water_view(self.repo, day)

    def estimate_validation(self):
        return water.estimate_and_validate(self.repo)

    # ---- 配额与新班可用水 -------------------------------------------
    def occupancies(self):
        """账簿确认占用 + booking_and_plan 中既有 plan 行占用。"""
        occ = self.book.occupancy()
        std = planning.calibrate_demand(self.repo)
        for p in self.repo.plans:
            if p["event"] != "plan":
                continue
            b = self.repo.basket(p["basket"])
            program = "单洗" if b["soil"] == "单洗" else "节水"
            rng = std.for_program(program)
            occ.append(quota.Occupancy(
                p["day"], p["slot"], "booking_plan",
                f'{p["booking"]}:{p["state"]}', rng, items=b["quantity"],
                baskets=[p["basket"]], machines=["A"],
                firm=p["state"] == "处理中", state=p["state"]))
        return occ

    def new_shift_availability(self, day: str = CURRENT_DAY, slot: int = CURRENT_SLOT):
        return quota.availability(self.repo, day, slot, self.occupancies())

    # ---- 检测/罐/调拨 ------------------------------------------------
    def overturn(self, check_id: str = "T13"):
        return quality.overturn_cascade(self.repo, check_id)

    def tank_positions(self):
        return tanks.tank_positions(self.repo)

    def tank_impact(self, tank: str = "B-2"):
        return tanks.grade_change_impact(self.repo, tank)

    def transfer(self, tx: str = "TX1"):
        return transfers.transfer_view(self.repo, tx).to_dict()

    def all_transfers(self):
        return [v.to_dict() for v in transfers.all_transfers(self.repo)]

    # ---- 排程 --------------------------------------------------------
    def scenarios(self, day: str = CURRENT_DAY, candidate_baskets=None, **kw):
        if candidate_baskets is None:
            candidate_baskets = [h["basket"] for h in self.repo.handovers
                                 if h["day"] == day and h["state"] != "已交接"]
        normal = [b for b in candidate_baskets
                  if self.repo.basket(b)["soil"] != "单洗"]
        target = normal or candidate_baskets
        # 账面检测许可口径：只取最近经营日仍有效通过的批回收量作为
        # “可跨班的账面可确认回用水”，不把往季已用掉的回收量累加进来。
        cert = planning.certified_reuse_for(self.repo, target)
        latest_day = self.repo.operating_days()[-1]
        book_reuse_l = sum(s["usable_l"] for s in cert["sources"]
                           if s["permitted"]
                           and self.repo.cycle(s["source_cycle"])["day"] == latest_day)
        # 罐段实物口径：用料筐交接指向罐的当前 certified_usable
        pos = {p["tank"]: p for p in self.tank_positions()}
        physical = None
        for h in self.repo.handovers:
            if h["basket"] in target and h["day"] == day:
                p = pos.get(h["tank"])
                if p and p.get("certified_usable_l") is not None:
                    physical = min(physical if physical is not None else 1e9,
                                   p["certified_usable_l"])
        kw.setdefault("certified_reuse_l", book_reuse_l)
        kw.setdefault("reuse_note",
                      f"最近经营日 {latest_day} 有效检测许可的批回收水 "
                      f"{book_reuse_l:g}L；账面许可还须落到当前实物合格罐段")
        kw.setdefault("physical_certified_reuse_l", physical or 0.0)
        return planning.compare_pickup_scenarios(
            self.repo, day, candidate_baskets=candidate_baskets, **kw)

    def verify(self, assignments):
        return planning.verify_plan(self.repo, assignments,
                                    existing=self.book.assignments)

    # ---- 计费/核对 ----------------------------------------------------
    def billing_actions(self):
        return billing.counter_billing_actions(self.repo)

    def month_end(self, month: str = "2026-04"):
        return billing.month_end_property_bill(self.repo, month)

    def reconcile_cycle(self, cycle: str):
        return reconcile.reconcile_batch(self.repo, cycle)

    def reconcile_all(self):
        return reconcile.reconcile_all(self.repo)

    # ---- 承诺/交接/异常 ----------------------------------------------
    def commitments(self):
        return commitments.confirmed_pickup_commitments(self.repo)

    def dispatch(self):
        return commitments.station_dispatch(self.repo)

    def verify_leases(self, leases):
        """leases 为 commitments.LeaseCommitment 列表或 (station, min_items) 元组。"""
        lc = []
        for x in leases:
            if isinstance(x, commitments.LeaseCommitment):
                lc.append(x)
            else:
                station, min_items = x[0], x[1]
                period = x[2] if len(x) > 2 else "next_shift"
                lc.append(commitments.LeaseCommitment(station, min_items, period))
        return commitments.verify_lease_commitments(self.repo, lc)

    def delivery_vs_quota(self, day: str):
        return reconcile.counter_delivery_vs_quota(self.repo, day)

    def shift_log(self):
        return shift.receive_shift_log(self.repo)

    def explain_slot(self, day: str, slot: int):
        return shift.explain_slot_anomaly(self.repo, day, slot)

    def replacement_plan(self, baskets, now_day=CURRENT_DAY, now_slot=CURRENT_SLOT, **kw):
        return commitments.rewash_replacement_plan(
            self.repo, baskets, now_day, now_slot, **kw)

    # ---- 汇总报告 ----------------------------------------------------
    def report(self) -> dict[str, Any]:
        from .report import build_report
        return build_report(self)
