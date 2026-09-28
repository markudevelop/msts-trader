"""Daily-close backtest of a symphony.

Timing (Composer's convention): weights are decided from closes up to day t
and held from the close of t to the close of t+1. Nothing evaluated on day t
can see t+1, which `test_symphony_backtest.py` pins with a lookahead canary.
The live runner trades ~10 minutes before the close on a near-close price, so
the backtest is slightly optimistic by that gap; costs are charged as bps of
turnover on every rebalance.

Weights that sum below 1 leave the remainder in cash earning 0%.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from .evaluate import Engine, EvalError, max_lookback
from .model import Symphony, tickers

TRADING_DAYS = 252


def _period_key(ts: pd.Timestamp, cadence: str):
    if cadence == "daily":
        return ts
    if cadence == "weekly":
        iso = ts.isocalendar()
        return (iso[0], iso[1])
    if cadence == "monthly":
        return (ts.year, ts.month)
    if cadence == "quarterly":
        return (ts.year, (ts.month - 1) // 3)
    return ts.year


def metrics(equity: np.ndarray, dates: list[pd.Timestamp]) -> dict:
    """CAGR / volatility / Sharpe (rf = 0) / max drawdown / total return."""
    if len(equity) < 2:
        return {"cagr": None, "vol": None, "sharpe": None, "max_drawdown": None, "total_return": None}
    rets = equity[1:] / equity[:-1] - 1.0
    years = max((dates[-1] - dates[0]).days / 365.25, 1e-9)
    total = equity[-1] / equity[0]
    cagr = total ** (1.0 / years) - 1.0 if total > 0 else -1.0
    sd = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    vol = sd * math.sqrt(TRADING_DAYS)
    sharpe = float(np.mean(rets)) / sd * math.sqrt(TRADING_DAYS) if sd > 0 else None
    peak = np.maximum.accumulate(equity)
    mdd = float(np.max(1.0 - equity / peak))
    return {
        "cagr": float(cagr),
        "vol": vol,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "total_return": float(total - 1.0),
    }


def first_tradable_index(sym: Symphony, closes: pd.DataFrame) -> int:
    firsts = []
    for t in tickers(sym):
        if t not in closes.columns:
            raise EvalError(f"no price history for {t}")
        fv = closes[t].first_valid_index()
        if fv is None:
            raise EvalError(f"no price history for {t}")
        firsts.append(closes.index.get_loc(fv))
    return (max(firsts) if firsts else 0) + max_lookback(sym)


def run(
    sym: Symphony,
    closes: pd.DataFrame,
    *,
    start: date | None = None,
    end: date | None = None,
    cost_bps: float = 5.0,
    benchmark: str | None = "SPY",
) -> dict:
    closes = closes.sort_index()
    if end is not None:
        closes = closes[closes.index <= pd.Timestamp(end)]
    eng = Engine(sym, closes)
    dates = list(closes.index)
    t0 = first_tradable_index(sym, closes)
    if start is not None:
        t0 = max(t0, int(np.searchsorted(closes.index.values, np.datetime64(pd.Timestamp(start)))))
    if t0 >= len(dates) - 1:
        raise EvalError(
            "not enough price history for this backtest window "
            f"(strategy needs {max_lookback(sym)} trading days of warm-up after its youngest ticker)"
        )

    rets = {t: eng._returns(t) for t in tickers(sym)}
    cost = cost_bps / 1e4
    equity = [1.0]
    eq_dates = [dates[t0]]
    held: dict[str, float] = {}
    allocations: list[dict] = []
    turnover_total = 0.0
    last_period = None

    for t in range(t0, len(dates) - 1):
        period = _period_key(dates[t], sym.rebalance)
        if period != last_period:
            last_period = period
            try:
                target = eng.weights(t)
            except EvalError as e:
                raise EvalError(f"{e} (backtest day {eng._day(t)})") from e
            names = set(target) | set(held)
            turn = sum(abs(target.get(k, 0.0) - held.get(k, 0.0)) for k in names)
            turnover_total += turn
            equity[-1] *= 1.0 - turn * cost
            # Log target CHANGES (> 0.5% on any line), not the daily drift trims.
            prev = allocations[-1]["weights"] if allocations else None
            if prev is None or any(abs(target.get(k, 0.0) - prev.get(k, 0.0)) > 0.005 for k in set(target) | set(prev)):
                allocations.append({"date": eng._day(t), "weights": {k: round(v, 6) for k, v in target.items()}})
            held = dict(target)
        # Hold from close t to close t+1, then let weights drift.
        r = 0.0
        grown = {}
        for k, w in held.items():
            rk = rets[k][t + 1]
            rk = 0.0 if math.isnan(rk) else rk
            r += w * rk
            grown[k] = w * (1.0 + rk)
        equity.append(equity[-1] * (1.0 + r))
        eq_dates.append(dates[t + 1])
        denom = 1.0 + r
        held = {k: v / denom for k, v in grown.items()} if denom > 0 else {}

    eq = np.asarray(equity)
    out = {
        "start": eq_dates[0].date().isoformat(),
        "end": eq_dates[-1].date().isoformat(),
        "dates": [d.date().isoformat() for d in eq_dates],
        "equity": [round(float(x), 6) for x in eq],
        "metrics": metrics(eq, eq_dates),
        "allocations": allocations,
        "cost_bps": cost_bps,
    }
    years = max((eq_dates[-1] - eq_dates[0]).days / 365.25, 1e-9)
    out["metrics"]["annual_turnover"] = float(turnover_total / years)
    if benchmark and benchmark in closes.columns:
        b = closes[benchmark].iloc[t0:].to_numpy(dtype=float)
        if not np.isnan(b[0]):
            b = pd.Series(b).ffill().to_numpy()
            bench = b / b[0]
            out["benchmark"] = {
                "ticker": benchmark,
                "equity": [round(float(x), 6) for x in bench],
                "metrics": metrics(bench, eq_dates),
            }
    return out
