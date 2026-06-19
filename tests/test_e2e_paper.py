"""End-to-end: drive the CLI through a real rebalance on the paper broker.

Exercises the whole pipeline in-process (parse -> balances/positions/quote
-> diff -> margin-aware -> execute -> fill log -> idempotency), with the
keychain and paper state isolated. No network, no real money.
"""
from __future__ import annotations

import json as _json
from decimal import Decimal

import keyring
import pytest
from click.testing import CliRunner
from keyring.backend import KeyringBackend

from msts_trader.__main__ import main


class _MemKeyring(KeyringBackend):
    priority = 100

    def __init__(self):
        self._s = {}

    def get_password(self, service, username):
        return self._s.get((service, username))

    def set_password(self, service, username, password):
        self._s[(service, username)] = password

    def delete_password(self, service, username):
        self._s.pop((service, username), None)


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    backend = _MemKeyring()
    monkeypatch.setattr(keyring, "get_password", lambda s, u: backend.get_password(s, u))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: backend.set_password(s, u, p))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: backend.delete_password(s, u))
    from msts_trader.brokers import paper
    from msts_trader import runstate, fill_log
    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "paper.json")
    monkeypatch.setattr(runstate, "STATE_PATH", tmp_path / "runstate.json")
    monkeypatch.setattr(fill_log, "LOG_DIR", tmp_path / "fills")
    return tmp_path


def _seed_quotes():
    from msts_trader.brokers.paper import Paper
    p = Paper(starting_cash="50000")
    p.set_quote("SPY", Decimal("500"))
    p.set_quote("SHV", Decimal("110"))


def test_full_paper_rebalance_executes_and_fills(tmp_path):
    runner = CliRunner()
    assert runner.invoke(main, ["login", "--broker", "paper"], input="50000\n").exit_code == 0
    _seed_quotes()

    csv = tmp_path / "t.csv"
    csv.write_text("ticker,weight\nSPY,0.6\nSHV,0.4\n")

    # Execute for real on paper (no prompt: --yes).
    r = runner.invoke(main, ["--broker", "paper", "rebalance", "--csv-file", str(csv), "--yes"])
    assert r.exit_code == 0, r.output
    assert "Done." in r.output

    # Positions actually changed.
    out = runner.invoke(main, ["--broker", "paper", "status", "--json"])
    payload = _json.loads(out.output.strip().splitlines()[-1])
    held = {p["ticker"]: Decimal(p["quantity"]) for p in payload["positions"]}
    assert held.get("SPY") == Decimal("60")        # 0.6*50000/500
    assert held.get("SHV") == Decimal("181.81")     # 0.4*50000/110, rounded down

    # Re-running the same targets the same day is a no-op (idempotency).
    r2 = runner.invoke(main, ["--broker", "paper", "rebalance", "--csv-file", str(csv), "--yes"])
    assert r2.exit_code == 0
    assert "within drift" in r2.output.lower() or "nothing to do" in r2.output.lower() or "duplicate" in r2.output.lower()


def test_full_paper_rebalance_limit_chase_executes_and_fills(tmp_path):
    """The next-best test: drive the full CLI entrypoint with
    --order-type limit-chase so the rebalance routes through chase_fill (not
    place_market) end-to-end, and confirm the positions actually fill."""
    runner = CliRunner()
    assert runner.invoke(main, ["login", "--broker", "paper"], input="50000\n").exit_code == 0
    _seed_quotes()

    csv = tmp_path / "t.csv"
    csv.write_text("ticker,weight\nSPY,0.6\nSHV,0.4\n")

    r = runner.invoke(main, [
        "--broker", "paper", "rebalance", "--csv-file", str(csv), "--yes",
        "--order-type", "limit-chase", "--chase-interval", "0.01", "--chase-poll", "0.01",
    ])
    assert r.exit_code == 0, r.output
    assert "Done." in r.output
    assert "CHASE" in r.output  # routed through the chase engine, not plain market

    out = runner.invoke(main, ["--broker", "paper", "status", "--json"])
    payload = _json.loads(out.output.strip().splitlines()[-1])
    held = {p["ticker"]: Decimal(p["quantity"]) for p in payload["positions"]}
    assert held.get("SPY") == Decimal("60")         # 0.6*50000/500, filled at the mid
    assert held.get("SHV") == Decimal("181.81")      # 0.4*50000/110, rounded down


def test_full_paper_rebalance_json_execute(tmp_path):
    runner = CliRunner()
    runner.invoke(main, ["login", "--broker", "paper"], input="50000\n")
    _seed_quotes()
    csv = tmp_path / "t.csv"
    csv.write_text("ticker,weight\nSPY,1.0\n")
    r = runner.invoke(main, ["--broker", "paper", "rebalance", "--csv-file", str(csv), "--yes", "--json"])
    assert r.exit_code == 0, r.output
    lines = [ln for ln in r.output.strip().splitlines() if ln.startswith("{")]
    executed = _json.loads(lines[-1])
    assert executed.get("executed") is True
    assert executed.get("sent", 0) >= 1 and executed.get("failed", 1) == 0
