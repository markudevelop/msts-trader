"""IBKR read-path parsing — faked ib_insync socket (no live connection)."""
from __future__ import annotations

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
