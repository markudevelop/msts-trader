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
from msts_trader.symphony import prices, runner, store  # noqa: E402
from msts_trader.symphony.model import Symphony  # noqa: E402
from msts_trader.ui import scheduler as sched  # noqa: E402
from msts_trader.ui.server import create_app  # noqa: E402

TOKEN = "test-token"
ORIGIN = "http://127.0.0.1:8765"


def _closes(tickers, start=None, **_):
    idx = pd.bdate_range("2023-01-02", periods=300)
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
