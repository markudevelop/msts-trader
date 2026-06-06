"""Hyperliquid adapter parsing — mock the Info client (no network)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from msts_trader.brokers.hyperliquid import Hyperliquid, _coin


def _broker(user_state=None, mids=None):
    b = Hyperliquid.__new__(Hyperliquid)
    b._address = "0xabc0000000000000000000000000000000000def"
    b.account_id = "0xabc…0def"
    b._meta = None
    b._info = SimpleNamespace(
        user_state=lambda addr: user_state or {},
        all_mids=lambda: mids or {},
    )
    return b


def test_balances():
    st = {"marginSummary": {"accountValue": "12345.67"}, "withdrawable": "1000.00"}
    bal = _broker(user_state=st).balances()
    assert bal.nav == Decimal("12345.67")
    assert bal.cash == Decimal("1000.00")
    assert bal.buying_power == Decimal("1000.00")


def test_positions_filters_flat_and_reads_szi():
    st = {"assetPositions": [
        {"position": {"coin": "BTC", "szi": "0.5", "entryPx": "60000"}},
        {"position": {"coin": "ETH", "szi": "0"}},     # flat -> skipped
        {"position": {"coin": "SOL", "szi": "-10", "entryPx": "150"}},  # short kept
    ]}
    out = _broker(user_state=st).positions()
    assert set(out) == {"BTC", "SOL"}
    assert out["BTC"].quantity == Decimal("0.5")
    assert out["BTC"].price == Decimal("60000")
    assert out["SOL"].quantity == Decimal("-10")


def test_quote_normalises_tickers():
    mids = {"BTC": "61000.5", "ETH": "3000"}
    out = _broker(mids=mids).quote(["BTC-USD", "eth", "DOGE"])
    assert out["BTC"] == Decimal("61000.5")
    assert out["ETH"] == Decimal("3000")
    assert "DOGE" not in out  # no mid for it


def test_quote_swallows_errors():
    b = Hyperliquid.__new__(Hyperliquid)
    b._info = SimpleNamespace(all_mids=lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert b.quote(["BTC"]) == {}


def test_coin_normalisation():
    assert _coin("BTC-USD") == "BTC"
    assert _coin("eth/usdc") == "ETH"
    assert _coin("SOL") == "SOL"


# ----- place_market -----

def _exec_broker(market_open):
    b = Hyperliquid.__new__(Hyperliquid)
    b._meta = {"BTC": {"name": "BTC", "szDecimals": 3}}  # avoid meta() network call
    b._exchange = SimpleNamespace(market_open=market_open)
    return b


def test_place_market_filled():
    from msts_trader.models import Order, Side
    resp = {"response": {"data": {"statuses": [{"filled": {"oid": 777, "totalSz": "0.5"}}]}}}
    b = _exec_broker(lambda coin, is_buy, sz: resp)
    r = b.place_market(Order(ticker="BTC-USD", side=Side.BUY, quantity=Decimal("0.5")))
    assert r["status"] == "FILLED" and r["order_id"] == "777" and r["ticker"] == "BTC"


def test_place_market_resting():
    from msts_trader.models import Order, Side
    resp = {"response": {"data": {"statuses": [{"resting": {"oid": 888}}]}}}
    r = _exec_broker(lambda c, b, s: resp).place_market(Order(ticker="BTC", side=Side.BUY, quantity=Decimal("0.5")))
    assert r["status"] == "resting" and r["order_id"] == "888"


def test_place_market_error_status():
    from msts_trader.models import Order, Side
    resp = {"response": {"data": {"statuses": [{"error": "insufficient margin"}]}}}
    r = _exec_broker(lambda c, b, s: resp).place_market(Order(ticker="BTC", side=Side.BUY, quantity=Decimal("0.5")))
    assert r["status"] == "error" and "insufficient" in r["reason"]


def test_place_market_dry_run():
    from msts_trader.models import Order, Side
    r = _exec_broker(lambda c, b, s: {}).place_market(Order(ticker="BTC", side=Side.BUY, quantity=Decimal("0.5")), dry_run=True)
    assert r["status"] == "dry-run" and r["dry_run"] is True


def test_place_market_exchange_exception():
    from msts_trader.models import Order, Side
    b = _exec_broker(lambda c, bb, s: (_ for _ in ()).throw(RuntimeError("ws down")))
    r = b.place_market(Order(ticker="BTC", side=Side.BUY, quantity=Decimal("0.5")))
    assert r["status"] == "error" and "ws down" in r["reason"]
