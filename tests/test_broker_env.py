from __future__ import annotations

import pytest

from msts_trader.creds_file import broker_kwargs_from_env, broker_kwargs_from_file

TT = ("TT_PROVIDER_SECRET", "TT_REFRESH_TOKEN", "TT_ACCOUNT_ID", "TT_TEST")
ALP = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA_PAPER")
IB = ("IBKR_HOST", "IBKR_PORT", "IBKR_CLIENT_ID", "IBKR_ACCOUNT_ID")
SC = ("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_CALLBACK_URL")
TR = ("TRADIER_ACCESS_TOKEN", "TRADIER_ACCOUNT_ID", "TRADIER_SANDBOX")


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    for v in TT + ALP + IB + SC + TR + ("PAPER_STARTING_CASH",):
        monkeypatch.delenv(v, raising=False)


def test_tastytrade_from_env(monkeypatch):
    monkeypatch.setenv("TT_PROVIDER_SECRET", "ps")
    monkeypatch.setenv("TT_REFRESH_TOKEN", "rt")
    monkeypatch.setenv("TT_ACCOUNT_ID", "acct")
    assert broker_kwargs_from_env("tastytrade") == {
        "provider_secret": "ps",
        "refresh_token": "rt",
        "account_id": "acct",
        "is_test": False,
    }


def test_tastytrade_account_optional(monkeypatch):
    monkeypatch.setenv("TT_PROVIDER_SECRET", "ps")
    monkeypatch.setenv("TT_REFRESH_TOKEN", "rt")
    out = broker_kwargs_from_env("tastytrade")
    assert out["account_id"] is None
    assert out["is_test"] is False  # production by default


def test_tastytrade_test_env(monkeypatch):
    monkeypatch.setenv("TT_PROVIDER_SECRET", "ps")
    monkeypatch.setenv("TT_REFRESH_TOKEN", "rt")
    monkeypatch.setenv("TT_TEST", "1")
    assert broker_kwargs_from_env("tastytrade")["is_test"] is True


def test_tastytrade_test_env_accepts_sandbox_word(monkeypatch):
    monkeypatch.setenv("TT_PROVIDER_SECRET", "ps")
    monkeypatch.setenv("TT_REFRESH_TOKEN", "rt")
    monkeypatch.setenv("TT_TEST", "sandbox")
    assert broker_kwargs_from_env("tastytrade")["is_test"] is True


def test_tastytrade_test_env_falsy_value(monkeypatch):
    monkeypatch.setenv("TT_PROVIDER_SECRET", "ps")
    monkeypatch.setenv("TT_REFRESH_TOKEN", "rt")
    monkeypatch.setenv("TT_TEST", "false")
    assert broker_kwargs_from_env("tastytrade")["is_test"] is False


def test_tastytrade_missing_returns_none(monkeypatch):
    monkeypatch.setenv("TT_PROVIDER_SECRET", "ps")  # no refresh token
    assert broker_kwargs_from_env("tastytrade") is None


def test_alpaca_paper_default_true(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    assert broker_kwargs_from_env("alpaca") == {"api_key": "k", "secret_key": "s", "paper": True}


def test_alpaca_paper_false(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    monkeypatch.setenv("APCA_PAPER", "false")
    assert broker_kwargs_from_env("alpaca")["paper"] is False


def test_ibkr_defaults(monkeypatch):
    monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
    out = broker_kwargs_from_env("ibkr")
    assert out == {"host": "127.0.0.1", "port": 4002, "client_id": 17, "account_id": None}


def test_ibkr_full(monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.setenv("IBKR_CLIENT_ID", "42")
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "U123")
    out = broker_kwargs_from_env("ibkr")
    assert out["port"] == 7497 and out["client_id"] == 42 and out["account_id"] == "U123"


def test_ibkr_absent_returns_none():
    assert broker_kwargs_from_env("ibkr") is None


def test_schwab_from_env(monkeypatch):
    monkeypatch.setenv("SCHWAB_APP_KEY", "ak")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "as")
    out = broker_kwargs_from_env("schwab")
    # No trailing slash: must mirror the registration value in the setup
    # instructions — Schwab requires an exact character-for-character match.
    assert out["app_key"] == "ak" and out["callback_url"] == "https://127.0.0.1:8182"


def test_schwab_callback_env_override(monkeypatch):
    monkeypatch.setenv("SCHWAB_APP_KEY", "ak")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "as")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:9999/")
    out = broker_kwargs_from_env("schwab")
    assert out["callback_url"] == "https://127.0.0.1:9999/"


def test_tradier_from_env(monkeypatch):
    monkeypatch.setenv("TRADIER_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("TRADIER_ACCOUNT_ID", "VA123")
    monkeypatch.setenv("TRADIER_SANDBOX", "1")
    out = broker_kwargs_from_env("tradier")
    assert out == {"access_token": "tok", "account_id": "VA123", "sandbox": True}


def test_tradier_sandbox_default_false_when_absent(monkeypatch):
    monkeypatch.setenv("TRADIER_ACCESS_TOKEN", "tok")
    assert broker_kwargs_from_env("tradier")["sandbox"] is False


def test_tradier_absent_returns_none():
    assert broker_kwargs_from_env("tradier") is None


def test_hyperliquid_from_env(monkeypatch):
    monkeypatch.setenv("HL_PRIVATE_KEY", "0xabc")
    monkeypatch.setenv("HL_TESTNET", "1")
    out = broker_kwargs_from_env("hyperliquid")
    assert out == {"private_key": "0xabc", "account_address": None, "testnet": True}


def test_hyperliquid_absent_returns_none():
    assert broker_kwargs_from_env("hyperliquid") is None


def test_paper_from_env(monkeypatch):
    monkeypatch.setenv("PAPER_STARTING_CASH", "25000")
    assert broker_kwargs_from_env("paper") == {"starting_cash": "25000"}


def test_paper_absent_returns_none():
    assert broker_kwargs_from_env("paper") is None


def test_strips_quotes(monkeypatch):
    monkeypatch.setenv("TT_PROVIDER_SECRET", '"ps"')
    monkeypatch.setenv("TT_REFRESH_TOKEN", "'rt'")
    out = broker_kwargs_from_env("tastytrade")
    assert out["provider_secret"] == "ps" and out["refresh_token"] == "rt"


def test_from_file_isolated(tmp_path, monkeypatch):
    # File values build the kwargs; no os.environ mutation, no cross-leak.
    for v in TT:
        monkeypatch.delenv(v, raising=False)
    f = tmp_path / "a.env"
    f.write_text("TT_PROVIDER_SECRET=ps1\nTT_REFRESH_TOKEN=rt1\nTT_ACCOUNT_ID=acc1\n")
    out = broker_kwargs_from_file("tastytrade", f)
    assert out == {"provider_secret": "ps1", "refresh_token": "rt1", "account_id": "acc1", "is_test": False}
    import os
    assert "TT_PROVIDER_SECRET" not in os.environ  # not leaked into the process env


def test_from_file_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_ACCOUNT_ID", "from-env")
    f = tmp_path / "a.env"
    f.write_text("TT_PROVIDER_SECRET=ps\nTT_REFRESH_TOKEN=rt\n")  # no account id in file
    out = broker_kwargs_from_file("tastytrade", f)
    assert out["account_id"] == "from-env"
