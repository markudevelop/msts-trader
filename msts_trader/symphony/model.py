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
_BOOK_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# Column name for a feed's history series in the price table. "@" can never
# be a real ticker (see _TICKER_RE), so it can't collide or reach an order.
FEED_PREFIX = "@PNL:"  # pnlportfolio books (kept for existing strategies)
URL_FEED_PREFIX = "@URL:"  # custom URL feeds
_HTTP_URL_RE = re.compile(r"https?://[^\s]{3,2000}")


def feed_key(book: str) -> str:
    """Series column for a pnlportfolio book."""
    return f"{FEED_PREFIX}{book.upper()}"


def is_feed_key(ticker: str) -> bool:
    return ticker.startswith("@")


# series key -> the Feed block that owns it. Filled as strategies are
# validated, so the price loader can find a feed's history source from the
# column name alone (the URL never has to travel through ticker lists).
FEED_SOURCES: dict[str, "Feed"] = {}


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
    """Either a comparison (`lhs <comparator> rhs|rhs_value`) or a compound:
    `any` (OR) / `all` (AND) of nested conditions — Composer's compound and
    multi-ticker ("any of / all of") conditions."""

    lhs: Metric | None = None
    comparator: Comparator | None = None
    # Exactly one of rhs (another metric) / rhs_value (a fixed number).
    rhs: Metric | None = None
    rhs_value: float | None = None
    any: list[Condition] | None = None
    all: list[Condition] | None = None

    @model_validator(mode="after")
    def _shape(self) -> Condition:
        compound = [x for x in (self.any, self.all) if x is not None]
        if compound:
            if len(compound) > 1 or self.lhs is not None or self.comparator is not None:
                raise ValueError("a condition is either a comparison or one of any/all, not both")
            if not compound[0]:
                raise ValueError("an any/all condition needs at least one condition")
            return self
        if self.lhs is None or self.comparator is None:
            raise ValueError("a comparison needs lhs and comparator")
        if (self.rhs is None) == (self.rhs_value is None):
            raise ValueError("condition needs exactly one of rhs (metric) or rhs_value (number)")
        return self

    def metrics(self) -> list[Metric]:
        """Every indicator this condition reads, nested ones included."""
        if self.any is not None or self.all is not None:
            return [m for c in (self.any or self.all or []) for m in c.metrics()]
        return [m for m in (self.lhs, self.rhs) if m is not None]


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


class Feed(_NodeBase):
    """Hold an externally published set of weights — see feeds.py.

    provider "pnlportfolio": a pnlportfolio.com book (`book`).
    provider "url": any http(s) URL serving `ticker,weight` CSV or JSON
      (`weights_url`), optionally with a `date,nav` history (`nav_url`) and a
      token sent as a Bearer header or a query parameter. The token itself is
      kept in the OS keychain, never in this block.
    """

    step: Literal["feed"] = "feed"
    provider: Literal["pnlportfolio", "url"] = "pnlportfolio"
    book: str | None = None
    name: str | None = None
    weights_url: str | None = None
    nav_url: str | None = None
    auth: Literal["none", "bearer", "query"] = "none"
    token_param: str = Field(default="token", pattern=r"^[A-Za-z0-9_.-]{1,40}$")

    @field_validator("book")
    @classmethod
    def _book(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if not _BOOK_RE.fullmatch(v):
            raise ValueError(f"invalid book id {v!r}")
        return v

    @field_validator("weights_url", "nav_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not _HTTP_URL_RE.fullmatch(v):
            raise ValueError("feed URLs must be http(s)")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Feed:
        if self.provider == "pnlportfolio" and not self.book:
            raise ValueError("a pnlportfolio feed needs a book")
        if self.provider == "url" and not self.weights_url:
            raise ValueError("a URL feed needs weights_url")
        FEED_SOURCES[self.series_key] = self
        return self

    @property
    def ref(self) -> str:
        """Stable identity: the book id, or url-<hash of the weights URL>."""
        if self.provider == "pnlportfolio":
            return self.book  # type: ignore[return-value]
        import hashlib

        return "url-" + hashlib.sha1(self.weights_url.encode("utf-8")).hexdigest()[:10]  # type: ignore[union-attr]

    @property
    def series_key(self) -> str:
        if self.provider == "pnlportfolio":
            return feed_key(self.book)  # type: ignore[arg-type]
        return f"{URL_FEED_PREFIX}{self.ref[4:].upper()}"

    @property
    def label(self) -> str:
        return self.name or self.book or self.ref


class Filter(_NodeBase):
    step: Literal["filter"] = "filter"
    sort_fn: IndicatorFn
    window: int = Field(default=10, ge=1, le=2000)
    select: Literal["top", "bottom"] = "top"
    n: int = Field(default=1, ge=1, le=100)
    children: list[Node] = Field(default_factory=list)


Node = Annotated[
    Union[Asset, Group, WeightEqual, WeightSpecified, WeightInverseVol, If, Filter, Feed],
    Field(discriminator="step"),
]

Condition.model_rebuild()
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
    # Free-form labels for grouping a large library (a "folder" is just a tag).
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for t in v:
            t = " ".join(str(t).split())[:40]
            if t and t.lower() not in {x.lower() for x in out}:
                out.append(t)
        if len(out) > 30:
            raise ValueError("at most 30 tags per strategy")
        return out

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
        elif isinstance(n, Feed):
            seen.add(n.series_key)  # its history series (backtests, indicators)
        elif isinstance(n, If):
            seen.update(m.ticker for m in n.condition.metrics())
    return sorted(seen)


def feed_books(sym: Symphony) -> list[str]:
    """pnlportfolio book ids used by the strategy."""
    return sorted({n.book for n in walk(sym.children) if isinstance(n, Feed) and n.provider == "pnlportfolio"})


def feed_nodes(sym: Symphony) -> list[Feed]:
    """One Feed block per distinct feed (by ref)."""
    out: dict[str, Feed] = {}
    for n in walk(sym.children):
        if isinstance(n, Feed):
            out.setdefault(n.ref, n)
    return [out[k] for k in sorted(out)]


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
