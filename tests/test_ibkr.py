"""IBKR read-path parsing — faked ib_insync socket (no live connection)."""
from __future__ import annotations

import pytest

pytest.importorskip("ib_insync")  # optional dep — skip when the SDK is not installed

from decimal import Decimal
from types import SimpleNamespace

from msts_trader.brokers.ibkr import IBKR


class _IB:
    def __init__(self, summary=None, positions=None, tickers=None):
        self._summary = summary or []
        self._positions = positions or []
        self._tickers = tickers or []

    def accountSummary(self, acct):
        return self._summary

    def positions(self, acct):
        return self._positions

    def qualifyContracts(self, ct):
        return None

    def reqMarketDataType(self, t):
        return None

    def reqTickers(self, *cts):
        return self._tickers


def _ibkr(**kw):
    b = IBKR.__new__(IBKR)
    b._ib = _IB(**kw)
    b.account_id = "U1"
    return b


def _row(tag, value):
    return SimpleNamespace(tag=tag, value=value)


def test_balances():
    b = _ibkr(summary=[
        _row("NetLiquidation", "100000"),
        _row("TotalCashValue", "25000"),
        _row("BuyingPower", "400000"),
        _row("Junk", "not-a-number"),  # skipped, no crash
    ])
    bal = b.balances()
    assert bal.nav == Decimal("100000")
    assert bal.cash == Decimal("25000")
    assert bal.buying_power == Decimal("400000")


def test_positions_filters_non_stk_and_zero():
    positions = [
        SimpleNamespace(contract=SimpleNamespace(secType="STK", symbol="SPY"), position=10, avgCost=500),
        SimpleNamespace(contract=SimpleNamespace(secType="OPT", symbol="SPY  240..."), position=1, avgCost=2),  # non-STK
        SimpleNamespace(contract=SimpleNamespace(secType="STK", symbol="FLAT"), position=0, avgCost=0),  # zero
        SimpleNamespace(contract=SimpleNamespace(secType="STK", symbol="TSLA"), position=-4, avgCost=395),  # short kept
    ]
    out = _ibkr(positions=positions).positions()
    assert set(out) == {"SPY", "TSLA"}
    assert out["SPY"].quantity == Decimal("10")
    assert out["SPY"].price == Decimal("500")
    assert out["TSLA"].quantity == Decimal("-4")


def _ticker(sym, **fields):
    ns = SimpleNamespace(contract=SimpleNamespace(symbol=sym), **fields)
    ns.marketPrice = lambda: fields.get("_mkt", float("nan"))
    return ns


def test_quote_prefers_last_then_close_then_mid():
    tickers = [
        _ticker("SPY", last=500.0, close=499.0, bid=499.0, ask=501.0),         # last wins
        _ticker("QQQ", last=float("nan"), close=400.0, bid=0, ask=0),          # close
        _ticker("IWM", last=float("nan"), close=float("nan"), bid=200.0, ask=202.0),  # midpoint 201
    ]
    out = _ibkr(tickers=tickers).quote(["SPY", "QQQ", "IWM"])
    assert out["SPY"] == Decimal("500.0")
    assert out["QQQ"] == Decimal("400.0")
    assert out["IWM"] == Decimal("201.0")


def test_quote_empty_symbols():
    assert _ibkr().quote([]) == {}


def test_quote_reqtickers_failure_returns_empty():
    b = _ibkr()
    b._ib.reqTickers = lambda *a: (_ for _ in ()).throw(RuntimeError("no data farm"))
    assert b.quote(["SPY"]) == {}


def test_place_market_moc_builds_moc_order():
    # order.moc must reach IBKR as orderType MOC with whole shares.
    from msts_trader.models import Order, Side

    captured = {}
    b = _ibkr()
    b._ib.placeOrder = lambda ct, o: captured.update(order=o) or SimpleNamespace(
        orderStatus=SimpleNamespace(status="Submitted"),
        order=SimpleNamespace(permId="77", orderId=1),
        log=[],
    )
    b._ib.sleep = lambda s: None
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10.7"), moc=True))
    assert captured["order"].orderType == "MOC"
    assert captured["order"].totalQuantity == 10.0  # whole shares for the closing auction
    assert r["moc"] is True and r["status"] == "Submitted"


def test_place_market_moc_sub_share_skips():
    from msts_trader.models import Order, Side

    r = _ibkr().place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("0.4"), moc=True))
    assert r["status"] == "skipped" and "whole shares" in r["reason"]


def test_place_market_without_moc_stays_mkt():
    from msts_trader.models import Order, Side

    captured = {}
    b = _ibkr()
    b._ib.placeOrder = lambda ct, o: captured.update(order=o) or SimpleNamespace(
        orderStatus=SimpleNamespace(status="Submitted"),
        order=SimpleNamespace(permId="78", orderId=2),
        log=[],
    )
    b._ib.sleep = lambda s: None
    b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10.7")))
    assert captured["order"].orderType == "MKT"
    assert captured["order"].totalQuantity == 10.7  # fractional preserved


def test_stop_methods_are_bound_to_class():
    # Regression guard: a misindent once nested these inside a module-level
    # helper, so the IBKR class silently lost them and stop reconcile blew up
    # with "'IBKR' object has no attribute 'open_stops'". They must be real
    # instance methods, not module functions.
    for name in ("place_stop", "open_stops", "cancel_order"):
        assert callable(getattr(IBKR, name, None)), f"IBKR.{name} is not a method"


def _stp_trade(symbol, order_id, qty, aux, order_type="STP"):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        order=SimpleNamespace(orderId=order_id, orderType=order_type, totalQuantity=qty, auxPrice=aux),
    )


def test_open_stops_filters_to_stop_orders():
    b = _ibkr()
    b._ib.openTrades = lambda: [
        _stp_trade("SPY", 11, 10, 480.0),
        _stp_trade("QQQ", 12, 5, 0, order_type="MKT"),  # not a stop — ignored
        _stp_trade("SPY", 13, 3, 475.0),
    ]
    out = b.open_stops()
    assert set(out) == {"SPY"}
    assert {s["order_id"] for s in out["SPY"]} == {"11", "13"}
    assert out["SPY"][0]["stop_price"] == Decimal("480.0")


def test_place_stop_submits_gtc_sell_and_rounds_to_whole_shares():
    captured = {}
    b = _ibkr()
    b._ib.placeOrder = lambda ct, o: captured.update(order=o) or SimpleNamespace(
        orderStatus=SimpleNamespace(status="Submitted"), order=SimpleNamespace(orderId=99),
    )
    b._ib.sleep = lambda s: None
    r = b.place_stop("SPY", Decimal("10.9"), Decimal("480"))
    assert captured["order"].action == "SELL" and captured["order"].tif == "GTC"
    assert captured["order"].totalQuantity == 10  # whole shares
    assert r["status"] == "Submitted" and r["order_id"] == "99"


def test_cancel_order_finds_and_cancels():
    cancelled = {}
    b = _ibkr()
    b._ib.openTrades = lambda: [_stp_trade("SPY", 42, 10, 480.0)]
    b._ib.cancelOrder = lambda o: cancelled.update(id=o.orderId)
    b._ib.sleep = lambda s: None
    r = b.cancel_order("42")
    assert cancelled["id"] == 42 and r["status"] == "CANCELLED"
    assert b.cancel_order("999")["status"] == "error"  # unknown id


def test_balances_zero_values_do_not_fall_through():
    # A legitimate 0 must not fall through to the fallback tag.
    b = _ibkr(summary=[
        _row("NetLiquidation", "0"),
        _row("NetLiquidationByCurrency", "100000"),
        _row("BuyingPower", "0"),
        _row("AvailableFunds", "50000"),
    ])
    bal = b.balances()
    assert bal.nav == Decimal("0")
    assert bal.buying_power == Decimal("0")


# ----- event-loop bootstrap (Python 3.12+ removed implicit loop creation) -----

def test_ensure_event_loop_creates_loop_when_unset():
    # set_event_loop(None) reproduces a fresh Python 3.14 main thread:
    # get_event_loop() raises "There is no current event loop ...".
    import asyncio

    from msts_trader.brokers.ibkr import _ensure_event_loop

    asyncio.set_event_loop(None)
    try:
        _ensure_event_loop()
        loop = asyncio.get_event_loop_policy().get_event_loop()
        assert loop is not None and not loop.is_closed()
    finally:
        asyncio.get_event_loop_policy().get_event_loop().close()
        asyncio.set_event_loop(None)


def test_ensure_event_loop_replaces_closed_loop():
    import asyncio

    from msts_trader.brokers.ibkr import _ensure_event_loop

    dead = asyncio.new_event_loop()
    asyncio.set_event_loop(dead)
    dead.close()
    try:
        _ensure_event_loop()
        loop = asyncio.get_event_loop_policy().get_event_loop()
        assert not loop.is_closed()
        assert loop is not dead
    finally:
        asyncio.get_event_loop_policy().get_event_loop().close()
        asyncio.set_event_loop(None)


def test_ensure_event_loop_keeps_existing_loop():
    import asyncio

    from msts_trader.brokers.ibkr import _ensure_event_loop

    existing = asyncio.new_event_loop()
    asyncio.set_event_loop(existing)
    try:
        _ensure_event_loop()
        assert asyncio.get_event_loop_policy().get_event_loop() is existing
    finally:
        existing.close()
        asyncio.set_event_loop(None)
