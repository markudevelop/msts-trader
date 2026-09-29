"""Symphony schema: a Composer-style strategy tree that evaluates to target weights.

Node `step` names mirror Composer.trade's (`wt-cash-equal`, `if`, `filter`, ...)
so imported symphonies map nearly 1:1 and users recognise the blocks.

Weights inside the tree are fractions (0..1). Indicator values that are
returns / drawdowns / volatilities are expressed in PERCENT, as Composer does,
so a condition like "cumulative return of SPY over 10d > 5" means +5%.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Indicator functions (Composer names). See indicators.py for the math.
INDICATORS = (
    "current-price",
    "cumulative-return",
    "moving-average-price",
    "exponential-moving-average-price",
    "moving-average-return",
    "relative-strength-index",
    "standard-deviation-price",
    "standard-deviation-return",
    "max-drawdown",
)
IndicatorFn = Literal[
    "current-price",
    "cumulative-return",
    "moving-average-price",
    "exponential-moving-average-price",
    "moving-average-return",
    "relative-strength-index",
    "standard-deviation-price",
    "standard-deviation-return",
    "max-drawdown",
]
Comparator = Literal["gt", "gte", "lt", "lte"]
Rebalance = Literal["daily", "weekly", "monthly", "quarterly", "yearly"]

# Same rule as `rebalance --sleeve`: the strategy id doubles as its sleeve name.
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
_TICKER_RE = re.compile(r"[A-Za-z0-9.\-^=/]{1,20}")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Metric(_Base):
    """An indicator applied to one ticker's daily close series."""

    fn: IndicatorFn
    ticker: str
    window: int = Field(default=1, ge=1, le=2000)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str) -> str:
        v = v.strip().upper()
        if not _TICKER_RE.fullmatch(v):
            raise ValueError(f"invalid ticker {v!r}")
        return v


class Condition(_Base):
    lhs: Metric
    comparator: Comparator
    # Exactly one of rhs (another metric) / rhs_value (a fixed number).
    rhs: Metric | None = None
    rhs_value: float | None = None

    @model_validator(mode="after")
    def _one_rhs(self) -> Condition:
        if (self.rhs is None) == (self.rhs_value is None):
            raise ValueError("condition needs exactly one of rhs (metric) or rhs_value (number)")
        return self


class _NodeBase(_Base):
    # Weight of this node inside a `wt-cash-specified` parent (fraction 0..1);
    # ignored by every other parent.
    weight: float | None = Field(default=None, ge=0, le=3)


class Asset(_NodeBase):
    step: Literal["asset"] = "asset"
    ticker: str
    name: str | None = None

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str) -> str:
        v = v.strip().upper()
        if not _TICKER_RE.fullmatch(v):
            raise ValueError(f"invalid ticker {v!r}")
        return v


class Group(_NodeBase):
    step: Literal["group"] = "group"
    name: str = "Group"
    children: list[Node] = Field(default_factory=list)


class WeightEqual(_NodeBase):
    step: Literal["wt-cash-equal"] = "wt-cash-equal"
    children: list[Node] = Field(default_factory=list)


class WeightSpecified(_NodeBase):
    step: Literal["wt-cash-specified"] = "wt-cash-specified"
    children: list[Node] = Field(default_factory=list)


class WeightInverseVol(_NodeBase):
    step: Literal["wt-inverse-vol"] = "wt-inverse-vol"
    window: int = Field(default=20, ge=2, le=2000)
    children: list[Node] = Field(default_factory=list)


class If(_NodeBase):
    step: Literal["if"] = "if"
    condition: Condition
    then: list[Node] = Field(default_factory=list)
    # `else` is a Python keyword; serialised as "else".
    otherwise: list[Node] = Field(default_factory=list, alias="else")


class Filter(_NodeBase):
    step: Literal["filter"] = "filter"
    sort_fn: IndicatorFn
    window: int = Field(default=10, ge=1, le=2000)
    select: Literal["top", "bottom"] = "top"
    n: int = Field(default=1, ge=1, le=100)
    children: list[Node] = Field(default_factory=list)


Node = Annotated[
    Union[Asset, Group, WeightEqual, WeightSpecified, WeightInverseVol, If, Filter],
    Field(discriminator="step"),
]

for _cls in (Group, WeightEqual, WeightSpecified, WeightInverseVol, If, Filter):
    _cls.model_rebuild()


class Deploy(_Base):
    """Where/how the strategy trades. Money only moves with live_enabled."""

    broker: str = "paper"
    account: str | None = None
    live_enabled: bool = False
    schedule_enabled: bool = False
    schedule_time: str = "15:50"  # US/Eastern, HH:MM
    # Drift threshold passed to `rebalance --threshold` (fraction of position).
    threshold: float = Field(default=0.02, ge=0, le=1)

    @field_validator("schedule_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("schedule_time must be HH:MM (24h, US/Eastern)")
        return v


class Symphony(_Base):
    id: str
    name: str
    description: str = ""
    rebalance: Rebalance = "daily"
    children: list[Node] = Field(default_factory=list)
    deploy: Deploy = Field(default_factory=Deploy)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.fullmatch(v):
            raise ValueError("id must be 1-40 chars of letters, digits, - and _")
        return v


def walk(nodes: list) -> list:
    """All nodes in the subtree, depth-first."""
    out = []
    for n in nodes:
        out.append(n)
        if isinstance(n, If):
            out.extend(walk(n.then))
            out.extend(walk(n.otherwise))
        elif hasattr(n, "children"):
            out.extend(walk(n.children))
    return out


def tickers(sym: Symphony) -> list[str]:
    """Every ticker the strategy can hold or reads an indicator from."""
    seen: set[str] = set()
    for n in walk(sym.children):
        if isinstance(n, Asset):
            seen.add(n.ticker)
        elif isinstance(n, If):
            seen.add(n.condition.lhs.ticker)
            if n.condition.rhs is not None:
                seen.add(n.condition.rhs.ticker)
    return sorted(seen)


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").lower()
    return (s or "strategy")[:40]


_CADENCE_ORDER = ("daily", "weekly", "monthly", "quarterly", "yearly")


def combine(parts: list[tuple[Symphony, float]], *, id: str, name: str, rebalance: Rebalance | None = None) -> Symphony:
    """Blend strategies into ONE strategy: each part becomes a named group
    under a fixed-weight parent (`wt-cash-specified`), so the blend holds
    weight_i x (what part i would hold) and is rebalanced as one book.

    Weights are fractions as given: summing under 1 leaves cash, over 1 is
    leverage. The blend rebalances at the most frequent of its parts'
    cadences unless `rebalance` is set — a monthly part inside a daily blend
    is re-evaluated daily, which is the price of a single book.
    """
    if len(parts) < 2:
        raise ValueError("combine needs at least two strategies")
    if any(w < 0 for _, w in parts) or sum(w for _, w in parts) <= 0:
        raise ValueError("combine weights must be >= 0 and not all zero")
    groups = []
    for sym, w in parts:
        groups.append(
            {
                "step": "group",
                "name": sym.name,
                "weight": w,
                "children": [c.model_dump(by_alias=True) for c in sym.children],
            }
        )
    cadence = rebalance or min((p.rebalance for p, _ in parts), key=_CADENCE_ORDER.index)
    desc = "Blend of " + ", ".join(f"{round(w * 100, 2):g}% {p.name}" for p, w in parts) + "."
    return Symphony.model_validate(
        {
            "id": id,
            "name": name,
            "description": desc,
            "rebalance": cadence,
            "children": [{"step": "wt-cash-specified", "children": groups}],
        }
    )
