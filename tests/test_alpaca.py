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


def test_place_market_moc_uses_cls_tif_and_whole_shares():
    # MOC routes as TimeInForce.CLS, and Alpaca only takes whole shares there.
    from decimal import Decimal as D

    from alpaca.trading.enums import TimeInForce

    from msts_trader.models import Order, Side
    captured = {}
    b = Alpaca.__new__(Alpaca)
    b._client = SimpleNamespace(submit_order=lambda req: captured.update(req=req) or SimpleNamespace(status="accepted", id="m-1"))
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10.7"), moc=True))
    assert captured["req"].time_in_force == TimeInForce.CLS
    assert r["quantity"] == 10.0 and r["moc"] is True


def test_place_market_moc_sub_share_skips():
    from decimal import Decimal as D

    from msts_trader.models import Order, Side
    r = _broker().place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("0.6"), moc=True))
    assert r["status"] == "skipped" and "whole shares" in r["reason"]


def test_place_market_day_tif_without_moc():
    from decimal import Decimal as D

    from alpaca.trading.enums import TimeInForce

    from msts_trader.models import Order, Side
    captured = {}
    b = Alpaca.__new__(Alpaca)
    b._client = SimpleNamespace(submit_order=lambda req: captured.update(req=req) or SimpleNamespace(status="accepted", id="d-1"))
    b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10.7")))
    assert captured["req"].time_in_force == TimeInForce.DAY
    assert captured["req"].qty == 10.7  # fractional preserved on regular market orders


def test_place_market_missing_order_id_is_none():
    # resp.id of None must come back as None, not the string "None".
    from decimal import Decimal as D

    from msts_trader.models import Order, Side
    b = Alpaca.__new__(Alpaca)
    b._client = SimpleNamespace(submit_order=lambda req: SimpleNamespace(status="accepted", id=None))
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=D("10")))
    assert r["order_id"] is None


# ----- protective stops (regression: keep these methods bound + correct) -----

def test_place_stop_submits_gtc_sell_stop():
    from alpaca.trading.enums import OrderSide, TimeInForce
    captured = {}
    b = _broker()
    b._client = SimpleNamespace(
        submit_order=lambda req: captured.update(req=req) or SimpleNamespace(status="accepted", id="ord-1"))
    r = b.place_stop("SPY", Decimal("10.9"), Decimal("480.25"))
    assert r["status"] == "accepted" and r["order_id"] == "ord-1" and r["quantity"] == 10.0
    req = captured["req"]
    assert req.symbol == "SPY" and int(req.qty) == 10
    assert req.side == OrderSide.SELL and req.time_in_force == TimeInForce.GTC
    assert req.stop_price == 480.25


def test_place_stop_sub_share_skips():
    r = _broker().place_stop("SPY", Decimal("0.4"), Decimal("480"))
    assert r["status"] == "skipped" and "whole-share" in r["reason"]


def test_place_stop_error_wrapped():
    b = _broker()
    b._client = SimpleNamespace(
        submit_order=lambda req: (_ for _ in ()).throw(RuntimeError("422 potential wash trade")))
    r = b.place_stop("SPY", Decimal("5"), Decimal("480"))
    assert r["status"] == "error" and "422" in r["reason"]


def test_open_stops_filters_to_stop_orders():
    orders = [
        SimpleNamespace(order_type="stop", symbol="SPY", id="o1", qty="10", stop_price="475"),
        SimpleNamespace(order_type="market", symbol="QQQ", id="o2", qty="5", stop_price=None),  # not a stop
    ]
    b = _broker()
    b._client = SimpleNamespace(get_orders=lambda req: orders)
    out = b.open_stops()
    assert set(out) == {"SPY"}
    assert out["SPY"][0]["order_id"] == "o1" and out["SPY"][0]["stop_price"] == Decimal("475")


def test_cancel_order_ok_and_error():
    b = _broker()
    b._client = SimpleNamespace(cancel_order_by_id=lambda oid: None)
    assert b.cancel_order("o1")["status"] == "CANCELLED"
    b._client = SimpleNamespace(
        cancel_order_by_id=lambda oid: (_ for _ in ()).throw(RuntimeError("404 not found")))
    r = b.cancel_order("o1")
    assert r["status"] == "error" and "404" in r["reason"]
