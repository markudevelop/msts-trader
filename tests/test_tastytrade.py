"""Tastytrade adapter parsing — mock the SDK session/account (no network)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace


from msts_trader.brokers.tastytrade import Tastytrade
from msts_trader.models import Order, Side


def _broker(balances=None, positions=None, place_resp=None):
    b = Tastytrade.__new__(Tastytrade)
    b._sess = object()
    b.account_id = "5W"
    b._acct = SimpleNamespace(
        get_balances=lambda sess: balances,
        get_positions=lambda sess: positions or [],
        place_order=lambda sess, order, dry_run=False: place_resp,
    )
    return b


def test_balances():
    bal = _broker(balances=SimpleNamespace(
        net_liquidating_value="42000", cash_balance="-29000",
        equity_buying_power="6000", derivative_buying_power="3000",
    )).balances()
    assert bal.nav == Decimal("42000")
    assert bal.cash == Decimal("-29000")
    assert bal.buying_power == Decimal("6000")  # prefers equity BP


def test_positions_filters_non_equity_and_zero():
    positions = [
        SimpleNamespace(instrument_type="Equity", quantity="10", symbol="SPY", close_price="500", mark=None),
        SimpleNamespace(instrument_type="Future", quantity="1", symbol="/ES", close_price="5000", mark=None),  # skip
        SimpleNamespace(instrument_type="Equity", quantity="0", symbol="FLAT", close_price="1", mark=None),    # zero
    ]
    out = _broker(positions=positions).positions()
    assert set(out) == {"SPY"}
    assert out["SPY"].quantity == Decimal("10")
    assert out["SPY"].price == Decimal("500")


def test_quote(monkeypatch):
    import tastytrade.market_data as md

    rows = [
        SimpleNamespace(symbol="SPY", last="500.0", mark="500.1", mid="500.2", close="499"),
        SimpleNamespace(symbol="SHV", last=None, mark="110.0", mid=None, close=None),  # mark fallback
    ]
    monkeypatch.setattr(md, "get_market_data_by_type", lambda sess, equities=None: rows)
    out = _broker().quote(["SPY", "SHV"])
    assert out["SPY"] == Decimal("500.0")
    assert out["SHV"] == Decimal("110.0")


def test_quote_empty():
    assert _broker().quote([]) == {}


def test_place_market_routed():
    resp = SimpleNamespace(order=SimpleNamespace(id="9", status="Routed"))
    b = _broker(positions=[], place_resp=resp)
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10")))
    assert r["status"] == "Routed" and r["order_id"] == "9" and r["dry_run"] is False


def test_place_market_zero_qty():
    r = _broker(positions=[]).place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("0")))
    assert r["status"] == "skipped"


def test_place_market_error():
    b = Tastytrade.__new__(Tastytrade)
    b._sess = object()
    b.account_id = "5W"
    b._acct = SimpleNamespace(
        get_positions=lambda sess: [],
        place_order=lambda sess, order, dry_run=False: (_ for _ in ()).throw(RuntimeError("margin_check_failed")),
    )
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10")))
    assert r["status"] == "error" and "margin_check_failed" in r["reason"]


def test_place_market_fractional_fallback_reports_whole_qty():
    # When the fractional order is rejected and we resubmit whole shares,
    # the result must report the whole-share quantity actually sent.
    calls = []

    def place_order(sess, order, dry_run=False):
        calls.append(order)
        if len(calls) == 1:
            raise RuntimeError("preflight: fractional_trading_invalid_symbol")
        return SimpleNamespace(order=SimpleNamespace(id="7", status="Routed"))

    b = Tastytrade.__new__(Tastytrade)
    b._sess = object()
    b.account_id = "5W"
    b._acct = SimpleNamespace(get_positions=lambda sess: [], place_order=place_order)
    r = b.place_market(Order(ticker="VOO", side=Side.BUY, quantity=Decimal("10.5")))
    assert r["status"] == "Routed"
    assert r["quantity"] == 10.0
    assert calls[1].legs[0].quantity == Decimal("10")
