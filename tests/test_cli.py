"""CLI smoke tests via Click's CliRunner.

Doesn't hit any real broker — uses the paper broker which is self-contained.
"""
from __future__ import annotations

import keyring
import pytest
from click.testing import CliRunner
from keyring.backend import KeyringBackend

from msts_trader.__main__ import main


class _MemBackend(KeyringBackend):
    priority = 100

    def __init__(self):
        self._store = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


@pytest.fixture(autouse=True)
def mem_keyring(monkeypatch):
    backend = _MemBackend()
    monkeypatch.setattr(keyring, "get_password", lambda s, u: backend.get_password(s, u))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: backend.set_password(s, u, p))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: backend.delete_password(s, u))
    yield backend


def test_help_renders():
    r = CliRunner().invoke(main, ["--help"])
    assert r.exit_code == 0
    assert "msts-trader" in r.output.lower() or "rebalance" in r.output.lower()


def test_version_prints():
    r = CliRunner().invoke(main, ["--version"])
    assert r.exit_code == 0
    assert "0." in r.output  # any 0.x.y


def test_brokers_lists_supported():
    r = CliRunner().invoke(main, ["brokers"])
    assert r.exit_code == 0
    out = r.output.lower()
    for name in ("tastytrade", "alpaca", "ibkr", "schwab", "paper"):
        assert name in out


def test_rebalance_without_creds_exits_clean(monkeypatch):
    # Ensure no env-derived creds leak in from the host shell.
    for v in ("TT_PROVIDER_SECRET", "TT_REFRESH_TOKEN", "TT_ACCOUNT_ID"):
        monkeypatch.delenv(v, raising=False)
    r = CliRunner().invoke(main, ["--broker", "tastytrade", "rebalance", "--dry-run"], input="ticker,weight\nSPY,1.0\n")
    assert r.exit_code != 0
    assert "no credentials" in r.output.lower()


def test_paper_rebalance_dry_run_end_to_end(tmp_path, monkeypatch):
    """Login to paper, paste a CSV, see preview, no orders sent."""
    # paper broker doesn't need real network; isolate state via fixture chain
    from msts_trader.brokers import paper

    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "paper_state.json")

    runner = CliRunner()
    # 1) login
    r1 = runner.invoke(main, ["login", "--broker", "paper"], input="50000\n")
    assert r1.exit_code == 0, r1.output
    assert "paper book ready" in r1.output.lower()

    # 2) dry-run rebalance — paper broker has no quotes, so we just verify the flow doesn't crash on the "no positions, no quotes" path
    csv = "ticker,weight\nSPY,1.0\n"
    r2 = runner.invoke(main, ["--broker", "paper", "rebalance", "--dry-run"], input=csv)
    # paper has no quote for SPY -> warning, no orders, but flow completes
    assert r2.exit_code in (0,), r2.output


def test_status_json_paper(tmp_path, monkeypatch):
    import json as _json

    from msts_trader.brokers import paper

    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "paper_state.json")
    runner = CliRunner()
    runner.invoke(main, ["login", "--broker", "paper"], input="40000\n")
    r = runner.invoke(main, ["--broker", "paper", "status", "--json"])
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.output.strip().splitlines()[-1])
    assert payload["broker"] == "paper"
    assert payload["nav"] == "40000"
    assert payload["positions"] == []


def test_multi_dry_run_two_paper_accounts(tmp_path, monkeypatch):
    import json as _json
    from decimal import Decimal

    from msts_trader.brokers import paper

    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "paper_state.json")
    # seed quotes so the paper book can size orders
    p = paper.Paper(starting_cash="50000")
    p.set_quote("SPY", Decimal("500"))
    p.set_quote("SHV", Decimal("110"))

    a = tmp_path / "a.env"
    a.write_text("PAPER_STARTING_CASH=50000\n")
    b = tmp_path / "b.env"
    b.write_text("PAPER_STARTING_CASH=50000\n")
    cfg = tmp_path / "multi.toml"
    cfg.write_text(
        f'threshold = 0.04\n'
        f'[[account]]\nname = "a"\nbroker = "paper"\ncreds_file = "{a}"\n'
        f'[[account]]\nname = "b"\nbroker = "paper"\ncreds_file = "{b}"\n'
    )
    csv = tmp_path / "t.csv"
    csv.write_text("ticker,weight\nSPY,0.6\nSHV,0.4\n")

    r = CliRunner().invoke(main, ["multi", "--config", str(cfg), "--csv-file", str(csv), "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.output.strip().splitlines()[-1])
    assert len(payload["accounts"]) == 2
    assert all(acct["status"] == "dry-run" for acct in payload["accounts"])
    assert {acct["name"] for acct in payload["accounts"]} == {"a", "b"}


def test_multi_requires_yes_to_execute(tmp_path):
    cfg = tmp_path / "multi.toml"
    cfg.write_text('[[account]]\nname = "a"\nbroker = "paper"\n')
    csv = tmp_path / "t.csv"
    csv.write_text("ticker,weight\nSPY,1.0\n")
    r = CliRunner().invoke(main, ["multi", "--config", str(cfg), "--csv-file", str(csv)])
    assert r.exit_code != 0
    assert "without --yes" in r.output.lower()


def test_paper_reset_clears_book(tmp_path, monkeypatch):
    from msts_trader.brokers import paper

    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "paper_state.json")
    runner = CliRunner()
    runner.invoke(main, ["login", "--broker", "paper"], input="50000\n")
    r = runner.invoke(main, ["paper-reset"])
    assert r.exit_code == 0
    assert "paper book reset" in r.output.lower()
