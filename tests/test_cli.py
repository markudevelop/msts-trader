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


def test_rebalance_moc_refused_on_unsupported_broker(tmp_path, monkeypatch):
    # tastytrade has no MOC order type — the CLI must refuse, not downgrade.
    import msts_trader.__main__ as cli

    class _NoMoc:
        name = "tastytrade"
        account_id = "5W"
        supports_moc = False

    monkeypatch.setattr(cli, "_load_broker", lambda name: _NoMoc())
    monkeypatch.setattr(
        cli, "market_status", lambda: type("MS", (), {"status": "open", "next_open": None, "minutes_to_close": 120})()
    )
    r = CliRunner().invoke(
        main, ["--broker", "tastytrade", "rebalance", "--moc", "--dry-run"], input="ticker,weight\nSPY,1.0\n"
    )
    assert r.exit_code != 0
    assert "market-on-close" in r.output.lower()


def test_rebalance_moc_accepted_on_paper(tmp_path, monkeypatch):
    from decimal import Decimal

    from msts_trader.brokers import paper

    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "paper_state.json")
    p = paper.Paper(starting_cash="50000")
    p.set_quote("SPY", Decimal("500"))
    runner = CliRunner()
    runner.invoke(main, ["login", "--broker", "paper"], input="50000\n")
    r = runner.invoke(main, ["--broker", "paper", "rebalance", "--moc", "--dry-run"], input="ticker,weight\nSPY,1.0\n")
    assert r.exit_code == 0, r.output


def test_login_schwab_reauth_clears_cached_token(tmp_path, monkeypatch):
    # --reauth must delete the cached token file so the browser flow re-runs
    # (the weekend refresh-token reset).
    import msts_trader.__main__ as cli
    import msts_trader.brokers.schwab as schwab_mod

    tok = tmp_path / "schwab_token.json"
    tok.write_text("{}")
    monkeypatch.setattr(schwab_mod, "TOKEN_PATH", tok)
    monkeypatch.setitem(cli._LOGIN_FLOWS, "schwab", lambda: None)  # skip the real OAuth flow
    r = CliRunner().invoke(main, ["login", "--broker", "schwab", "--reauth"])
    assert r.exit_code == 0, r.output
    assert "cleared cached schwab token" in r.output.lower()
    assert not tok.exists()


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
        f"threshold = 0.04\n"
        f'[[account]]\nname = "a"\nbroker = "paper"\ncreds_file = "{a.as_posix()}"\n'
        f'[[account]]\nname = "b"\nbroker = "paper"\ncreds_file = "{b.as_posix()}"\n'
    )
    csv = tmp_path / "t.csv"
    csv.write_text("ticker,weight\nSPY,0.6\nSHV,0.4\n")

    r = CliRunner().invoke(main, ["multi", "--config", str(cfg), "--csv-file", str(csv), "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.output.strip().splitlines()[-1])
    assert len(payload["accounts"]) == 2
    assert all(acct["status"] == "dry-run" for acct in payload["accounts"])
    assert {acct["name"] for acct in payload["accounts"]} == {"a", "b"}


def test_help_survives_legacy_console_encoding():
    # Windows consoles often run a legacy code page (cp1252/cp437) that can't
    # encode the arrows/check marks in our help text; `msts-trader --help`
    # must degrade instead of dying with UnicodeEncodeError.
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    r = subprocess.run(
        [sys.executable, "-m", "msts_trader", "--help"],
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    assert b"Usage:" in r.stdout


def test_multi_no_accounts_errors(tmp_path):
    cfg = tmp_path / "multi.toml"
    cfg.write_text("threshold = 0.04\n")  # no [[account]] tables
    csv = tmp_path / "t.csv"
    csv.write_text("ticker,weight\nSPY,1.0\n")
    r = CliRunner().invoke(main, ["multi", "--config", str(cfg), "--csv-file", str(csv), "--dry-run"])
    assert r.exit_code != 0
    # brackets must survive (rich-escaped), not be eaten to "no [] entries"
    import re as _re

    _clean = _re.sub(r"\x1b\[[0-9;]*m", "", r.output)  # strip ANSI color (env-dependent in CliRunner)
    assert "[[account]]" in _clean


def test_multi_no_csv_source_errors(tmp_path):
    cfg = tmp_path / "multi.toml"
    cfg.write_text('[[account]]\nname = "a"\nbroker = "paper"\n')
    r = CliRunner().invoke(main, ["multi", "--config", str(cfg), "--dry-run"])
    assert r.exit_code != 0
    assert "csv" in r.output.lower()


def test_multi_requires_yes_to_execute(tmp_path):
    cfg = tmp_path / "multi.toml"
    cfg.write_text('[[account]]\nname = "a"\nbroker = "paper"\n')
    csv = tmp_path / "t.csv"
    csv.write_text("ticker,weight\nSPY,1.0\n")
    r = CliRunner().invoke(main, ["multi", "--config", str(cfg), "--csv-file", str(csv)])
    assert r.exit_code != 0
    assert "without --yes" in r.output.lower()


def test_login_creds_file_overrides_stale_env(tmp_path, monkeypatch):
    """An explicit --creds-file must win over a stale exported env var.

    Regression: a revoked TT_REFRESH_TOKEN left in the shell shadowed the
    fresh token in the creds file (load_into_env defaulted overwrite=False),
    so users got 'token revoked' even after fixing the file. Reproduced here
    with the paper broker, which loads PAPER_STARTING_CASH the same way.
    """
    import json as _json

    from msts_trader.brokers import paper

    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "paper_state.json")
    monkeypatch.setenv("PAPER_STARTING_CASH", "999")  # stale value in the shell
    creds = tmp_path / "creds.json"
    creds.write_text('{"PAPER_STARTING_CASH": "40000"}')

    runner = CliRunner()
    r = runner.invoke(main, ["login", "--broker", "paper", "--creds-file", str(creds)])
    assert r.exit_code == 0, r.output

    s = runner.invoke(main, ["--broker", "paper", "status", "--json"])
    assert s.exit_code == 0, s.output
    payload = _json.loads(s.output.strip().splitlines()[-1])
    assert payload["nav"] == "40000", "creds file must override stale env var"


def test_paper_login_existing_book_shows_hint(tmp_path, monkeypatch):
    from decimal import Decimal

    from msts_trader.brokers import paper
    from tests.conftest import write_paper_state

    state = tmp_path / "paper_state.json"
    monkeypatch.setattr(paper, "STATE_PATH", state)
    write_paper_state(state, cash="42000", positions={"SPY": "5"}, last_prices={"SPY": "500"})

    r = CliRunner().invoke(main, ["login", "--broker", "paper"], input="60000\n")
    assert r.exit_code == 0, r.output
    assert "existing book" in r.output.lower()
    assert "paper-reset" in r.output.lower()
    assert "60000" in r.output
    # Existing book must survive login — starting cash only seeds new files.
    assert paper.Paper().balances().cash == Decimal("42000")


def test_paper_reset_clears_book(tmp_path, monkeypatch):
    from msts_trader.brokers import paper

    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "paper_state.json")
    runner = CliRunner()
    runner.invoke(main, ["login", "--broker", "paper"], input="50000\n")
    r = runner.invoke(main, ["paper-reset"])
    assert r.exit_code == 0
    assert "paper book reset" in r.output.lower()


def test_paper_reset_uses_keychain_starting_cash(tmp_path, monkeypatch):
    from decimal import Decimal

    from msts_trader.brokers import paper
    from tests.conftest import write_paper_state

    state = tmp_path / "paper_state.json"
    monkeypatch.setattr(paper, "STATE_PATH", state)
    write_paper_state(state, cash="1000", positions={"SPY": "1"}, last_prices={"SPY": "500"})

    runner = CliRunner()
    runner.invoke(main, ["login", "--broker", "paper"], input="75000\n")
    r = runner.invoke(main, ["paper-reset"])
    assert r.exit_code == 0, r.output
    assert paper.Paper().balances().cash == Decimal("75000")
    assert paper.Paper().positions() == {}


def test_rebalance_whole_shares_rounds_quantities(tmp_path, monkeypatch):
    """--whole-shares must make every order quantity integral (the fix for
    IBKR error 10243 'fractional order cannot be placed via API')."""
    import json as _json
    from decimal import Decimal

    from msts_trader.brokers import paper

    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "paper_state.json")
    monkeypatch.setenv("PAPER_STARTING_CASH", "50000")  # satisfies headless creds resolution
    p = paper.Paper(starting_cash="50000")
    p.set_quote("SPY", Decimal("500"))
    p.set_quote("SHV", Decimal("110"))

    csv = tmp_path / "t.csv"
    csv.write_text("ticker,weight\nSPY,0.6\nSHV,0.4\n")  # SHV 20000/110 = 181.81 sh

    r = CliRunner().invoke(
        main,
        ["--broker", "paper", "rebalance", "--whole-shares", "--dry-run", "--json", "--csv-file", str(csv)],
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.output.strip().splitlines()[-1])
    qtys = {o["ticker"]: Decimal(o["quantity"]) for o in payload["orders"]}
    assert qtys["SHV"] == Decimal("181")  # 181.81 truncated to whole shares
    assert all(q == q.to_integral_value() for q in qtys.values())


def test_rebalance_without_whole_shares_allows_fraction(tmp_path, monkeypatch):
    import json as _json
    from decimal import Decimal

    from msts_trader.brokers import paper

    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "paper_state.json")
    monkeypatch.setenv("PAPER_STARTING_CASH", "50000")  # satisfies headless creds resolution
    p = paper.Paper(starting_cash="50000")
    p.set_quote("SPY", Decimal("500"))
    p.set_quote("SHV", Decimal("110"))

    csv = tmp_path / "t.csv"
    csv.write_text("ticker,weight\nSPY,0.6\nSHV,0.4\n")

    r = CliRunner().invoke(
        main,
        ["--broker", "paper", "rebalance", "--dry-run", "--json", "--csv-file", str(csv)],
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.output.strip().splitlines()[-1])
    qtys = {o["ticker"]: Decimal(o["quantity"]) for o in payload["orders"]}
    assert qtys["SHV"] == Decimal("181.81")  # fractional preserved by default


def test_rebalance_auto_whole_shares_for_nonfractional_broker(tmp_path, monkeypatch):
    """A broker with supports_fractional=False (e.g. Schwab/Tradier) must size
    the preview to whole shares automatically — it truncates at submit anyway,
    so the preview must match what's actually sent."""
    import json as _json
    from decimal import Decimal

    from msts_trader.brokers import paper

    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "paper_state.json")
    monkeypatch.setenv("PAPER_STARTING_CASH", "50000")
    p = paper.Paper(starting_cash="50000")
    p.set_quote("SPY", Decimal("500"))
    p.set_quote("SHV", Decimal("110"))
    # Flip the loaded paper broker to look like a whole-share-only broker.
    monkeypatch.setattr(paper.Paper, "supports_fractional", False, raising=False)

    csv = tmp_path / "t.csv"
    csv.write_text("ticker,weight\nSPY,0.6\nSHV,0.4\n")

    r = CliRunner().invoke(
        main,  # no --whole-shares flag — must auto-engage from the capability
        ["--broker", "paper", "rebalance", "--dry-run", "--json", "--csv-file", str(csv)],
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.output.strip().splitlines()[-1])
    qtys = {o["ticker"]: Decimal(o["quantity"]) for o in payload["orders"]}
    assert qtys["SHV"] == Decimal("181")  # auto-rounded despite no flag
    assert all(q == q.to_integral_value() for q in qtys.values())
