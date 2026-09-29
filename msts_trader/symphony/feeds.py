"""External weight feeds: pnlportfolio.com books as strategy blocks.

A `feed` block means "hold whatever this published book holds today":

  - LIVE (evaluate/run): the book's current target weights, fetched from
    https://api.pnlportfolio.com/v1/sleeves/<book>/weights with your token.
  - BACKTEST / indicators: the book's own published daily NAV
    (/v1/sleeves/<book>/nav.csv, public) stands in as a price series, so a
    feed backtests on its real track record — not on a re-simulation — and
    composes with everything else (blends, filters, comparisons).

The token lives in the OS keychain (or PNLPORTFOLIO_TOKEN for headless runs)
and is never written into strategy files or returned by the UI's API.
"""

from __future__ import annotations

import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from .. import __version__
from .model import FEED_PREFIX, feed_key

API = os.environ.get("PNLPORTFOLIO_API", "https://api.pnlportfolio.com")
TOKEN_ENV = "PNLPORTFOLIO_TOKEN"
_KEYRING_SERVICE = "msts-trader"
_KEYRING_KEY = "feed-token:pnlportfolio"
PREFIX = FEED_PREFIX  # synthetic "ticker" for a feed's NAV series; never tradable
# The books on live.html's Research desk, listed first in the import picker.
FEATURED = ("core", "apex", "hydra", "blend", "unified")
# Their public WAF rejects default urllib/curl agents (see memory: webhook UA 403).
_UA = f"msts-trader/{__version__} (+https://github.com/markudevelop/msts-trader)"
_NAV_TTL = 600.0
_nav_cache: dict[str, tuple[float, pd.Series]] = {}


class FeedError(RuntimeError):
    pass


def series_key(book: str) -> str:
    return feed_key(book)


def is_series_key(ticker: str) -> bool:
    return ticker.startswith(PREFIX)


def book_of(key: str) -> str:
    return key[len(PREFIX) :].lower()


# ── token ──────────────────────────────────────────────────────────────────
def get_token() -> str | None:
    env = os.environ.get(TOKEN_ENV, "").strip()
    if env:
        return env
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY) or None
    except Exception:
        return None


def set_token(token: str) -> None:
    token = token.strip()
    if not token:
        raise FeedError("empty token")
    import keyring

    keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, token)


def clear_token() -> None:
    try:
        import keyring

        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_KEY)
    except Exception:
        pass


# ── HTTP ───────────────────────────────────────────────────────────────────
def _get(path: str, *, token: str | None = None, timeout: float = 30) -> tuple[bytes, dict]:
    headers = {"User-Agent": _UA, "Accept": "application/json, text/csv"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https base)
            return r.read(20 * 1024 * 1024), dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read(2000)
        try:
            detail = json.loads(body)
        except Exception:
            detail = {"message": body.decode("utf-8", "replace")[:200]}
        raise _http_error(e.code, detail, path) from None
    except urllib.error.URLError as e:
        raise FeedError(f"pnlportfolio unreachable ({e.reason})") from None


def _http_error(code: int, detail: dict, path: str) -> FeedError:
    msg = str(detail.get("message") or detail.get("error") or "")
    if code == 401:
        return FeedError("pnlportfolio rejected the token (401) — set a valid one in Studio → Import → pnlportfolio")
    if code == 409 and detail.get("error") == "stale_book":
        asof = detail.get("date") or detail.get("prices_as_of") or "?"
        return FeedError(
            f"the book is not current yet (last published {asof}); pnlportfolio publishes near 15:45 ET — "
            f"run at/after that (the default 15:50 schedule is)"
        )
    if code == 404:
        return FeedError(f"unknown pnlportfolio book ({path})")
    return FeedError(f"pnlportfolio HTTP {code}: {msg or 'error'}")


# ── catalog / weights / NAV ────────────────────────────────────────────────
def catalog() -> list[dict]:
    """Public book list (no token needed), featured books first."""
    raw, _ = _get("/v1/sleeves")
    data = json.loads(raw)
    rows = data.get("sleeves", data) if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = list(rows.values())
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("id") or r.get("runnable") is False:
            continue
        out.append(
            {
                "id": r["id"],
                "label": r.get("label") or r["id"],
                "cagr": r.get("cagr"),
                "sharpe": r.get("sharpe"),
                "num_positions": r.get("num_positions"),
                "date": r.get("date"),
                "featured": r["id"] in FEATURED,
            }
        )
    rank = {b: i for i, b in enumerate(FEATURED)}
    out.sort(key=lambda r: (rank.get(r["id"], len(FEATURED)), str(r["label"]).lower()))
    return out


def book_weights(book: str, token: str | None = None) -> dict:
    """{'weights': {TICKER: w}, 'date': ..., 'stops': {...}} for the CURRENT book.

    Refuses stale books (the API answers 409) and shorts (msts-trader is
    long-only); tickers are upper-cased and zero weights dropped.
    """
    token = token or get_token()
    if not token:
        raise FeedError("no pnlportfolio token — add it in Studio → Import → pnlportfolio (or set PNLPORTFOLIO_TOKEN)")
    raw, _ = _get(f"/v1/sleeves/{urllib.parse.quote(book)}/weights", token=token)
    d = json.loads(raw)
    if d.get("stale_book"):
        raise FeedError(f"the {book} book is stale (last published {d.get('date')})")
    weights = {}
    for t, w in (d.get("weights") or {}).items():
        w = float(w)
        if w < 0:
            raise FeedError(f"the {book} book shorts {t} ({w:+.4f}); msts-trader is long-only")
        if w > 1e-9:
            weights[str(t).strip().upper()] = w
    if not weights:
        raise FeedError(f"the {book} book has no positions")
    return {"weights": weights, "date": d.get("date"), "stops": d.get("stops") or {}}


def nav_series(book: str) -> pd.Series:
    """The book's published daily NAV (net of its modelled costs)."""
    hit = _nav_cache.get(book)
    if hit and time.monotonic() - hit[0] < _NAV_TTL:
        return hit[1]
    raw, _ = _get(f"/v1/sleeves/{urllib.parse.quote(book)}/nav.csv")
    df = pd.read_csv(io.BytesIO(raw), parse_dates=["date"])
    col = "nav_net" if "nav_net" in df.columns else ("nav" if "nav" in df.columns else df.columns[-1])
    s = pd.Series(df[col].to_numpy(dtype=float), index=pd.DatetimeIndex(df["date"]).normalize(), name=series_key(book))
    s = s[~s.index.duplicated(keep="last")].sort_index().dropna()
    if s.empty:
        raise FeedError(f"the {book} book has no NAV history")
    _nav_cache[book] = (time.monotonic(), s)
    return s
