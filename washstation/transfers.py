"""跨站调拨（transfers）的事件身份、确认时点与两端计量差。

业务规则：

- ``litres`` 是出/入站各自申报计量，按同一 transfer 的事件身份区分两端；
- 预留（kind=预留）只是意向，不构成实物到站；
- 出站确认后，出借站不能再安排这部分水；
- 到站确认前，借入站只能按“不可用”看待；
- 到站更正只替换同端相应读数，历史申报保留；
- 两端分量不一致时，先挂计量差额，再按最新到站确认量调整后续可调度量。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .core import Range, Repository


@dataclass
class TransferView:
    transfer: str
    origin: str
    target: str
    tank: str
    reservation: str
    proposed_l: float | None
    outbound_l: float | None
    arrival_l: float | None          # 最新（含更正）到站确认量
    arrival_history: list[dict[str, Any]]
    metering_gap_l: float | None      # 出站 - 最新到站
    gap_history: list[dict[str, Any]]
    origin_locked_l: float           # 出站后出借站锁定
    target_available_l: float        # 到站确认后借入站可调度
    target_usable_before_arrival: float
    status: str
    events: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _sort_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(events, key=lambda e: (e["day"], e["slot"], e["event"]))


def transfer_view(repo: Repository, transfer_id: str) -> TransferView:
    events = _sort_events(repo.transfer_events(transfer_id))
    if not events:
        raise KeyError(f"未知调拨单 {transfer_id}")
    head = events[0]
    proposed = next((e["litres"] for e in events if e["kind"] == "预留"), None)
    outbound = next((e["litres"] for e in events if e["kind"] == "出站"), None)

    arrivals = [e for e in events if e["kind"] in {"到站", "到站更正"}]
    latest_arrival = arrivals[-1]["litres"] if arrivals else None
    arrival_history = [{"event": e["event"], "kind": e["kind"], "litres": e["litres"],
                        "when": f'{e["day"]}#{e["slot"]}'} for e in arrivals]

    gap = None
    gap_history: list[dict[str, Any]] = []
    if outbound is not None:
        for a in arrivals:
            gap_history.append({"against_event": a["event"],
                                "outbound_l": outbound, "arrival_l": a["litres"],
                                "gap_l": outbound - a["litres"]})
        if latest_arrival is not None:
            gap = outbound - latest_arrival

    origin_locked = outbound if outbound is not None else 0.0
    # 到站确认（含更正）前借入站不可用；确认后按最新确认量可调度。
    target_available = latest_arrival if latest_arrival is not None else 0.0

    if outbound is None:
        status = "仅预留/争用，未见出站确认"
    elif latest_arrival is None:
        status = "已出站，未到站确认：借出站已锁定，借入站按不可用"
    elif gap not in (0, None):
        status = f"已到站（最新 {latest_arrival:g}L），存在 {gap:g}L 计量差额待查"
    else:
        status = "出入站计量一致，调拨完成"

    return TransferView(
        transfer=transfer_id, origin=head["origin"], target=head["target"],
        tank=head["tank"], reservation=head["reservation"],
        proposed_l=proposed, outbound_l=outbound, arrival_l=latest_arrival,
        arrival_history=arrival_history, metering_gap_l=gap, gap_history=gap_history,
        origin_locked_l=origin_locked, target_available_l=target_available,
        target_usable_before_arrival=0.0, status=status, events=events,
    )


def all_transfers(repo: Repository) -> list[TransferView]:
    return [transfer_view(repo, tid)
            for tid in sorted({e["transfer"] for e in repo.transfers})]


def propose_transfer(repo: Repository, tank_id: str, litres: float,
                     origin_free_l: float, target_intake_capacity_l: float | None = None,
                     certified_usable_l: float | None = None) -> dict[str, Any]:
    """站长发起调拨提议时的双向可接量核验（纯计算，不落账）。

    - 出借站能出：``origin_free_l``（已扣除出站锁定与既有预留），
      且不能超过该罐有检测许可的可用量 ``certified_usable_l``（若提供）；
    - 借入站能接：罐/工位余量 ``target_intake_capacity_l``；资料未给罐容时
      为 None（不设上限，但明确提示缺少容量证据）。
    """
    can_give_lo = min(origin_free_l, certified_usable_l) if certified_usable_l is not None \
        else origin_free_l
    can_take = target_intake_capacity_l
    feasible = litres <= can_give_lo + 1e-9 and (can_take is None or litres <= can_take + 1e-9)
    return {
        "tank": tank_id, "requested_l": litres,
        "origin_can_give_l": Range(0, can_give_lo).to_dict(),
        "target_can_take_l": (None if can_take is None
                              else Range.exact(can_take).to_dict()),
        "feasible": feasible,
        "reasons": [
            *([] if litres <= origin_free_l + 1e-9 else ["超过出借站未占用余量"]),
            *([] if certified_usable_l is None or litres <= certified_usable_l + 1e-9
              else ["超过该罐有检测许可的可用量（待测/停用段不能外借作合规格水）"]),
            *([] if can_take is None or litres <= can_take + 1e-9 else ["超过借入站接收余量"]),
            *([] if can_take is not None else ["借入站罐容未提供，接收上限缺证据"]),
        ],
    }
