"""Out-of-sample tracking: how a deployed strategy has ACTUALLY done since go-live,
next to what its model (the backtest) says it should have done.

Actual NAV is rebuilt, not sampled: a sleeve's holdings only change through
this tool (runs) and its cash only through runs and invest/divest, so a
snapshot of {cash, holdings, contributed} after each of those events, marked
at each day's close, gives the exact sleeve NAV for every trading day since
go-live — no daemon has to be running every evening.

Returns are TIME-WEIGHTED: invest/divest move NAV but are not performance.

Go-live = the first LIVE run that executed (paper counts: it is the strategy's
own out-of-sample record). Everything before it is in-sample by definition.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd

from .. import sleeves
from ..market_hours import ET
from . import backtest, prices, store
from .evaluate import EvalError
from .model import Symphony


# ── sleeve ledger access (file-only; no broker login) ─────────────────────
def sleeve_ledgers(sid: str, broker: str) -> list[dict]:
    """Every ledger (account) of `broker` that knows sleeve `sid`."""
    out = []
    if not sleeves.LEDGER_DIR.exists():
        return out
    for p in sorted(sleeves.LEDGER_DIR.glob(f"{broker}_*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        book = (data.get("sleeves") or {}).get(sid)
        cash = (data.get("cash") or {}).get(sid)
        if book is None and cash is None:
            continue
        out.append(
            {
                "account": data.get("account"),
                "cash": cash,
                "contributed": (data.get("contributed") or {}).get(sid),
                "holdings": {t: q for t, q in sorted((book or {}).items()) if Decimal(q) != 0},
                "pending": [x for x in data.get("pending") or [] if x.get("sleeve") == sid],
            }
        )
    return out


def _pick(ledgers: list[dict], account: str | None) -> dict | None:
    if not ledgers:
        return None
    if account:
        for lg in ledgers:
            acct = str(lg.get("account") or "")
            if acct == account or acct.endswith(account):
                return lg
    return ledgers[0]


def snapshot(sym: Symphony, event: str) -> dict | None:
    """Record the sleeve's current state (after a live run or a capital change)."""
    lg = _pick(sleeve_ledgers(sym.id, sym.deploy.broker), sym.deploy.account)
    if lg is None:
        return None
    return store.log_snapshot(
        {
            "strategy": sym.id,
            "broker": sym.deploy.broker,
            "account": lg["account"],
            "event": event,
            "cash": lg["cash"] or "0",
            "contributed": lg["contributed"],
            "holdings": lg["holdings"],
        }
    )


# ── go-live and NAV reconstruction ────────────────────────────────────────
def _et_date(ts: str) -> date:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ET).date()


def go_live(sid: str, broker: str | None = None) -> date | None:
    """ET date of the first executed LIVE run (optionally on one broker)."""
    first = None
    for e in store.read_runs(sid, limit=100000):  # newest first
        if e.get("mode") == "live" and e.get("status") in ("executed", "partial"):
            if broker is None or e.get("broker") == broker:
                first = e
    return _et_date(first["ts"]) if first else None


def nav_series(snaps: list[dict], closes: pd.DataFrame, start: date) -> pd.DataFrame:
    """Daily sleeve NAV / contributed from `start` through the last close.

    Each trading day uses the latest snapshot taken on or before that ET date,
    marked at that day's close. Columns: nav, contributed, twr (index, 1.0 at
    the first day).
    """
    snaps = sorted(snaps, key=lambda s: s["ts"])
    # A ticker can miss a day other tickers have (today's bar not published
    # yet, a listing-venue holiday): mark it at its last close, up to 5 days.
    closes = closes.ffill(limit=5)
    idx = closes.index[closes.index >= pd.Timestamp(start)]
    rows = []
    k = -1
    for d in idx:
        while k + 1 < len(snaps) and _et_date(snaps[k + 1]["ts"]) <= d.date():
            k += 1
        if k < 0:
            continue
        s = snaps[k]
        nav = float(s.get("cash") or 0)
        for tkr, qty in (s.get("holdings") or {}).items():
            px = closes[tkr].loc[d] if tkr in closes.columns else float("nan")
            if pd.isna(px):
                raise ValueError(f"no close for {tkr} on {d.date()}")
            nav += float(qty) * float(px)
        contributed = s.get("contributed")
        rows.append((d, nav, float(contributed) if contributed not in (None, "") else float("nan")))
    df = pd.DataFrame(rows, columns=["date", "nav", "contributed"]).set_index("date")
    if df.empty:
        df["twr"] = []
        return df
    # Time-weighted index: strip each day's net contribution out of its return.
    flows = df["contributed"].ffill().fillna(0).diff().fillna(0).to_numpy()
    nav = df["nav"].to_numpy()
    twr = [1.0]
    for i in range(1, len(nav)):
        prev = nav[i - 1]
        r = (nav[i] - flows[i]) / prev - 1.0 if prev > 0 else 0.0
        twr.append(twr[-1] * (1.0 + r))
    df["twr"] = twr
    return df


LIVE_MIN_DAYS = 30  # below this, annualised CAGR / Sharpe are noise — report the plain return


def live_stats(sid: str, broker: str, closes: pd.DataFrame | None = None) -> dict | None:
    """Out-of-sample stats of the sleeve since go-live, from the time-weighted
    index (deposits / withdrawals stripped out): CAGR, max drawdown, Sharpe,
    total return. None when the strategy never executed live. `closes` defaults
    to the disk cache only, so the dashboard stays offline and fast; a held
    ticker with no cached close gives `reason` instead of numbers."""
    live = go_live(sid, broker)
    if live is None:
        return None
    out: dict = {"since": live.isoformat()}
    snaps = [s for s in store.read_snapshots(sid) if s.get("broker") == broker]
    if not snaps:
        return {**out, "reason": "no sleeve snapshots yet"}
    held = {t for s in snaps for t in (s.get("holdings") or {})}
    if closes is None:
        closes = prices.cached_closes(held, live)
    missing = sorted(held - set(closes.columns))
    if missing or closes.empty:
        return {**out, "reason": "no cached prices for " + (", ".join(missing[:5]) or "the sleeve") + " yet"}
    try:
        nav = nav_series(snaps, closes, live)
    except ValueError as e:
        return {**out, "reason": str(e)}
    if len(nav) < 2:
        return {**out, "days": len(nav), "asof": nav.index[-1].date().isoformat() if len(nav) else None}
    m = backtest.metrics(nav["twr"].to_numpy(dtype=float), list(nav.index))
    span = (nav.index[-1] - nav.index[0]).days
    short = span < LIVE_MIN_DAYS
    return {
        **out,
        "asof": nav.index[-1].date().isoformat(),
        "days": len(nav),
        "total_return": m["total_return"],
        "cagr": None if short else m["cagr"],
        "sharpe": None if short else m["sharpe"],
        "max_drawdown": m["max_drawdown"],
    }


def _ret(series) -> float | None:
    if len(series) < 1:
        return None
    return float(series[-1] / series[0] - 1.0)


def oos(sym: Symphony, closes: pd.DataFrame | None, *, benchmark: str | None = "SPY", cost_bps: float = 5.0) -> dict:
    """Actual (sleeve) vs model (backtest) vs benchmark since go-live."""
    live = go_live(sym.id, sym.deploy.broker)
    if live is None:
        return {"live_since": None, "reason": "no executed live run yet — out-of-sample tracking starts at go-live"}
    snaps = [s for s in store.read_snapshots(sym.id) if s.get("broker") == sym.deploy.broker]
    nav = nav_series(snaps, closes, live)
    out: dict = {"live_since": live.isoformat(), "dates": [d.date().isoformat() for d in nav.index]}
    out["actual"] = {
        "nav": [round(float(x), 2) for x in nav["nav"]],
        "contributed": [None if math.isnan(x) else round(float(x), 2) for x in nav["contributed"]],
        "index": [round(float(x), 6) for x in nav["twr"]],
    }
    tw = nav["twr"].to_numpy()
    last_nav = float(nav["nav"].iloc[-1]) if len(nav) else None
    last_contrib = nav["contributed"].dropna()
    pnl = last_nav - float(last_contrib.iloc[-1]) if last_nav is not None and len(last_contrib) else None
    out["metrics"] = {
        "actual_return": _ret(tw),
        "pnl": None if pnl is None else round(pnl, 2),
        "nav": None if last_nav is None else round(last_nav, 2),
        "actual_max_drawdown": float(np.max(1 - tw / np.maximum.accumulate(tw))) if len(tw) else None,
    }
    # Model: the same tree, backtested from go-live (its warm-up uses earlier data).
    if len(out["dates"]) <= 1:
        # Go-live day: nothing has moved yet (a backtest needs two closes).
        out["model"] = {"index": [1.0] * len(out["dates"])}
        out["metrics"]["model_return"] = 0.0 if out["dates"] else None
    else:
        try:
            bt = backtest.run(sym, closes, start=live, cost_bps=cost_bps, benchmark=benchmark)
            m = dict(zip(bt["dates"], bt["equity"]))
            out["model"] = {"index": [m.get(d) for d in out["dates"]]}
            out["metrics"]["model_return"] = _ret([x for x in out["model"]["index"] if x is not None])
        except EvalError as e:  # e.g. the tree was edited to need more history than exists
            out["model"] = None
            out["model_error"] = str(e)
    if benchmark and benchmark in closes.columns and len(nav):
        b = closes[benchmark].reindex(nav.index).ffill().to_numpy(dtype=float)
        out["benchmark"] = {"ticker": benchmark, "index": [round(float(x / b[0]), 6) for x in b]}
        out["metrics"]["benchmark_return"] = _ret(b)
    if out["metrics"].get("model_return") is not None and out["metrics"]["actual_return"] is not None:
        out["metrics"]["tracking_gap"] = out["metrics"]["actual_return"] - out["metrics"]["model_return"]
    return out
