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


# ----- place_market -----

class _OrderResp:
    def __init__(self, location=None):
        self.headers = {"Location": location} if location else {}

    def raise_for_status(self):
        return None


def test_place_market_submits_and_reads_location(monkeypatch):
    from msts_trader.models import Order, Side
    from decimal import Decimal as D
    b = _broker()
    b._client = SimpleNamespace(place_order=lambda h, spec: _OrderResp("https://api.schwab.com/v1/accounts/HASH/orders/12345"))
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10")))
    assert r["status"] == "submitted" and r["order_id"] == "12345" and r["quantity"] == 10


def test_place_market_dry_run():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D
    r = _broker().place_market(Order(ticker="SPY", side=Side.SELL, quantity=D("5")), dry_run=True)
    assert r["status"] == "dry-run" and r["dry_run"] is True


def test_place_market_fractional_skipped():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D
    # 0.4 shares -> int() -> 0 -> Schwab whole-share skip
    r = _broker().place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("0.4")))
    assert r["status"] == "skipped" and "whole shares" in r["reason"]


def test_place_market_moc_sets_order_type(monkeypatch):
    # order.moc must produce a MARKET_ON_CLOSE order spec.
    from decimal import Decimal as D

    from msts_trader.models import Order, Side
    captured = {}
    b = _broker()
    b._client = SimpleNamespace(
        place_order=lambda h, spec: captured.update(spec=spec) or _OrderResp("https://api.schwab.com/v1/accounts/HASH/orders/777")
    )
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10"), moc=True))
    assert r["status"] == "submitted" and r["moc"] is True
    assert captured["spec"].get("orderType") == "MARKET_ON_CLOSE"


def test_place_market_without_moc_stays_market():
    from decimal import Decimal as D

    from msts_trader.models import Order, Side
    captured = {}
    b = _broker()
    b._client = SimpleNamespace(
        place_order=lambda h, spec: captured.update(spec=spec) or _OrderResp("https://api.schwab.com/v1/accounts/HASH/orders/778")
    )
    b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10")))
    assert captured["spec"].get("orderType") == "MARKET"


def test_place_market_error():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D
    b = _broker()
    b._client = SimpleNamespace(place_order=lambda h, spec: (_ for _ in ()).throw(RuntimeError("401 unauthorized")))
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10")))
    assert r["status"] == "error" and "401" in r["reason"]


def test_balances_zero_values_do_not_fall_through():
    # A legitimate 0 (account in liquidation, exhausted BP) must not fall
    # through to the secondary field.
    acct = {"securitiesAccount": {"currentBalances": {
        "liquidationValue": 0, "equity": 50000, "cashBalance": 0,
        "buyingPower": 0, "dayTradingBuyingPower": 99999,
    }}}
    bal = _broker(account=acct).balances()
    assert bal.nav == Decimal("0")
    assert bal.cash == Decimal("0")
    assert bal.buying_power == Decimal("0")
