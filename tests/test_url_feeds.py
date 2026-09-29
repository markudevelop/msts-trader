"""Custom URL feeds: fetch/auth against a real local HTTP server, parsing,
recorded history, API test/import, runner on paper, CLI."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")

from click.testing import CliRunner  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from msts_trader.__main__ import main  # noqa: E402
from msts_trader.symphony import backtest, feeds, runner, store  # noqa: E402
from msts_trader.symphony.evaluate import EvalError  # noqa: E402
from msts_trader.symphony.model import Feed, Symphony  # noqa: E402
from msts_trader.ui.server import create_app  # noqa: E402
from tests.test_studio import H, TOKEN, _closes, studio_home  # noqa: E402,F401  (autouse: isolates ~/.msts-trader)

SECRET = "s3cret-token"
REAL_LOAD_CLOSES = __import__("msts_trader.symphony.prices", fromlist=["x"]).load_closes  # before fixtures fake it


def _now_stamp(hours_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="text/plain"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        self.server.seen.append({"path": path, "query": query, "auth": self.headers.get("Authorization")})
        if path == "/bearer.csv":
            if self.headers.get("Authorization") != f"Bearer {SECRET}":
                return self._send(401, "no")
            return self._send(200, f"# asof: {_now_stamp()}\nticker,weight\nSPY,0.6\nTLT,0.4\n", "text/csv")
        if path == "/query.json":
            if f"token={SECRET}" not in query:
                return self._send(403, "no")
            return self._send(
                200, json.dumps({"weights": {"spy": 0.5, "qqq": 0.5}, "asof": _now_stamp()}), "application/json"
            )
        if path == "/open.csv":
            return self._send(200, "ticker,weight\nSPY,1.0\n", "text/csv")
        if path == "/stale.csv":
            return self._send(200, f"# asof: {_now_stamp(24 * 10)}\nticker,weight\nSPY,1\n", "text/csv")
        if path == "/nav.csv":
            idx = _closes(["SPY"]).index[-200:]
            rows = "\n".join(f"{d.date()},{1 + i * 0.001:.6f}" for i, d in enumerate(idx))
            return self._send(200, "date,nav\n" + rows + "\n", "text/csv")
        return self._send(404, "nope")


@pytest.fixture(scope="module")
def http():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.seen = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch, tmp_path):
    vault: dict[str, str] = {}
    monkeypatch.setattr(feeds, "get_feed_token", lambda ref: vault.get(ref))
    monkeypatch.setattr(feeds, "set_feed_token", lambda ref, tok: vault.__setitem__(ref, tok.strip()))
    monkeypatch.setenv("MSTS_FEED_HISTORY_DIR", str(tmp_path / "feed_history"))
    return vault


def url_node(url, **kw):
    return Feed.model_validate({"provider": "url", "name": kw.pop("name", "My feed"), "weights_url": url, **kw})


# ── model ─────────────────────────────────────────────────────────────────
def test_url_feed_model():
    n = url_node("https://example.com/w.csv?x=1")
    assert n.ref.startswith("url-") and n.series_key.startswith("@URL:") and n.label == "My feed"
    with pytest.raises(Exception):
        url_node("ftp://example.com/w.csv")
    with pytest.raises(Exception):
        Feed.model_validate({"provider": "url"})
    with pytest.raises(Exception):
        Feed.model_validate({"provider": "pnlportfolio"})


# ── fetch + auth over real HTTP ───────────────────────────────────────────
def test_bearer_and_query_auth(http):
    srv, base = http
    w = feeds.url_weights(url_node(base + "/bearer.csv", auth="bearer"), token=SECRET)
    assert w["weights"] == {"SPY": 0.6, "TLT": 0.4} and w["date"]
    assert srv.seen[-1]["auth"] == f"Bearer {SECRET}" and SECRET not in srv.seen[-1]["query"]

    w = feeds.url_weights(url_node(base + "/query.json", auth="query"), token=SECRET)
    assert w["weights"] == {"SPY": 0.5, "QQQ": 0.5}
    assert srv.seen[-1]["auth"] is None and f"token={SECRET}" in srv.seen[-1]["query"]


def test_bad_token_error_never_leaks_it(http):
    _, base = http
    with pytest.raises(feeds.FeedError) as e:
        feeds.url_weights(url_node(base + "/query.json", auth="query"), token="wrong-token")
    assert "refused access (403)" in str(e.value) and "wrong-token" not in str(e.value)
    with pytest.raises(feeds.FeedError, match="needs a token"):
        feeds.url_weights(url_node(base + "/bearer.csv", auth="bearer"))


def test_stale_and_missing(http):
    _, base = http
    with pytest.raises(feeds.FeedError, match="stale"):
        feeds.url_weights(url_node(base + "/stale.csv"))
    with pytest.raises(feeds.FeedError, match="404"):
        feeds.url_weights(url_node(base + "/missing.csv"))


# ── parsing ───────────────────────────────────────────────────────────────
def test_parse_weights_formats():
    assert feeds.parse_weights("ticker,weight\nspy,0.7\ngld,0.3\nbil,0\n")["weights"] == {"SPY": 0.7, "GLD": 0.3}
    flat = feeds.parse_weights('{"SPY": 0.5, "TLT": 0.5, "note": "x"}')
    assert flat["weights"] == {"SPY": 0.5, "TLT": 0.5}
    with pytest.raises(feeds.FeedError, match="long-only"):
        feeds.parse_weights('{"weights": {"SH": -0.1}}')
    with pytest.raises(feeds.FeedError):
        feeds.parse_weights("ticker,weight\nSPY,-0.1\n")
    stops = feeds.parse_weights("ticker,weight,stop_pct\nSPY,1,0.05\n")
    assert stops["stops"] == {"SPY": 0.05}


def test_parse_history_columns():
    s = feeds.parse_history("Date,Close\n2026-01-02,100\n2026-01-05,101\n")
    assert list(s) == [100.0, 101.0]
    s = feeds.parse_history("date,nav_gross,nav_net\n2026-01-02,1,1\n2026-01-05,1.1,1.05\n")
    assert list(s) == [1.0, 1.05]  # prefers nav_net
    with pytest.raises(feeds.FeedError):
        feeds.parse_history("date,nav\n2026-01-02,1\n")


# ── history ───────────────────────────────────────────────────────────────
def test_history_url_feed_backtests(http):
    _, base = http
    n = url_node(base + "/open.csv", nav_url=base + "/nav.csv", name="With history")
    s = Symphony.model_validate({"id": "h", "name": "H", "children": [n.model_dump(exclude_none=True)]})
    hist = feeds.history(n)  # fetched over HTTP from the history URL
    frame = _closes(["SPY"])
    frame[n.series_key] = hist.reindex(frame.index)
    r = backtest.run(s, frame, cost_bps=0, benchmark=None)
    assert r["equity"][-1] == pytest.approx(hist.iloc[-1] / hist.loc[pd.Timestamp(r["start"])], rel=1e-6)


def test_recorded_history_rebuilds_nav():
    n = url_node("https://example.invalid/w.csv", name="Rec")
    closes = _closes(["SPY", "TLT"])
    d0, d1 = closes.index[-10].date(), closes.index[-5].date()
    feeds.record_weights(n, {"SPY": 1.0}, d0)
    feeds.record_weights(n, {"TLT": 1.0}, d1)
    nav = feeds.recorded_history(n)
    c = closes[closes.index >= pd.Timestamp(d0)]
    exp = [1.0]
    for i in range(1, len(c)):
        t = "SPY" if c.index[i - 1] < pd.Timestamp(d1) else "TLT"
        exp.append(exp[-1] * c[t].iloc[i] / c[t].iloc[i - 1])
    np.testing.assert_allclose(nav.to_numpy(), exp, rtol=1e-9)
    assert feeds.recorded_history(url_node("https://example.invalid/none.csv")) is None


# ── runner on paper (real rebalance subprocess) ──────────────────────────
def test_url_feed_runs_on_paper_and_records(http):
    _, base = http
    n = url_node(base + "/open.csv", name="Open feed")
    s = Symphony.model_validate(
        {
            "id": "open-feed",
            "name": "Open feed",
            "children": [n.model_dump(exclude_none=True)],
            "deploy": {"broker": "paper"},
        }
    )
    res = runner.run(s, mode="dry")  # no history yet: live evaluation still works
    assert res["status"] == "preview", res
    assert res["weights"] == {"SPY": 1.0} and res["feeds"] == {"Open feed": None}
    assert [o["ticker"] for o in res["preview"]["orders"]] == ["SPY"]
    rec = json.loads((feeds.history_dir() / f"{n.ref}.json").read_text())
    assert list(rec.values()) == [{"SPY": 1.0}]
    with pytest.raises(EvalError, match="no history yet|insufficient|history"):
        backtest.run(s, pd.DataFrame(index=_closes(["SPY"]).index[-5:]), cost_bps=0, benchmark=None)


# ── API ───────────────────────────────────────────────────────────────────
def test_api_test_and_import_keep_token_out_of_files(http, fake_keychain):
    _, base = http
    c = TestClient(create_app(TOKEN, allowed_origins=set()))
    body = {"weights_url": base + "/bearer.csv", "nav_url": base + "/nav.csv", "auth": "bearer"}
    bad = c.post("/api/feeds/url/test", json=body, headers=H())
    assert bad.status_code == 422 and "needs a token" in bad.json()["detail"]
    ok = c.post("/api/feeds/url/test", json={**body, "token": SECRET}, headers=H()).json()
    assert ok["positions"] == 2 and ok["top"][0] == ["SPY", 0.6] and ok["history"]["days"] == 200
    assert SECRET not in json.dumps(ok)

    made = c.post("/api/feeds/url/import", json={**body, "name": "Bearer feed", "token": SECRET}, headers=H()).json()
    assert made["id"] == "bearer-feed"
    node = made["children"][0]
    assert node["provider"] == "url" and node["auth"] == "bearer" and "token" not in node
    assert SECRET not in (store.strategies_dir() / "bearer-feed.json").read_text()
    assert list(fake_keychain.values()) == [SECRET]
    # a stored token is reused: no token needed on the next test
    again = c.post("/api/feeds/url/test", json=body, headers=H()).json()
    assert again["token_stored"] is True and again["positions"] == 2
    no_tok = c.post(
        "/api/feeds/url/import", json={"name": "X", "weights_url": base + "/query.json", "auth": "query"}, headers=H()
    )
    assert no_tok.status_code == 422


# ── CLI ───────────────────────────────────────────────────────────────────
def test_cli_import_url(http):
    _, base = http
    r = CliRunner().invoke(main, ["strategy", "import-url", "Open CLI", base + "/open.csv"])
    assert r.exit_code == 0, r.output
    s = store.get("open-cli")
    assert s.children[0].provider == "url" and s.children[0].weights_url.endswith("/open.csv")
    bad = CliRunner().invoke(main, ["strategy", "import-url", "Bad", base + "/bearer.csv", "--auth", "bearer"])
    assert bad.exit_code != 0 and "needs a token" in bad.output


def test_new_feed_without_history_still_evaluates_live(http):
    """Real price loader: a URL feed with no history yields a one-day frame, so
    live evaluation works from day one while backtests say why they can't."""
    _, base = http
    n = url_node(base + "/open.csv", name="Brand new")
    frame = REAL_LOAD_CLOSES([n.series_key])
    assert len(frame) == 1 and n.series_key not in frame.columns
    s = Symphony.model_validate({"id": "bn", "name": "BN", "children": [n.model_dump(exclude_none=True)]})
    cur = runner.current_weights(s, closes=frame)
    assert cur["weights"] == {"SPY": 1.0}


def test_json_freshness_uses_newest_stamp():
    fresh = _now_stamp(2)
    d = {"weights": {"SPY": 1}, "as_of": "2020-01-01T00:00:00Z", "date": fresh}
    assert feeds.parse_weights(json.dumps(d))["weights"] == {"SPY": 1.0}
    with pytest.raises(feeds.FeedError, match="stale"):
        feeds.parse_weights(json.dumps({"weights": {"SPY": 1}, "date": "2020-01-01"}))
