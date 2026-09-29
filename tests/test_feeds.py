"""pnlportfolio feed blocks: client, evaluation (live vs NAV), prices, runner, API."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from msts_trader.symphony import backtest, feeds, prices, runner, store  # noqa: E402
from msts_trader.symphony.evaluate import Engine, EvalError, evaluate  # noqa: E402
from msts_trader.symphony.model import Symphony, feed_books, feed_key, tickers  # noqa: E402
from msts_trader.ui.server import create_app  # noqa: E402
from tests.test_studio import H, TOKEN, _closes, studio_home  # noqa: E402,F401  (autouse: isolates ~/.msts-trader)

BOOK = {"SPY": 0.6, "TLT": 0.4}
# The studio_home fixture swaps prices.load_closes for a fake; keep the real one.
REAL_LOAD_CLOSES = prices.load_closes


def feed_sym(sid="pnl-unified", book="unified", **deploy):
    return Symphony.model_validate(
        {
            "id": sid,
            "name": "Unified",
            "children": [{"step": "feed", "book": book}],
            "deploy": {"broker": "paper", **deploy},
        }
    )


def _nav(book="unified", n=300):
    idx = _closes(["SPY"]).index[-n:]
    rng = np.random.default_rng(11)
    return pd.Series(np.cumprod(1 + rng.normal(0.0004, 0.008, len(idx))), index=idx, name=feed_key(book))


@pytest.fixture(autouse=True)
def fake_feeds(monkeypatch):
    """No network: the book, its NAV and the token are local fakes."""
    state = {"token": None, "weights": dict(BOOK), "stops": {}}
    monkeypatch.setattr(feeds, "nav_series", lambda book: _nav(book))
    monkeypatch.setattr(feeds, "get_token", lambda: state["token"])
    monkeypatch.setattr(feeds, "set_token", lambda t: state.update(token=t))
    monkeypatch.setattr(feeds, "clear_token", lambda: state.update(token=None))

    def fake_weights(book, token=None):
        if not (token or state["token"]):
            raise feeds.FeedError("no pnlportfolio token")
        return {"weights": dict(state["weights"]), "date": "2026-09-25", "stops": dict(state["stops"])}

    monkeypatch.setattr(feeds, "book_weights", fake_weights)
    monkeypatch.setattr(
        feeds,
        "catalog",
        lambda: [
            {
                "id": b,
                "label": b.title(),
                "cagr": 0.2,
                "sharpe": 1.1,
                "num_positions": 2,
                "date": "2026-09-25",
                "featured": True,
            }
            for b in feeds.FEATURED
        ],
    )
    return state


# ── model ─────────────────────────────────────────────────────────────────
def test_feed_block_model():
    s = feed_sym(book="Unified")
    assert s.children[0].book == "unified" and feed_books(s) == ["unified"]
    assert tickers(s) == ["@PNL:UNIFIED"]
    with pytest.raises(Exception):
        feed_sym(book="../etc")


# ── evaluation ────────────────────────────────────────────────────────────
def _frame():
    c = _closes(["SPY", "TLT", "QQQ"])
    c[feed_key("unified")] = _nav()
    return c


def test_feed_expands_live_but_is_nav_in_backtests():
    s = Symphony.model_validate(
        {
            "id": "mix",
            "name": "Mix",
            "children": [
                {
                    "step": "wt-cash-specified",
                    "children": [
                        {"step": "feed", "book": "unified", "weight": 0.5},
                        {"step": "asset", "ticker": "SPY", "weight": 0.5},
                    ],
                }
            ],
        }
    )
    closes = _frame()
    live, _ = evaluate(s, closes, feeds={"unified": BOOK})
    assert live == pytest.approx({"SPY": 0.5 + 0.3, "TLT": 0.2})  # book scaled by its share, SPY netted
    hist, _ = evaluate(s, closes)  # no live books -> the NAV series stands in
    assert hist == pytest.approx({feed_key("unified"): 0.5, "SPY": 0.5})


def test_feed_backtest_is_its_published_nav():
    s = feed_sym()
    closes = _frame()
    r = backtest.run(s, closes, cost_bps=0, benchmark=None)
    nav = closes[feed_key("unified")].loc[r["start"] :].to_numpy()
    np.testing.assert_allclose(r["equity"], nav / nav[0], rtol=1e-5)


def test_filter_can_rank_groups_of_feeds_live():
    s = Symphony.model_validate(
        {
            "id": "f",
            "name": "F",
            "children": [
                {
                    "step": "filter",
                    "sort_fn": "cumulative-return",
                    "window": 20,
                    "n": 1,
                    "children": [
                        {"step": "group", "name": "G", "children": [{"step": "feed", "book": "unified"}]},
                        {"step": "asset", "ticker": "QQQ"},
                    ],
                }
            ],
        }
    )
    # the group's HISTORY must come from the NAV series, not today's live tickers
    w, _ = evaluate(s, _frame(), feeds={"unified": BOOK})
    assert set(w) in ({"SPY", "TLT"}, {"QQQ"})


def test_live_feed_missing_is_an_error():
    eng = Engine(feed_sym(), _frame(), feeds={})
    eng.live_t = len(eng.dates) - 1
    with pytest.raises(EvalError, match="no live weights"):
        eng.weights(eng.live_t)


# ── client parsing (no network) ──────────────────────────────────────────
def test_book_weights_parsing(monkeypatch):
    monkeypatch.undo()  # the real client again, with only _get faked

    def fake_get(path, token=None, timeout=30):
        body = {"weights": {"spy": 0.7, "tlt": 0.3, "zero": 0.0}, "date": "2026-09-25", "stops": {}}
        return json.dumps(body).encode(), {}

    monkeypatch.setattr(feeds, "_get", fake_get)
    assert feeds.book_weights("unified", token="t")["weights"] == {"SPY": 0.7, "TLT": 0.3}

    monkeypatch.setattr(feeds, "_get", lambda *a, **k: (json.dumps({"weights": {"SH": -0.2}}).encode(), {}))
    with pytest.raises(feeds.FeedError, match="long-only"):
        feeds.book_weights("unified", token="t")

    monkeypatch.delenv(feeds.TOKEN_ENV, raising=False)
    monkeypatch.setattr(feeds, "get_token", lambda: None)
    with pytest.raises(feeds.FeedError, match="no pnlportfolio token"):
        feeds.book_weights("unified")


def test_http_errors_are_explained():
    assert "not current yet" in str(feeds._http_error(409, {"error": "stale_book", "date": "2026-09-25"}, "/x"))
    assert "(401)" in str(feeds._http_error(401, {}, "/x"))


def test_prices_load_feed_nav_without_yahoo(monkeypatch):
    def no_yahoo(*a, **k):
        raise AssertionError("feed-only strategies must not hit Yahoo")

    monkeypatch.setattr(prices, "fetch", no_yahoo)
    df = REAL_LOAD_CLOSES([feed_key("unified")])
    assert list(df.columns) == [feed_key("unified")] and len(df) == 300


# ── runner: real rebalance subprocess on paper ───────────────────────────
def test_feed_strategy_runs_on_paper(fake_feeds):
    fake_feeds["token"] = "tok"
    fake_feeds["stops"] = {"SPY": 0.05}
    dry = runner.run(feed_sym(live_enabled=True), mode="dry", closes=_frame())
    assert dry["status"] == "preview", dry
    assert dry["weights"] == {"SPY": 0.6, "TLT": 0.4} and dry["feeds"] == {"unified": "2026-09-25"}
    assert {o["ticker"] for o in dry["preview"]["orders"]} == {"SPY", "TLT"}
    assert "stops" in dry["warnings"][0]


def test_feed_run_without_token_is_a_clear_error():
    res = runner.run(feed_sym(), mode="dry", closes=_frame())
    assert res["status"] == "error" and "token" in res["error"]


# ── API ───────────────────────────────────────────────────────────────────
def test_api_feed_catalog_token_import(fake_feeds):
    c = TestClient(create_app(TOKEN, allowed_origins=set()))
    cat = c.get("/api/feeds", headers=H()).json()
    assert cat["has_token"] is False and [b["id"] for b in cat["books"]] == list(feeds.FEATURED)
    assert c.put("/api/feeds/token", json={"token": "secret-123"}, headers=H()).json() == {"has_token": True}
    assert "secret-123" not in json.dumps(c.get("/api/feeds", headers=H()).json())  # never echoed back
    made = c.post("/api/feeds/import", json={"books": ["core", "unified"]}, headers=H()).json()["created"]
    assert [m["id"] for m in made] == ["pnl-core", "pnl-unified"] and made[1]["children"][0]["step"] == "feed"
    again = c.post("/api/feeds/import", json={"books": ["unified"]}, headers=H()).json()["created"]
    assert again[0]["id"] == "pnl-unified-2"
    assert store.get("pnl-core").children[0].book == "core"
    assert c.delete("/api/feeds/token", headers=H()).json() == {"has_token": False}


def test_backtest_stops_where_a_series_ends():
    """A feed NAV that lags the live book (or a delisted ticker) ends the
    backtest on its last day instead of failing on the gap."""
    closes = _frame()
    closes.loc[closes.index[-40:], feed_key("unified")] = np.nan  # NAV published 40 days behind
    s = Symphony.model_validate(
        {
            "id": "mix",
            "name": "Mix",
            "children": [
                {
                    "step": "wt-cash-equal",
                    "children": [{"step": "feed", "book": "unified"}, {"step": "asset", "ticker": "SPY"}],
                }
            ],
        }
    )
    r = backtest.run(s, closes, cost_bps=0, benchmark="SPY")
    assert r["end"] == closes.index[-41].date().isoformat()
    cmp = backtest.compare([s, feed_sym()], closes, cost_bps=0)
    assert cmp["end"] == r["end"]


def test_live_asof_is_the_book_date(fake_feeds):
    fake_feeds["token"] = "tok"
    closes = _frame()
    closes.loc[closes.index[-40:], feed_key("unified")] = np.nan
    cur = runner.current_weights(feed_sym(), closes=closes.dropna(subset=[feed_key("unified")]))
    assert cur["asof"] == "2026-09-25" and cur["weights"] == {"SPY": 0.6, "TLT": 0.4}


def test_price_table_never_pads_past_a_series_end(monkeypatch):
    nav = _nav()
    monkeypatch.setattr(feeds, "nav_series", lambda book: nav.iloc[:-10])  # NAV stops 10 days early
    df = REAL_LOAD_CLOSES([feed_key("unified")])
    assert df[feed_key("unified")].last_valid_index() == nav.index[-11]


def test_cli_import_feed():
    from click.testing import CliRunner

    from msts_trader.__main__ import main

    r = CliRunner().invoke(main, ["strategy", "import-feed", "core", "unified"])
    assert r.exit_code == 0, r.output
    assert store.get("pnl-core").children[0].book == "core" and store.exists("pnl-unified")
