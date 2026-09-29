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


# ── custom URL feeds ───────────────────────────────────────────────────────
# Any http(s) URL serving target weights (`ticker,weight` CSV, or JSON
# {"weights": {...}} / {TICKER: w}), optionally a `date,nav` history URL, and
# an optional token sent as a Bearer header or a query parameter.
MAX_BYTES = 5 * 1024 * 1024
STALE_HOURS = 144  # refuse weights stamped >6 days old (a Friday book is ~5 days old next Tuesday pre-publish)


def history_dir():
    from pathlib import Path

    return Path(os.environ.get("MSTS_FEED_HISTORY_DIR") or os.path.expanduser("~/.msts-trader/feed_history"))


def _token_key(ref: str) -> str:
    return f"feed-token:{ref}"


def get_feed_token(ref: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, _token_key(ref)) or None
    except Exception:
        return None


def set_feed_token(ref: str, token: str) -> None:
    import keyring

    keyring.set_password(_KEYRING_SERVICE, _token_key(ref), token.strip())


def _redact(url: str) -> str:
    """URL without its query string: never echo a token-bearing URL."""
    return url.split("?", 1)[0]


def fetch_url(url: str, *, token: str | None = None, auth: str = "none", token_param: str = "token") -> str:
    if not url.lower().startswith(("http://", "https://")):
        raise FeedError("feed URLs must be http(s)")
    shown = _redact(url)
    headers = {"User-Agent": _UA, "Accept": "application/json, text/csv, text/plain"}
    if token and auth == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    elif token and auth == "query":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode({token_param: token})}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (scheme checked above)
            data = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise FeedError(f"{shown} refused access ({e.code}): check the token and how it is sent") from None
        raise FeedError(f"{shown} returned HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise FeedError(f"{shown} unreachable ({e.reason})") from None
    if len(data) > MAX_BYTES:
        raise FeedError(f"{shown} returned more than 5 MB: is this really a weights file?")
    return data.decode("utf-8-sig", "replace")


def parse_weights(text: str, *, now=None) -> dict:
    """Weights from `ticker,weight` CSV (msts-trader's own format, `# asof:`
    honoured) or JSON. Long-only; zero rows dropped; stale stamps refused."""
    from datetime import datetime, timezone

    from ..csv_parser import CSVParseError, parse_csv
    from ..safety import check_stale, parse_asof

    stripped = text.strip()
    asof = None
    stops: dict = {}
    if stripped.startswith("{"):
        try:
            d = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise FeedError(f"weights JSON is invalid: {e}") from None
        nested = isinstance(d.get("weights"), dict)
        raw = d["weights"] if nested else d
        # Publishers label freshness differently (asof / as_of / date / ...);
        # the NEWEST parseable stamp is the one that describes these weights.
        stamps = []
        for k in ("asof", "as_of", "date", "trade_date", "updated"):
            v = d.get(k)
            if isinstance(v, str):
                ts = parse_asof(f"# asof: {v}")
                if ts is not None:
                    stamps.append((ts, v))
        weights = {}
        for t, w in raw.items():
            if isinstance(w, bool) or not isinstance(w, (int, float)):
                if not nested:
                    continue  # a flat mapping may carry metadata keys
                raise FeedError(f"weight for {t!r} is not a number")
            if w < 0:
                raise FeedError(f"{t} has a negative weight ({w}); msts-trader is long-only")
            if w > 1e-9:
                weights[str(t).strip().upper()] = float(w)
        if stamps:
            asof, asof_raw = max(stamps)
            age = ((now or datetime.now(timezone.utc)) - asof).total_seconds() / 3600
            if age > STALE_HOURS:
                raise FeedError(f"weights are stale (as of {asof_raw}, {age:.0f}h old)")
        stops = (d.get("stops") or {}) if nested else {}
    else:
        stale = check_stale(text, STALE_HOURS, now=now)
        if stale:
            raise FeedError(stale)
        try:
            targets = parse_csv(text)
        except CSVParseError as e:
            raise FeedError(f"weights CSV: {e}") from None
        weights = {t.ticker: float(t.weight) for t in targets if t.weight > 0}
        stops = {t.ticker: float(t.stop_pct) for t in targets if t.stop_pct}
        asof = parse_asof(text)
    if not weights:
        raise FeedError("the feed has no positions")
    return {"weights": weights, "date": asof.date().isoformat() if asof else None, "stops": stops}


def parse_history(text: str) -> pd.Series:
    """`date,<value>` CSV -> daily series. Value column: nav_net / nav / value /
    equity / close, else the last column."""
    try:
        df = pd.read_csv(io.StringIO(text), comment="#")
    except Exception as e:
        raise FeedError(f"history CSV is invalid: {e}") from None
    if df.shape[1] < 2:
        raise FeedError("history CSV needs a date column and a value column")
    cols = {str(c).lower().strip(): c for c in df.columns}
    dcol = cols.get("date", df.columns[0])
    vcol = next((cols[c] for c in ("nav_net", "nav", "value", "equity", "close") if c in cols), df.columns[-1])
    s = pd.Series(
        pd.to_numeric(df[vcol], errors="coerce").to_numpy(dtype=float),
        index=pd.to_datetime(df[dcol], errors="coerce"),
    )
    s = s[s.index.notna()].dropna()
    s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if len(s) < 2 or (s <= 0).any():
        raise FeedError("history needs at least two positive values")
    return s


def url_weights(node, token: str | None = None) -> dict:
    tok = token if token is not None else get_feed_token(node.ref)
    if node.auth != "none" and not tok:
        raise FeedError(f"feed {node.label!r} needs a token: set it in Studio (Import, Custom feed)")
    text = fetch_url(node.weights_url, token=tok, auth=node.auth, token_param=node.token_param)
    return parse_weights(text)


def url_history(node, token: str | None = None) -> pd.Series | None:
    """The feed's value history: its history URL, else what Studio recorded."""
    if node.nav_url:
        tok = token if token is not None else get_feed_token(node.ref)
        s = parse_history(fetch_url(node.nav_url, token=tok, auth=node.auth, token_param=node.token_param))
        return s.rename(node.series_key)
    return recorded_history(node)


# ── recorded history (URL feeds without a history URL) ─────────────────────
def _history_path(ref: str):
    return history_dir() / f"{ref}.json"


def record_weights(node, weights: dict, day) -> None:
    """Keep one weights snapshot per (ET) day for a URL feed with no history URL."""
    if node.provider != "url" or node.nav_url:
        return
    p = _history_path(node.ref)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        data = {}
    data[str(day)] = {k: round(float(v), 8) for k, v in sorted(weights.items())}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def recorded_history(node) -> pd.Series | None:
    """NAV rebuilt from recorded daily weights x real closes: the snapshot
    recorded on/before day d-1 earns day d's returns. None until there is a
    snapshot and at least one close after it."""
    p = _history_path(node.ref)
    if not p.exists():
        return None
    try:
        snaps = {pd.Timestamp(k): v for k, v in json.loads(p.read_text(encoding="utf-8")).items()}
    except Exception:
        return None
    if not snaps:
        return None
    from . import prices

    first = min(snaps)
    tickers = sorted({t for w in snaps.values() for t in w})
    closes = prices.load_closes(tickers, first.date())
    closes = closes[closes.index >= first]
    if len(closes) < 2:
        return None
    rets = closes.pct_change()
    days = sorted(snaps)
    nav = [1.0]
    k = 0
    for i in range(1, len(closes)):
        prev = closes.index[i - 1]
        while k + 1 < len(days) and days[k + 1] <= prev:
            k += 1
        w = snaps[days[k]] if days[k] <= prev else {}
        r = 0.0
        for t, wt in w.items():
            if t in rets.columns and not pd.isna(rets[t].iloc[i]):
                r += wt * rets[t].iloc[i]
        nav.append(nav[-1] * (1.0 + r))
    return pd.Series(nav, index=closes.index, name=node.series_key)


# ── one entry point for both providers ─────────────────────────────────────
def live_weights(node) -> dict:
    if node.provider == "pnlportfolio":
        return book_weights(node.book)
    return url_weights(node)


def history(node) -> pd.Series | None:
    if node.provider == "pnlportfolio":
        return nav_series(node.book)
    return url_history(node)
