"""Evaluate a symphony tree to target weights on a given day.

`Engine(sym, closes)` precomputes nothing up front; indicator series are cached
per (fn, ticker, window) on first use, and node weights per (node, day), so a
backtest that evaluates every day stays linear in history length.

Non-asset children inside `filter` / `wt-inverse-vol` are ranked on a
SYNTHETIC price series: the subtree's own daily returns over a trailing window
(Composer does the same when you filter groups).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from . import indicators
from .model import (
    Asset,
    Condition,
    Feed,
    Filter,
    Group,
    If,
    Metric,
    Symphony,
    WeightEqual,
    WeightInverseVol,
    WeightSpecified,
    walk,
)

Weights = dict[str, float]


class EvalError(ValueError):
    """Strategy can't be evaluated on this day (usually: not enough history)."""


def _scale(w: Weights, k: float) -> Weights:
    return {t: v * k for t, v in w.items()}


def _add(into: Weights, w: Weights) -> None:
    for t, v in w.items():
        into[t] = into.get(t, 0.0) + v


def _equal(parts: list[Weights]) -> Weights:
    out: Weights = {}
    if not parts:
        return out
    k = 1.0 / len(parts)
    for p in parts:
        _add(out, _scale(p, k))
    return out


_CMP = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
}


class Engine:
    def __init__(self, sym: Symphony, closes: pd.DataFrame, feeds: dict[str, dict[str, float]] | None = None):
        """`feeds` = {feed ref: {ticker: weight}} — the LIVE books. When given, a
        feed block expands to its real tickers on `live_t` (the evaluation
        day); on every other day (backtests, ranking a group's history) it is
        one position in the book's NAV series."""
        self.sym = sym
        self.feeds = feeds
        self.live_t: int | None = None
        self.closes = closes.sort_index()
        self.dates = list(self.closes.index)
        self._ind: dict[tuple, np.ndarray] = {}
        self._memo: dict[tuple[int, int], Weights] = {}
        self._ret: dict[str, np.ndarray] = {}

    # ── data access ───────────────────────────────────────────────────
    def _series(self, ticker: str) -> pd.Series:
        if ticker not in self.closes.columns:
            raise EvalError(f"no price history for {ticker}")
        return self.closes[ticker]

    def _returns(self, ticker: str) -> np.ndarray:
        r = self._ret.get(ticker)
        if r is None:
            c = self._series(ticker).to_numpy(dtype=float)
            r = np.full(len(c), np.nan)
            r[1:] = c[1:] / c[:-1] - 1.0
            self._ret[ticker] = r
        return r

    def indicator(self, fn: str, ticker: str, window: int, t: int) -> float:
        key = (fn, ticker, window)
        arr = self._ind.get(key)
        if arr is None:
            arr = indicators.compute(fn, self._series(ticker), window).to_numpy(dtype=float)
            self._ind[key] = arr
        v = arr[t]
        if math.isnan(v):
            raise EvalError(f"{fn}({ticker}, {window}d) has insufficient history on {self._day(t)}")
        return float(v)

    def _day(self, t: int) -> str:
        d = self.dates[t]
        return d.date().isoformat() if hasattr(d, "date") else str(d)

    # ── node evaluation ───────────────────────────────────────────────
    def metric(self, m: Metric, t: int) -> float:
        return self.indicator(m.fn, m.ticker, m.window, t)

    def condition(self, c: Condition, t: int) -> bool:
        if c.any is not None:
            return any(self.condition(x, t) for x in c.any)
        if c.all is not None:
            return all(self.condition(x, t) for x in c.all)
        lhs = self.metric(c.lhs, t)
        rhs = self.metric(c.rhs, t) if c.rhs is not None else float(c.rhs_value)
        return _CMP[c.comparator](lhs, rhs)

    def node(self, n, t: int) -> Weights:
        key = (id(n), t)
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        out = self._node(n, t)
        self._memo[key] = out
        return out

    def nodes_equal(self, nodes: list, t: int) -> Weights:
        return _equal([self.node(c, t) for c in nodes])

    def _node(self, n, t: int) -> Weights:
        if isinstance(n, Asset):
            v = self._series(n.ticker).iloc[t]
            if pd.isna(v):
                raise EvalError(f"{n.ticker} has no price on {self._day(t)}")
            return {n.ticker: 1.0}
        if isinstance(n, Feed):
            if self.feeds is not None and t == self.live_t:
                book = self.feeds.get(n.ref)
                if book is None:
                    raise EvalError(f"no live weights for feed {n.label!r}")
                return dict(book)
            key = n.series_key
            if key not in self.closes.columns:
                raise EvalError(f"feed {n.label} has no history yet (add a history URL, or let Studio record it)")
            v = self._series(key).iloc[t]
            if pd.isna(v):
                raise EvalError(f"feed {n.label} has no history on {self._day(t)}")
            return {key: 1.0}
        if isinstance(n, (Group, WeightEqual)):
            return self.nodes_equal(n.children, t)
        if isinstance(n, WeightSpecified):
            out: Weights = {}
            for c in n.children:
                _add(out, _scale(self.node(c, t), float(c.weight or 0.0)))
            return out
        if isinstance(n, If):
            branch = n.then if self.condition(n.condition, t) else n.otherwise
            return self.nodes_equal(branch, t)
        if isinstance(n, Filter):
            scored = [(self.child_metric(c, n.sort_fn, n.window, t), i, c) for i, c in enumerate(n.children)]
            # Stable: ties keep tree order.
            scored.sort(key=lambda x: (-x[0] if n.select == "top" else x[0], x[1]))
            return _equal([self.node(c, t) for _, _, c in scored[: n.n]])
        if isinstance(n, WeightInverseVol):
            inv = []
            for c in n.children:
                vol = self.child_metric(c, "standard-deviation-return", n.window, t)
                inv.append(1.0 / max(vol, 1e-9))
            total = sum(inv)
            out = {}
            for c, k in zip(n.children, inv):
                _add(out, _scale(self.node(c, t), k / total))
            return out
        raise EvalError(f"unsupported node {type(n).__name__}")

    def child_metric(self, c, fn: str, window: int, t: int) -> float:
        """An indicator of a child: the asset's own series, or the subtree's
        synthetic equity curve over a trailing window."""
        if isinstance(c, Asset):
            return self.indicator(fn, c.ticker, window, t)
        if isinstance(c, Feed):
            return self.indicator(fn, c.series_key, window, t)
        span = max(indicators.lookback(fn, window), 2)
        start = t - span
        if start < 1:
            raise EvalError(f"insufficient history to rank a group on {self._day(t)}")
        eq = [1.0]
        for d in range(start + 1, t + 1):
            w = self.node(c, d - 1)
            r = 0.0
            for tk, wt in w.items():
                rr = self._returns(tk)[d]
                if math.isnan(rr):
                    raise EvalError(f"{tk} has no return on {self._day(d)}")
                r += wt * rr
            eq.append(eq[-1] * (1.0 + r))
        v = indicators.compute(fn, pd.Series(eq), window).iloc[-1]
        if pd.isna(v):
            raise EvalError(f"insufficient history to rank a group on {self._day(t)}")
        return float(v)

    def weights(self, t: int) -> Weights:
        w = self.nodes_equal(self.sym.children, t)
        return {k: v for k, v in w.items() if v > 1e-9}


def max_lookback(sym: Symphony) -> int:
    """Conservative history (trading days) needed before the first evaluation."""
    need = 1
    for n in walk(sym.children):
        if isinstance(n, If):
            for m in n.condition.metrics():
                need = max(need, indicators.lookback(m.fn, m.window))
        elif isinstance(n, Filter):
            # Group children rank on a synthetic curve that itself needs history.
            need = max(need, indicators.lookback(n.sort_fn, n.window) * 2)
        elif isinstance(n, WeightInverseVol):
            need = max(need, (n.window + 1) * 2)
    return need


def to_csv(weights: Weights, *, asof: datetime | None = None, fallback: str | None = None) -> str:
    """Weights -> `ticker,weight` CSV that `csv_parser.parse_csv` accepts.

    An all-cash result still needs one data row, so it emits `fallback,0`:
    under `--sleeve` the sweep then sells everything the sleeve owns.
    """
    asof = asof or datetime.now(timezone.utc)
    lines = [f"# asof: {asof.strftime('%Y-%m-%dT%H:%M:%SZ')}", "ticker,weight"]
    for t in sorted(weights):
        lines.append(f"{t},{round(weights[t], 6):.6f}")
    if not weights:
        if not fallback:
            raise EvalError("strategy is 100% cash and has no ticker to emit")
        lines.append(f"{fallback},0")
    return "\n".join(lines) + "\n"


def evaluate(
    sym: Symphony, closes: pd.DataFrame, asof: date | None = None, *, feeds: dict[str, dict[str, float]] | None = None
) -> tuple[Weights, str]:
    """Weights for the last trading day on/before `asof` (default: latest).

    With `feeds`, feed blocks resolve to their live books (real tickers)."""
    eng = Engine(sym, closes, feeds=feeds)
    if not eng.dates:
        raise EvalError("no price data")
    t = len(eng.dates) - 1
    if asof is not None:
        idx = [i for i, d in enumerate(eng.dates) if pd.Timestamp(d).date() <= asof]
        if not idx:
            raise EvalError(f"no price data on or before {asof}")
        t = idx[-1]
    eng.live_t = t
    return eng.weights(t), eng._day(t)
