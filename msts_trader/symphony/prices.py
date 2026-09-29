"""Daily adjusted closes via yfinance, cached on disk.

Cache: one CSV per ticker under ~/.msts-trader/prices/ (override with
MSTS_PRICES_DIR). Only bars dated BEFORE today (US/Eastern) are persisted —
today's bar is intraday until the close, so it is always refetched. Each load
refetches a few recent days on top of the cache so late adjustments land.

`fetch` is the single network seam; tests monkeypatch it.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .model import FEED_PREFIX

ET = ZoneInfo("America/New_York")
DEFAULT_START = date(2000, 1, 1)
_REFRESH_DAYS = 7


class PriceError(RuntimeError):
    pass


def cache_dir() -> Path:
    return Path(os.environ.get("MSTS_PRICES_DIR") or os.path.expanduser("~/.msts-trader/prices"))


def fetch(tickers: list[str], start: date) -> pd.DataFrame:
    """Download daily adjusted closes: index = date, columns = tickers."""
    try:
        import yfinance as yf
    except ImportError as e:  # pragma: no cover - depends on the extra
        raise PriceError('price data needs yfinance: pip install "msts-trader[ui]"') from e
    raw = yf.download(
        tickers, start=start.isoformat(), auto_adjust=True, progress=False, threads=True, group_by="column"
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw
    if isinstance(close, pd.Series):
        close = close.to_frame(tickers[0])
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.astype(float)


def _path(ticker: str) -> Path:
    return cache_dir() / f"{ticker.replace('/', '_').replace('^', '_')}.csv"


def _read_cache(ticker: str) -> pd.Series | None:
    p = _path(ticker)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, parse_dates=["date"])
    except Exception:
        return None  # corrupt cache: refetch from scratch
    if df.empty:
        return None
    return pd.Series(df["close"].to_numpy(dtype=float), index=pd.DatetimeIndex(df["date"]), name=ticker)


def _write_cache(ticker: str, s: pd.Series, today: date) -> None:
    s = s.dropna()
    s = s[s.index.date < today]
    if s.empty:
        return
    p = _path(ticker)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    pd.DataFrame({"date": s.index.strftime("%Y-%m-%d"), "close": s.to_numpy()}).to_csv(tmp, index=False)
    os.replace(tmp, p)


def _meta() -> dict:
    try:
        return json.loads((cache_dir() / "_coverage.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_meta(meta: dict) -> None:
    p = cache_dir() / "_coverage.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=0, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def _fill_gaps(df: pd.DataFrame) -> pd.DataFrame:
    """Forward-fill short INTERIOR gaps (holiday-calendar mismatches) but never
    past a series' last real value — a feed NAV that stops publishing or a
    delisted ticker must end there, not grow flat fake days."""
    last = {c: df[c].last_valid_index() for c in df.columns}
    out = df.ffill(limit=5)
    for c, lv in last.items():
        if lv is not None:
            out.loc[out.index > lv, c] = float("nan")
    return out


def load_closes(tickers: list[str], start: date | None = None, *, today: date | None = None) -> pd.DataFrame:
    """Aligned daily closes for `tickers` from `start` through today.

    Missing values after a ticker's first bar are forward-filled (holiday
    calendar mismatches); before its first bar they stay NaN, which the
    evaluator reports as insufficient history.
    """
    tickers = sorted({t.upper() for t in tickers})
    if not tickers:
        raise PriceError("strategy references no tickers")
    # Feed books price from their own published NAV, not from Yahoo.
    feed_cols = [t for t in tickers if t.startswith(FEED_PREFIX)]
    tickers = [t for t in tickers if not t.startswith(FEED_PREFIX)]
    extra = {}
    for key in feed_cols:
        from . import feeds

        try:
            extra[key] = feeds.nav_series(key[len(FEED_PREFIX) :].lower())
        except feeds.FeedError as e:
            raise PriceError(str(e)) from e
    if not tickers:
        df = pd.DataFrame(extra).sort_index()
        return _fill_gaps(df[df.index >= pd.Timestamp(start or DEFAULT_START)])
    start = start or DEFAULT_START
    today = today or datetime.now(ET).date()

    # coverage[t] = earliest start ever fetched for t. A ticker younger than
    # `start` has a first bar after it forever; coverage says the cache is
    # still complete, so it isn't refetched from scratch on every load.
    coverage = _meta()
    cached: dict[str, pd.Series] = {}
    fetch_from: date = today - timedelta(days=_REFRESH_DAYS)
    for t in tickers:
        s = _read_cache(t)
        covered = t in coverage and date.fromisoformat(coverage[t]) <= start
        if s is None or not covered:
            fetch_from = min(fetch_from, start)
        else:
            cached[t] = s
            fetch_from = min(fetch_from, s.index.max().date() - timedelta(days=_REFRESH_DAYS))

    fresh = fetch(tickers, fetch_from)
    cols = {}
    for t in tickers:
        parts = [x for x in (cached.get(t), fresh[t] if t in fresh.columns else None) if x is not None]
        if not parts:
            raise PriceError(f"no price data for {t} (check the ticker symbol)")
        merged = pd.concat(parts)
        merged = merged[~merged.index.duplicated(keep="last")].sort_index().dropna()
        if merged.empty:
            raise PriceError(f"no price data for {t} (check the ticker symbol)")
        _write_cache(t, merged, today)
        if fetch_from <= start or t in cached:
            prev = coverage.get(t)
            coverage[t] = min(start, date.fromisoformat(prev)).isoformat() if prev else start.isoformat()
        cols[t] = merged
    _save_meta(coverage)
    df = pd.DataFrame({**cols, **extra}).sort_index()
    df = df[df.index >= pd.Timestamp(start)]
    return _fill_gaps(df)
