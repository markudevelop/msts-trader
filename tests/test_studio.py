"""Studio: strategy store, runner (real `rebalance` subprocess on paper),
UI API security + endpoints, scheduler, and the `strategy` CLI."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")

from click.testing import CliRunner  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from msts_trader import sleeves  # noqa: E402
from msts_trader.__main__ import main  # noqa: E402
from msts_trader.market_hours import ET  # noqa: E402
from msts_trader.symphony import backtest, performance, prices, runner, store  # noqa: E402
from msts_trader.symphony.model import Symphony  # noqa: E402
from msts_trader.ui import scheduler as sched  # noqa: E402
from msts_trader.ui.server import create_app  # noqa: E402

TOKEN = "test-token"
ORIGIN = "http://127.0.0.1:8765"


def _closes(tickers, start=None, **_):
    # Calendar-daily through TODAY (ET): go-live is stamped with the real date,
    # so out-of-sample tests need prices on it whatever weekday the suite runs.
    idx = pd.date_range(end=pd.Timestamp(datetime.now(ET).date()), periods=300, freq="D")
    rng = np.random.default_rng(3)
    return pd.DataFrame(
        {t: 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, len(idx))) for t in sorted(set(tickers))}, index=idx
    )


@pytest.fixture(autouse=True)
def studio_home(tmp_path, monkeypatch):
    """Point every piece of state — including the `rebalance` SUBPROCESS,
    which re-imports and re-expands ~ — at a temp home."""
    home = tmp_path / "home"
    (home / ".msts-trader").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("MSTS_STRATEGIES_DIR", str(home / ".msts-trader" / "strategies"))
    monkeypatch.setenv("MSTS_RUNS_LOG", str(home / ".msts-trader" / "runs.jsonl"))
    monkeypatch.setenv("MSTS_SNAPSHOTS_LOG", str(home / ".msts-trader" / "sleeve_snapshots.jsonl"))
    monkeypatch.setenv("PAPER_STARTING_CASH", "100000")
    monkeypatch.setattr(sleeves, "LEDGER_DIR", home / ".msts-trader" / "sleeves")
    monkeypatch.setattr(prices, "load_closes", _closes)
    # Paper fills at stored last_prices (yfinance is disabled suite-wide).
    (home / ".msts-trader" / "paper_state.json").write_text(
        json.dumps(
            {"cash": "100000", "positions": {}, "last_prices": {"SPY": "500", "TLT": "90", "BIL": "91.5", "QQQ": "400"}}
        )
    )
    return home


def make_sym(sid="momo", **deploy) -> Symphony:
    return Symphony.model_validate(
        {
            "id": sid,
            "name": "Momo",
            "children": [
                {
                    "step": "wt-cash-specified",
                    "children": [
                        {"step": "asset", "ticker": "SPY", "weight": 0.6},
                        {"step": "asset", "ticker": "TLT", "weight": 0.4},
                    ],
                }
            ],
            "deploy": {"broker": "paper", **deploy},
        }
    )


@pytest.fixture
def client():
    return TestClient(create_app(TOKEN, allowed_origins={ORIGIN}))


def H(**extra):
    return {"X-MSTS-Token": TOKEN, **extra}


# ── store ─────────────────────────────────────────────────────────────────
def test_store_round_trip_and_backup(studio_home):
    s = make_sym()
    p = store.save(s)
    assert store.get("momo") == s
    store.save(s.model_copy(update={"name": "Momo 2"}))
    assert p.with_suffix(".json.bak").exists()
    assert [x.id for x in store.list_all()] == ["momo"]
    assert store.unique_id("momo") == "momo-2"
    with pytest.raises(store.StoreError):
        store.get("../etc/passwd")


# ── runner: real rebalance subprocess on the paper broker ────────────────
def test_runner_dry_then_live_on_paper(studio_home):
    s = make_sym(live_enabled=True)
    dry = runner.run(s, mode="dry")
    assert dry["status"] == "preview", dry
    assert {o["ticker"] for o in dry["preview"]["orders"]} == {"SPY", "TLT"}
    assert dry["preview"]["sleeve"] == "momo"

    live = runner.run(s, mode="live")
    assert live["status"] == "executed", live
    ledger = sleeves.load("paper", "PAPER")
    assert ledger.tally("momo", "SPY") > 0 and ledger.tally("momo", "TLT") > 0

    again = runner.run(s, mode="dry")
    assert again["status"] == "nothing-to-do", again
    assert [r["mode"] for r in store.read_runs("momo")] == ["dry", "live", "dry"]


def test_runner_refuses_live_unless_enabled(studio_home):
    with pytest.raises(runner.RunError, match="not enabled for live"):
        runner.run(make_sym(), mode="live")


def test_runner_reports_eval_errors(studio_home, monkeypatch):
    def boom(*a, **k):
        raise prices.PriceError("no price data for ZZZZ")

    monkeypatch.setattr(prices, "load_closes", boom)
    res = runner.run(make_sym(), mode="dry")
    assert res["status"] == "error" and "ZZZZ" in res["error"]


# ── API security ──────────────────────────────────────────────────────────
def test_api_requires_token(client):
    assert client.get("/api/meta").status_code == 401
    assert client.get("/api/meta", headers={"X-MSTS-Token": "nope"}).status_code == 401
    assert client.get("/api/meta", headers=H()).status_code == 200


def test_api_refuses_foreign_origin(client):
    r = client.post("/api/strategies", json={"name": "x"}, headers=H(Origin="https://evil.example"))
    assert r.status_code == 403
    assert client.get("/api/meta", headers=H(Origin=ORIGIN)).status_code == 200


# ── API endpoints ─────────────────────────────────────────────────────────
def test_api_crud_eval_backtest(client):
    body = make_sym().model_dump(by_alias=True)
    body.pop("id")
    created = client.post("/api/strategies", json=body, headers=H()).json()
    sid = created["id"]
    assert sid == "momo"
    assert [s["id"] for s in client.get("/api/strategies", headers=H()).json()] == ["momo"]

    created["name"] = "Renamed"
    assert client.put(f"/api/strategies/{sid}", json=created, headers=H()).json()["name"] == "Renamed"
    assert client.put(f"/api/strategies/{sid}", json={**created, "id": "other"}, headers=H()).status_code == 422
    bad = {**created, "children": [{"step": "asset", "ticker": "SPY; rm"}]}
    assert client.put(f"/api/strategies/{sid}", json=bad, headers=H()).status_code == 422

    ev = client.post("/api/eval", json=created, headers=H()).json()
    assert ev["weights"] == {"SPY": 0.6, "TLT": 0.4}

    bt = client.post("/api/backtest", json={"strategy": created, "cost_bps": 0}, headers=H()).json()
    assert bt["metrics"]["cagr"] is not None and bt["benchmark"]["ticker"] == "SPY"

    assert client.delete(f"/api/strategies/{sid}", headers=H()).status_code == 200
    assert client.get(f"/api/strategies/{sid}", headers=H()).status_code == 404


def test_api_import(client):
    edn = '{:step :root :name "Imp" :children [{:step :wt-cash-equal :children [{:step :asset :ticker "SPY"}]}]}'
    r = client.post("/api/import", json={"text": edn}, headers=H()).json()
    assert r["strategy"]["id"] == "imp" and store.exists("imp")
    assert client.post("/api/import", json={"text": "{:step :bogus}"}, headers=H()).status_code == 422


def test_api_live_gate_and_paper_execution(client):
    store.save(make_sym())
    # live refused while live_enabled is off
    r = client.post("/api/strategies/momo/run", json={"mode": "live", "confirm": "momo"}, headers=H())
    assert r.status_code == 403
    store.save(make_sym(live_enabled=True))
    # live refused without the typed confirmation
    r = client.post("/api/strategies/momo/run", json={"mode": "live", "confirm": "Momo"}, headers=H())
    assert r.status_code == 403
    # capital, preview, then execute on paper
    cap = client.post("/api/strategies/momo/capital", json={"action": "invest", "amount": "10000"}, headers=H())
    assert cap.status_code == 200, cap.text
    assert cap.json()["sleeve"][0]["cash"] == "10000"
    dry = client.post("/api/strategies/momo/run", json={"mode": "dry"}, headers=H()).json()
    notional = sum(float(o["notional"]) for o in dry["preview"]["orders"])
    assert notional == pytest.approx(10000, rel=0.05)  # sized to the sleeve's capital, not the account
    live = client.post("/api/strategies/momo/run", json={"mode": "live", "confirm": "momo"}, headers=H()).json()
    assert live["status"] == "executed", live
    held = client.get("/api/strategies/momo/sleeve", headers=H()).json()["ledgers"][0]["holdings"]
    assert set(held) == {"SPY", "TLT"}
    assert len(client.get("/api/runs?strategy=momo", headers=H()).json()) == 2


# ── scheduler ─────────────────────────────────────────────────────────────
def test_scheduler_due_logic(studio_home):
    s = make_sym(schedule_enabled=True, schedule_time="15:50")
    store.save(s)
    tue = datetime(2026, 9, 29, 15, 55, tzinfo=ET)
    assert sched.due(s, tue)
    assert not sched.due(s, tue.replace(hour=15, minute=40))  # before schedule time
    assert not sched.due(s, tue.replace(hour=16, minute=5))  # after the close
    assert not sched.due(s, datetime(2026, 10, 3, 15, 55, tzinfo=ET))  # Saturday
    assert not sched.due(s.model_copy(update={"deploy": s.deploy.model_copy(update={"schedule_enabled": False})}), tue)

    results = sched.Scheduler().tick(tue)
    assert [r["status"] for r in results] == ["preview"]  # dry-run: live_enabled is off
    assert results[0]["mode"] == "dry" and results[0]["source"] == "scheduler"
    assert sched.Scheduler().tick(tue) == []  # already ran this period


def test_scheduler_half_day_and_cadence():
    s = make_sym(schedule_enabled=True, schedule_time="15:50")
    # 2026-11-27 (day after Thanksgiving) closes 13:00 -> run by 12:50
    assert sched.run_time(s, datetime(2026, 11, 27).date()).strftime("%H:%M") == "12:50"
    d = datetime(2026, 9, 29).date()
    assert sched.period_key(d, "monthly") == "2026-09" and sched.period_key(d, "weekly") == "2026-W40"


# ── CLI ───────────────────────────────────────────────────────────────────
def test_strategy_cli(studio_home, tmp_path):
    store.save(make_sym())
    cli = CliRunner()
    r = cli.invoke(main, ["strategy", "list"])
    assert r.exit_code == 0 and "momo" in r.output
    r = cli.invoke(main, ["strategy", "eval", "momo"])
    assert r.exit_code == 0 and "SPY,0.600000" in r.output
    r = cli.invoke(main, ["strategy", "backtest", "momo"])
    assert r.exit_code == 0 and "CAGR" in r.output
    r = cli.invoke(main, ["strategy", "run", "momo"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["status"] == "preview"
    r = cli.invoke(main, ["strategy", "run", "momo", "--yes"])
    assert r.exit_code != 0 and "not enabled for live" in r.output
    f = Path(tmp_path) / "sym.edn"
    f.write_text('{:step :root :name "From File" :children [{:step :asset :ticker "QQQ"}]}', encoding="utf-8")
    r = cli.invoke(main, ["strategy", "import", str(f)])
    assert r.exit_code == 0 and store.exists("from-file")


# ── out-of-sample tracking ────────────────────────────────────────────────
def _snap(ts, cash, holdings, contributed):
    return {"ts": ts, "cash": str(cash), "holdings": holdings, "contributed": str(contributed)}


def test_nav_series_marks_holdings_and_strips_contributions():
    idx = pd.bdate_range("2026-03-02", periods=6)
    closes = pd.DataFrame({"SPY": [100.0, 110.0, 99.0, 105.0, 120.0, 90.0]}, index=idx)
    snaps = [
        # 15:50 ET buy on day 0: 50 SPY + $5,000 cash, $10k contributed
        _snap("2026-03-02T20:50:00Z", 5000, {"SPY": "50"}, 10000),
        # +$10,000 invested on day 3 (after the close, still that ET date)
        _snap("2026-03-05T22:00:00Z", 15000, {"SPY": "50"}, 20000),
    ]
    df = performance.nav_series(snaps, closes, idx[0].date())
    assert list(df["nav"]) == [10000.0, 10500.0, 9950.0, 20250.0, 21000.0, 19500.0]
    # TWR ignores the $10k flow: day-3 return is (20250 - 10000) / 9950 - 1
    r3 = df["twr"].iloc[3] / df["twr"].iloc[2] - 1
    assert r3 == pytest.approx(10250 / 9950 - 1)
    assert df["twr"].iloc[1] == pytest.approx(1.05)


def test_nav_series_uses_no_future_snapshot():
    idx = pd.bdate_range("2026-03-02", periods=3)
    closes = pd.DataFrame({"SPY": [100.0, 100.0, 100.0]}, index=idx)
    snaps = [
        _snap("2026-03-02T20:50:00Z", 0, {"SPY": "10"}, 1000),
        _snap("2026-03-04T20:50:00Z", 0, {"SPY": "20"}, 1000),
    ]
    df = performance.nav_series(snaps, closes, idx[0].date())
    assert list(df["nav"]) == [1000.0, 1000.0, 2000.0]  # day 1 still sees the day-0 book


def test_oos_not_live_yet(studio_home):
    s = make_sym(live_enabled=True)
    store.save(s)
    assert performance.oos(s, None)["live_since"] is None


def test_api_oos_performance_and_backtest_split(client):
    store.save(make_sym(live_enabled=True))
    client.post("/api/strategies/momo/capital", json={"action": "invest", "amount": "10000"}, headers=H())
    before = client.post("/api/backtest", json={"strategy": store.get("momo").model_dump(by_alias=True)}, headers=H())
    assert before.json()["oos_start"] is None  # nothing out-of-sample before go-live
    live = client.post("/api/strategies/momo/run", json={"mode": "live", "confirm": "momo"}, headers=H()).json()
    assert live["status"] == "executed", live

    perf = client.get("/api/strategies/momo/performance", headers=H()).json()
    today = datetime.now(ET).date().isoformat()
    assert perf["live_since"] == today and perf["dates"][0] == today
    snaps = store.read_snapshots("momo")
    assert [x["event"] for x in snaps] == ["invest", "run"] and set(snaps[-1]["holdings"]) == {"SPY", "TLT"}
    # NAV = the post-run book marked at today's close (the fixture's closes, not paper's fill prices)
    closes = _closes(["SPY", "TLT"])
    book = snaps[-1]
    expect = float(book["cash"]) + sum(float(q) * closes[t].iloc[-1] for t, q in book["holdings"].items())
    assert perf["actual"]["nav"][0] == pytest.approx(expect, abs=0.01)
    assert perf["actual"]["contributed"][0] == 10000
    assert perf["actual"]["index"][0] == 1.0
    assert perf["model"]["index"][0] is not None and perf["benchmark"]["ticker"] == "SPY"

    after = client.post("/api/backtest", json={"strategy": store.get("momo").model_dump(by_alias=True)}, headers=H())
    assert after.json()["oos_start"] == today


# ── compare ───────────────────────────────────────────────────────────────
def test_compare_aligns_common_window_and_keeps_ids(client):
    a = make_sym("a")
    b = Symphony.model_validate(
        {
            "id": "b",
            "name": "Trend",
            "children": [
                {
                    "step": "if",
                    "condition": {
                        "lhs": {"fn": "current-price", "ticker": "SPY"},
                        "comparator": "gt",
                        "rhs": {"fn": "moving-average-price", "ticker": "SPY", "window": 50},
                    },
                    "then": [{"step": "asset", "ticker": "QQQ"}],
                    "else": [{"step": "asset", "ticker": "BIL"}],
                }
            ],
        }
    )
    body = {"strategies": [a.model_dump(by_alias=True), b.model_dump(by_alias=True)], "cost_bps": 0}
    r = client.post("/api/compare", json=body, headers=H()).json()
    assert [x["id"] for x in r["series"]] == ["a", "b"]
    n = len(r["dates"])
    assert all(len(x["equity"]) == n and x["equity"][0] == pytest.approx(1.0, abs=1e-9) for x in r["series"])
    closes = _closes(["SPY", "TLT", "QQQ", "BIL"])
    # the common window starts after B's 50-day warm-up, not A's
    assert r["start"] == closes.index[backtest.first_tradable_index(b, closes)].date().isoformat()
    assert r["correlation"][0][0] == pytest.approx(1.0) and len(r["correlation"]) == 2
    assert r["benchmark"]["ticker"] == "SPY"


def test_compare_validation(client):
    one = make_sym("a").model_dump(by_alias=True)
    assert client.post("/api/compare", json={"strategies": [one]}, headers=H()).status_code == 422
    assert client.post("/api/compare", json={"strategies": [one, one]}, headers=H()).status_code == 422


def test_compare_identical_strategies_fully_correlated():
    closes = _closes(["SPY", "TLT"])
    r = backtest.compare([make_sym("a"), make_sym("b")], closes, cost_bps=0)
    assert r["correlation"][0][1] == pytest.approx(1.0)
    assert r["series"][0]["equity"] == r["series"][1]["equity"]


def test_scheduler_early_close_window():
    s = make_sym(schedule_enabled=True, schedule_time="15:50")
    half = datetime(2026, 11, 27, 12, 55, tzinfo=ET)  # early close 13:00 -> runs from 12:50
    assert sched.due(s, half)
    assert not sched.due(s, half.replace(hour=12, minute=45))
    assert not sched.due(s, half.replace(hour=13, minute=5))


def test_oos_multi_day_actual_vs_model(studio_home):
    s = make_sym(live_enabled=True)
    store.save(s)
    closes = _closes(["SPY", "TLT"])
    d0 = closes.index[-30]
    ts = (d0 + pd.Timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")  # 15:50/16:00 ET that day
    px = closes.loc[d0]
    spy, tlt = 6000 / px["SPY"], 4000 / px["TLT"]  # the model's 60/40 bought at that close
    store._append(
        store.snapshots_log(),
        {"ts": ts, "strategy": "momo", "broker": "paper", "event": "run", "cash": "0",
         "holdings": {"SPY": str(spy), "TLT": str(tlt)}, "contributed": "10000"},
    )  # fmt: skip
    store._append(
        store.runs_log(), {"ts": ts, "strategy": "momo", "mode": "live", "broker": "paper", "status": "executed"}
    )
    r = performance.oos(s, closes, cost_bps=0)
    assert r["live_since"] == d0.date().isoformat() and len(r["dates"]) == 30
    assert r["model"]["index"][0] == pytest.approx(1.0)
    # Held without trading, the book drifts; the daily-rebalanced model does not -> close but not identical.
    assert r["metrics"]["actual_return"] == pytest.approx(r["actual"]["nav"][-1] / 10000 - 1, abs=1e-6)
    assert abs(r["metrics"]["tracking_gap"]) < 0.02
    assert r["metrics"]["pnl"] == pytest.approx(r["actual"]["nav"][-1] - 10000, abs=0.01)


# ── scheduler runs on New York time whatever the machine's timezone ───────
SYD = __import__("zoneinfo").ZoneInfo("Australia/Sydney")


def test_scheduler_uses_new_york_clock_from_sydney(studio_home):
    s = make_sym(schedule_enabled=True, schedule_time="15:50")
    store.save(s)
    # Tue 2026-09-29 15:55 ET == Wed 2026-09-30 05:55 AEST in Sydney
    syd = datetime(2026, 9, 29, 15, 55, tzinfo=ET).astimezone(SYD)
    assert (syd.day, syd.hour, syd.minute) == (30, 5, 55)
    assert sched.due(s, syd)  # judged on the NY date/time, not Sydney's Wednesday
    assert not sched.due(s, datetime(2026, 9, 29, 15, 40, tzinfo=ET).astimezone(SYD))
    # Sat 2026-10-03 05:55 AEST is still Friday afternoon in New York -> a trading day
    fri = datetime(2026, 10, 3, 5, 55, tzinfo=SYD)
    assert fri.astimezone(ET).weekday() == 4 and sched.due(s, fri)
    # Mon 2026-10-05 05:55 AEDT is Sunday in New York -> no run
    assert not sched.due(s, datetime(2026, 10, 5, 5, 55, tzinfo=SYD))
    res = sched.Scheduler().tick(syd)
    assert [r["period"] for r in res] == ["2026-09-29"]  # NY trading date, not Sydney's


def test_scheduler_follows_new_york_dst_not_local_dst():
    s = make_sym(schedule_enabled=True, schedule_time="15:50")
    # US DST ends 2026-11-01; Sydney DST started 2026-10-04. 15:50 ET maps to
    # 06:50 AEDT before the US change and 07:50 AEDT after it.
    before = sched.run_time(s, datetime(2026, 10, 30).date()).astimezone(SYD)
    after = sched.run_time(s, datetime(2026, 11, 2).date()).astimezone(SYD)
    assert (before.hour, before.minute) == (6, 50) and (after.hour, after.minute) == (7, 50)
    assert sched.due(s, datetime(2026, 11, 3, 7, 55, tzinfo=SYD))  # Mon 15:55 EST
    assert not sched.due(s, datetime(2026, 11, 3, 6, 55, tzinfo=SYD))  # Mon 14:55 EST: too early


def test_scheduler_state_reports_both_clocks():
    st = sched.tz_status()
    assert st["market_tz"] == "America/New_York" and st["market_now"][-6:] in ("-04:00", "-05:00")


# ── combine (blend strategies into one) ───────────────────────────────────
def _qqq(sid="q", rebalance="daily"):
    return Symphony.model_validate(
        {"id": sid, "name": "QQQ", "rebalance": rebalance, "children": [{"step": "asset", "ticker": "QQQ"}]}
    )


def test_combine_structure():
    from msts_trader.symphony.model import combine

    blend = combine([(make_sym("a"), 0.7), (_qqq(rebalance="monthly"), 0.3)], id="mix", name="Mix")
    top = blend.children[0]
    assert top.step == "wt-cash-specified" and [g.weight for g in top.children] == [0.7, 0.3]
    assert [g.name for g in top.children] == ["Momo", "QQQ"] and blend.rebalance == "daily"  # most frequent
    assert "70% Momo" in blend.description
    with pytest.raises(ValueError):
        combine([(make_sym("a"), 1.0)], id="x", name="x")
    with pytest.raises(ValueError):
        combine([(make_sym("a"), -0.1), (_qqq(), 1.0)], id="x", name="x")


def test_blend_return_is_weighted_sum_of_parts():
    """Daily parts, daily blend, no costs: every day's blend return is exactly
    0.5*rA + 0.5*rB — the blend is the two strategies run side by side."""
    from msts_trader.symphony.model import combine

    closes = _closes(["SPY", "TLT", "QQQ"])
    a, b = make_sym("a"), _qqq()
    blend = combine([(a, 0.5), (b, 0.5)], id="mix", name="Mix")
    ra, rb, rm = (backtest.run(x, closes, cost_bps=0, benchmark=None) for x in (a, b, blend))
    assert ra["dates"] == rb["dates"] == rm["dates"]
    ea, eb, em = (np.asarray(r["equity"]) for r in (ra, rb, rm))
    np.testing.assert_allclose(em[1:] / em[:-1], 0.5 * (ea[1:] / ea[:-1]) + 0.5 * (eb[1:] / eb[:-1]), rtol=1e-5)


def test_api_combine_then_compare_and_save(client):
    a, b = make_sym("a").model_dump(by_alias=True), _qqq().model_dump(by_alias=True)
    blend = client.post("/api/combine", json={"strategies": [a, b], "weights": [0.6, 0.4]}, headers=H()).json()
    assert blend["id"] == "__blend__" and blend["name"] == "Momo + QQQ"
    cmp = client.post("/api/compare", json={"strategies": [a, b, blend], "cost_bps": 0}, headers=H()).json()
    assert [x["id"] for x in cmp["series"]] == ["a", "q", "__blend__"]
    saved = client.post("/api/strategies", json={**blend, "id": None, "name": "My blend"}, headers=H()).json()
    assert saved["id"] == "my-blend" and store.get("my-blend").children[0].step == "wt-cash-specified"
    bad = client.post("/api/combine", json={"strategies": [a, b], "weights": [1.0]}, headers=H())
    assert bad.status_code == 422
