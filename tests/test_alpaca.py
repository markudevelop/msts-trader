"""Alpaca adapter parsing — mock the SDK clients (no network)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from msts_trader.brokers.alpaca import Alpaca


def _broker(account=None, positions=None, quotes=None):
    b = Alpaca.__new__(Alpaca)
    b._paper = True
    b.account_id = "PA1"
    b._client = SimpleNamespace(
        get_account=lambda: account,
        get_all_positions=lambda: positions or [],
    )
    b._data = SimpleNamespace(get_stock_latest_quote=lambda req: quotes or {})
    return b


def test_balances():
    acct = SimpleNamespace(equity="100000", cash="40000", buying_power="200000")
    b = _broker(account=acct)
    bal = b.balances()
    assert bal.nav == Decimal("100000")
    assert bal.cash == Decimal("40000")
    assert bal.buying_power == Decimal("200000")


def test_positions_skips_zero_qty():
    pos = [
        SimpleNamespace(symbol="SPY", qty="10", current_price="500"),
        SimpleNamespace(symbol="GLD", qty="0", current_price="200"),  # skipped
    ]
    out = _broker(positions=pos).positions()
    assert set(out) == {"SPY"}
    assert out["SPY"].quantity == Decimal("10")
    assert out["SPY"].price == Decimal("500")


def test_quote_midpoint_and_fallbacks():
    quotes = {
        "SPY": SimpleNamespace(ask_price=500.0, bid_price=499.0),   # midpoint 499.5
        "QQQ": SimpleNamespace(ask_price=400.0, bid_price=0),       # ask only
        "IWM": SimpleNamespace(ask_price=0, bid_price=200.0),       # bid only
        "DEAD": SimpleNamespace(ask_price=0, bid_price=0),          # dropped
    }
    out = _broker(quotes=quotes).quote(["SPY", "QQQ", "IWM", "DEAD"])
    assert out["SPY"] == Decimal("499.5")
    assert out["QQQ"] == Decimal("400.0")
    assert out["IWM"] == Decimal("200.0")
    assert "DEAD" not in out


def test_quote_empty_symbols():
    assert _broker().quote([]) == {}


def test_quote_swallows_data_errors():
    b = Alpaca.__new__(Alpaca)
    b._data = SimpleNamespace(get_stock_latest_quote=lambda req: (_ for _ in ()).throw(RuntimeError("boom")))
    assert b.quote(["SPY"]) == {}


# ----- place_market -----

def test_place_market_dry_run():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D
    b = _broker()
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10")), dry_run=True)
    assert r["status"] == "dry-run" and r["dry_run"] is True


def test_place_market_submits_and_reads_resp():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D
    captured = {}
    b = Alpaca.__new__(Alpaca)
    b._client = SimpleNamespace(submit_order=lambda req: captured.update(req=req) or SimpleNamespace(status="accepted", id="abc-1"))
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10")), dry_run=False)
    assert r["status"] == "accepted" and r["order_id"] == "abc-1" and r["dry_run"] is False


def test_place_market_error():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D
    b = Alpaca.__new__(Alpaca)
    b._client = SimpleNamespace(submit_order=lambda req: (_ for _ in ()).throw(RuntimeError("rejected: insufficient bp")))
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10")))
    assert r["status"] == "error" and "insufficient" in r["reason"]


def test_place_market_zero_qty():
    from msts_trader.models import Order, Side
    from decimal import Decimal as D
    r = _broker().place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("0")))
    assert r["status"] == "skipped"


def test_place_market_missing_order_id_is_none():
    # resp.id of None must come back as None, not the string "None".
    from decimal import Decimal as D

    from msts_trader.models import Order, Side
    b = Alpaca.__new__(Alpaca)
    b._client = SimpleNamespace(submit_order=lambda req: SimpleNamespace(status="accepted", id=None))
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10")))
    assert r["order_id"] is None
