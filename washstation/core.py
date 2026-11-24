"""通用类型与数据仓储。

所有“归属不确定”的水量都用 :class:`Range` 表示，绝不塌缩成一个
看似精确的平均值。上界为 ``None`` 表示在现有证据下没有有限上界。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data"

# 人民币与升/立方米换算
LITRES_PER_M3 = 1000
WATER_PRICE_PER_M3 = 6.1

# 新经营日切分后的“当前班”时点（数据中最后一个物业配额日的首个时段）
CURRENT_DAY = "2026-04-12"
CURRENT_SLOT = 0
PREVIOUS_DAY = "2026-04-11"


class StationError(ValueError):
    """业务规则或资料契约错误。"""


@dataclass(frozen=True)
class Range:
    """闭区间水量。``hi is None`` 表示上不封顶（证据不足）。"""

    lo: float
    hi: float | None = None

    def __post_init__(self) -> None:
        if self.lo is None or self.lo < 0:
            raise ValueError(f"Range.lo 必须是非负数，收到 {self.lo!r}")
        if self.hi is not None and self.hi < self.lo:
            raise ValueError(f"Range 上界小于下界: {self.lo} > {self.hi}")

    @classmethod
    def exact(cls, value: float) -> "Range":
        return cls(float(value), float(value))

    @property
    def exact_value(self) -> float | None:
        return self.lo if self.hi is not None and abs(self.hi - self.lo) < 1e-9 else None

    @property
    def is_exact(self) -> bool:
        return self.exact_value is not None

    @property
    def width(self) -> float | None:
        return None if self.hi is None else self.hi - self.lo

    def contains(self, value: float) -> bool:
        return value >= self.lo - 1e-9 and (self.hi is None or value <= self.hi + 1e-9)

    def __add__(self, other: "Range | float | int") -> "Range":
        other = other if isinstance(other, Range) else Range.exact(float(other))
        hi = None if self.hi is None or other.hi is None else self.hi + other.hi
        return Range(self.lo + other.lo, hi)

    __radd__ = __add__

    def __sub__(self, other: "Range | float | int") -> "Range":
        other = other if isinstance(other, Range) else Range.exact(float(other))
        # self - other：最小取下界差上界，最大取上界差下界
        lo = max(0.0, self.lo - (other.hi if other.hi is not None else 0.0))
        if self.hi is None or other.hi is None:
            hi = None
        else:
            hi = max(0.0, self.hi - other.lo)
        return Range(lo, hi)

    def scale(self, factor: float) -> "Range":
        if factor < 0:
            raise ValueError("scale factor 不能为负")
        return Range(self.lo * factor, None if self.hi is None else self.hi * factor)

    def to_dict(self) -> dict[str, Any]:
        return {"lo": _num(self.lo), "hi": None if self.hi is None else _num(self.hi),
                "exact": self.is_exact, "value": (None if not self.is_exact else _num(self.lo))}

    def format(self, unit: str = "L") -> str:
        if self.is_exact:
            return f"{_num(self.lo)}{unit}"
        if self.hi is None:
            return f"≥{_num(self.lo)}{unit}"
        return f"{_num(self.lo)}–{_num(self.hi)}{unit}"


def _num(value: float) -> int | float:
    """整数值序列化为 int，避免报告里到处是 ``60.0``。"""
    return int(value) if abs(value - round(value)) < 1e-9 else round(value, 3)


def money(litres: float | Range, price_per_m3: float = WATER_PRICE_PER_M3) -> dict[str, Any]:
    """按 6.1 元/m³ 计算水费；区间量返回区间金额。"""
    if isinstance(litres, Range):
        lo = litres.lo / LITRES_PER_M3 * price_per_m3
        hi = None if litres.hi is None else litres.hi / LITRES_PER_M3 * price_per_m3
        return {"currency": "CNY", "lo": round(lo, 4),
                "hi": None if hi is None else round(hi, 4),
                "range": Range(lo, hi).to_dict()}
    amount = litres / LITRES_PER_M3 * price_per_m3
    return {"currency": "CNY", "amount": round(amount, 4)}


@dataclass
class Repository:
    """全部资料表与常用索引。

    资料以 JSON 对象数组保存；本类只做读取和索引，不修改原始记录。
    需要“当前有效版本”的单据请用 :meth:`latest_version`，不要自行假设。
    """

    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    data_dir: Path = DEFAULT_DATA

    # -- 基础读取 -------------------------------------------------------
    @classmethod
    def from_dir(cls, data_dir: str | Path | None = None) -> "Repository":
        return load_repository(data_dir)

    def table(self, name: str) -> list[dict[str, Any]]:
        if name not in self.tables:
            raise KeyError(f"缺少资料表 {name!r}，现有：{sorted(self.tables)}")
        return self.tables[name]

    # -- 业务索引 -------------------------------------------------------
    @property
    def baskets(self) -> list[dict[str, Any]]:
        return self.table("baskets")

    @property
    def operations(self) -> list[dict[str, Any]]:
        return self.table("operations")

    @property
    def meters(self) -> list[dict[str, Any]]:
        return self.table("meters")

    @property
    def quota(self) -> list[dict[str, Any]]:
        return self.table("quota")

    @property
    def checks(self) -> list[dict[str, Any]]:
        return self.table("water_checks")

    @property
    def plans(self) -> list[dict[str, Any]]:
        return self.table("booking_and_plan")

    @property
    def handovers(self) -> list[dict[str, Any]]:
        return self.table("handover")

    @property
    def tanks(self) -> list[dict[str, Any]]:
        return self.table("tank_ledger")

    @property
    def transfers(self) -> list[dict[str, Any]]:
        return self.table("transfers")

    @property
    def statements(self) -> list[dict[str, Any]]:
        return self.table("utility_statements")

    def basket(self, basket_id: str) -> dict[str, Any]:
        for row in self.baskets:
            if row["basket"] == basket_id:
                return row
        raise KeyError(f"未知料筐 {basket_id!r}")

    def cycle(self, cycle_id: str) -> dict[str, Any]:
        for row in self.operations:
            if row["cycle"] == cycle_id:
                return row
        raise KeyError(f"未知洗护批 {cycle_id!r}")

    def cycles_for_day(self, day: str) -> list[dict[str, Any]]:
        return [c for c in self.operations if c["day"] == day]

    def quota_slot(self, day: str, slot: int) -> dict[str, Any] | None:
        for row in self.quota:
            if row["day"] == day and row["slot"] == slot:
                return row
        return None

    def latest_version(self, table: str, doc_id_field: str, doc_id: str) -> dict[str, Any]:
        rows = [r for r in self.table(table) if r[doc_id_field] == doc_id]
        if not rows:
            raise KeyError(f"{table} 中没有 {doc_id}")
        return max(rows, key=lambda r: r.get("version", 1))

    def checks_of(self, check_id: str) -> list[dict[str, Any]]:
        return sorted((r for r in self.checks if r["check"] == check_id),
                      key=lambda r: r["version"])

    def effective_checks(self, at_day: str | None = None,
                         at_slot: int | None = None) -> dict[str, dict[str, Any]]:
        """每个检测单号在指定时点可见的最新版本。

        检测版本按 ``version`` 订正；若给定时点，则只纳入其
        ``available_slot`` 不晚于该时点的版本（同日同时段视为可见）。
        """
        result: dict[str, dict[str, Any]] = {}
        for row in self.checks:
            cid = row["check"]
            if at_day is not None:
                # 资料里 available_slot 是当日时段；跨日订正一律按已可见处理，
                # 时点过滤只用于同一经营日的级联判断。
                cyc = self.cycle(row["cycle"])
                if cyc["day"] == at_day and at_slot is not None \
                        and row["available_slot"] > at_slot:
                    continue
            cur = result.get(cid)
            if cur is None or row["version"] > cur["version"]:
                result[cid] = row
        return result

    def transfer_events(self, transfer_id: str) -> list[dict[str, Any]]:
        return [r for r in self.transfers if r["transfer"] == transfer_id]

    def operating_days(self) -> list[str]:
        """有洗护批或料筐记录的经营日（升序）。"""
        days = {r["day"] for r in self.baskets} | {c["day"] for c in self.operations}
        return sorted(days)


def load_repository(data_dir: str | Path | None = None) -> Repository:
    directory = Path(data_dir) if data_dir else DEFAULT_DATA
    names = [
        "baskets", "operations", "meters", "quota", "water_checks",
        "booking_and_plan", "handover", "tank_ledger", "transfers",
        "utility_statements",
    ]
    tables: dict[str, list[dict[str, Any]]] = {}
    for name in names:
        path = directory / f"{name}.json"
        with path.open(encoding="utf-8") as stream:
            records = json.load(stream)
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise StationError(f"{path.name} 应为 JSON 对象数组")
        tables[name] = records
    return Repository(tables=tables, data_dir=directory)


def group_by(rows: Iterable[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row[key], []).append(row)
    return out
