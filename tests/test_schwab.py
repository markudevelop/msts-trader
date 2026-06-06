"""Schwab adapter parsing — mock the schwab-py client (no network)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from msts_trader.brokers.schwab import Schwab


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _broker(account=None, quotes=None):
    b = Schwab.__new__(Schwab)
    b._account_hash = "HASH"
    b.account_id = "HASH…"
    b._client = SimpleNamespace(
        get_account=lambda h, fields=None: _Resp(account or {}),
        get_quotes=lambda syms: _Resp(quotes or {}),
    )
    return b


def test_balances():
    acct = {"securitiesAccount": {"currentBalances": {
        "liquidationValue": 50000, "cashBalance": 10000, "buyingPower": 80000,
    }}}
    bal = _broker(account=acct).balances()
    assert bal.nav == Decimal("50000")
    assert bal.cash == Decimal("10000")
    assert bal.buying_power == Decimal("80000")


def test_positions_filters_non_equity_and_computes_price():
    acct = {"securitiesAccount": {"positions": [
        {"instrument": {"symbol": "SPY", "assetType": "ETF"}, "longQuantity": 10, "marketValue": 5000},
        {"instrument": {"symbol": "AAPL", "assetType": "EQUITY"}, "longQuantity": 5, "marketValue": 1000},
        {"instrument": {"symbol": "OPT", "assetType": "OPTION"}, "longQuantity": 1, "marketValue": 100},  # skipped
        {"instrument": {"symbol": "ZERO", "assetType": "EQUITY"}, "longQuantity": 0, "marketValue": 0},   # flat
    ]}}
    out = _broker(account=acct).positions()
    assert set(out) == {"SPY", "AAPL"}
    assert out["SPY"].quantity == Decimal("10")
    assert out["SPY"].price == Decimal("500")  # 5000 / 10


def test_positions_short_quantity():
    acct = {"securitiesAccount": {"positions": [
        {"instrument": {"symbol": "TSLA", "assetType": "EQUITY"}, "shortQuantity": 4, "marketValue": -1600},
    ]}}
    out = _broker(account=acct).positions()
    assert out["TSLA"].quantity == Decimal("-4")


def test_quote_price_priority():
    quotes = {
        "SPY": {"quote": {"lastPrice": 500.1}},
        "QQQ": {"quote": {"mark": 400.2, "closePrice": 399}},  # no last -> mark
        "IWM": {"quote": {"closePrice": 200.0}},               # only close
        "DEAD": {"quote": {"lastPrice": 0}},                   # zero -> dropped
    }
    out = _broker(quotes=quotes).quote(["SPY", "QQQ", "IWM", "DEAD"])
    assert out["SPY"] == Decimal("500.1")
    assert out["QQQ"] == Decimal("400.2")
    assert out["IWM"] == Decimal("200.0")
    assert "DEAD" not in out


def test_quote_empty():
    assert _broker().quote([]) == {}
