from __future__ import annotations

import pytest

from msts_trader.creds_file import broker_kwargs_from_env

TT = ("TT_PROVIDER_SECRET", "TT_REFRESH_TOKEN", "TT_ACCOUNT_ID")
ALP = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA_PAPER")
IB = ("IBKR_HOST", "IBKR_PORT", "IBKR_CLIENT_ID", "IBKR_ACCOUNT_ID")
SC = ("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_CALLBACK_URL")


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    for v in TT + ALP + IB + SC + ("PAPER_STARTING_CASH",):
        monkeypatch.delenv(v, raising=False)


def test_tastytrade_from_env(monkeypatch):
    monkeypatch.setenv("TT_PROVIDER_SECRET", "ps")
    monkeypatch.setenv("TT_REFRESH_TOKEN", "rt")
    monkeypatch.setenv("TT_ACCOUNT_ID", "acct")
    assert broker_kwargs_from_env("tastytrade") == {"provider_secret": "ps", "refresh_token": "rt", "account_id": "acct"}


def test_tastytrade_account_optional(monkeypatch):
    monkeypatch.setenv("TT_PROVIDER_SECRET", "ps")
    monkeypatch.setenv("TT_REFRESH_TOKEN", "rt")
    out = broker_kwargs_from_env("tastytrade")
    assert out["account_id"] is None


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
    assert out["app_key"] == "ak" and out["callback_url"] == "https://127.0.0.1:8182/"


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
