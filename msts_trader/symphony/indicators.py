"""Composer-style indicators over a daily close series.

Every function is CAUSAL: `out[t]` uses closes[0..t] only, so evaluating a
strategy at day t can never peek at t+1. Values that are returns, volatilities
or drawdowns are in PERCENT (Composer's convention); prices stay in dollars.
Insufficient history yields NaN, which the evaluator turns into a clear error.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _returns(c: pd.Series) -> pd.Series:
    return c / c.shift(1) - 1.0


def rsi(c: pd.Series, window: int) -> pd.Series:
    """Wilder's RSI: seed = simple mean of the first `window` changes, then
    avg = (prev * (w - 1) + x) / w. First value lands at index `window`."""
    delta = c.diff().to_numpy()
    n = len(delta)
    out = np.full(n, np.nan)
    if n <= window:
        return pd.Series(out, index=c.index)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    # Start at the first index where `window` consecutive valid changes exist.
    valid = ~np.isnan(delta)
    start = None
    run = 0
    for i in range(n):
        run = run + 1 if valid[i] else 0
        if run == window:
            start = i
            break
    if start is None:
        return pd.Series(out, index=c.index)
    ag = gain[start - window + 1 : start + 1].mean()
    al = loss[start - window + 1 : start + 1].mean()

    def _val(g: float, lo: float) -> float:
        if lo == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / lo)

    out[start] = _val(ag, al)
    for i in range(start + 1, n):
        if not valid[i]:
            break
        ag = (ag * (window - 1) + gain[i]) / window
        al = (al * (window - 1) + loss[i]) / window
        out[i] = _val(ag, al)
    return pd.Series(out, index=c.index)


def max_drawdown(c: pd.Series, window: int) -> pd.Series:
    """Worst peak-to-trough decline (percent, positive) inside the trailing window."""
    arr = c.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    for t in range(window - 1, len(arr)):
        seg = arr[t - window + 1 : t + 1]
        if np.isnan(seg).any():
            continue
        peak = np.maximum.accumulate(seg)
        out[t] = float(np.max(1.0 - seg / peak)) * 100.0
    return pd.Series(out, index=c.index)


def compute(fn: str, c: pd.Series, window: int) -> pd.Series:
    """Full causal indicator series for one close series."""
    w = int(window)
    if fn == "current-price":
        return c.astype(float)
    if fn == "cumulative-return":
        return (c / c.shift(w) - 1.0) * 100.0
    if fn == "moving-average-price":
        return c.rolling(w, min_periods=w).mean()
    if fn == "exponential-moving-average-price":
        # Seeded from the first close; masked until `w` observations exist.
        ema = c.ewm(span=w, adjust=False).mean()
        return ema.where(c.notna().rolling(w, min_periods=w).sum() >= w)
    if fn == "moving-average-return":
        return _returns(c).rolling(w, min_periods=w).mean() * 100.0
    if fn == "relative-strength-index":
        return rsi(c, w)
    if fn == "standard-deviation-price":
        return c.rolling(w, min_periods=w).std(ddof=0)
    if fn == "standard-deviation-return":
        return _returns(c).rolling(w, min_periods=w).std(ddof=0) * 100.0
    if fn == "max-drawdown":
        return max_drawdown(c, w)
    raise ValueError(f"unknown indicator {fn!r}")


def lookback(fn: str, window: int) -> int:
    """Days of history needed before `fn` has its first value (plus warm-up
    for the recursive ones, so live and backtest values agree closely)."""
    w = int(window)
    if fn == "current-price":
        return 0
    if fn in ("relative-strength-index", "exponential-moving-average-price"):
        return w * 4
    return w + 1
