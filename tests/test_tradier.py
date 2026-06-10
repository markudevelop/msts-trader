"""Tradier adapter parsing tests — mock the HTTP layer (no network).

Exercises the response-shape handling (single-object vs list, "null"
positions, buying-power fallbacks) that is the likeliest place for a bug.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from msts_trader.brokers.base import BrokerError
from msts_trader.brokers.tradier import Tradier
from msts_trader.models import Order, Side


def _broker(monkeypatch, routes):
    """Build a Tradier without network; `routes` maps path -> response dict."""
    b = Tradier.__new__(Tradier)
    b._token = "x"
    b._base = "https://sandbox.tradier.com"
    b._timeout = 5.0
    b.account_id = "VA123"

    def fake_request(method, path, params=None):
        key = (method, path)
        if key in routes:
            val = routes[key]
            return val(params) if callable(val) else val
        # match by path only
        if path in routes:
            return routes[path]
        raise AssertionError(f"unexpected request {key}")

    monkeypatch.setattr(b, "_request", fake_request)
    return b


def test_balances(monkeypatch):
    b = _broker(monkeypatch, {
        "/v1/accounts/VA123/balances": {"balances": {
            "total_equity": 17000, "total_cash": 5000,
            "margin": {"stock_buying_power": 34000},
        }},
    })
    bal = b.balances()
    assert bal.nav == Decimal("17000")
    assert bal.cash == Decimal("5000")
    assert bal.buying_power == Decimal("34000")


def test_balances_cash_account_bp_fallback(monkeypatch):
    b = _broker(monkeypatch, {
        "/v1/accounts/VA123/balances": {"balances": {
            "total_equity": 10000, "total_cash": 8000,
            "cash": {"cash_available": 8000},
        }},
    })
    assert b.balances().buying_power == Decimal("8000")


def test_positions_list(monkeypatch):
    b = _broker(monkeypatch, {
        "/v1/accounts/VA123/positions": {"positions": {"position": [
            {"symbol": "SPY", "quantity": 10, "cost_basis": 5000},
            {"symbol": "QQQ", "quantity": 5, "cost_basis": 2000},
        ]}},
    })
    pos = b.positions()
    assert set(pos) == {"SPY", "QQQ"}
    assert pos["SPY"].quantity == Decimal("10")
    assert pos["SPY"].price == Decimal("500")  # cost_basis / qty


def test_positions_single_object(monkeypatch):
    # Tradier returns a bare object (not a list) when there's exactly one.
    b = _broker(monkeypatch, {
        "/v1/accounts/VA123/positions": {"positions": {"position": {"symbol": "SPY", "quantity": 3, "cost_basis": 1500}}},
    })
    pos = b.positions()
    assert list(pos) == ["SPY"]
    assert pos["SPY"].quantity == Decimal("3")


def test_positions_null(monkeypatch):
    b = _broker(monkeypatch, {"/v1/accounts/VA123/positions": {"positions": "null"}})
    assert b.positions() == {}


def test_quote_list_and_single(monkeypatch):
    b = _broker(monkeypatch, {
        "/v1/markets/quotes": {"quotes": {"quote": [
            {"symbol": "SPY", "last": 500.1},
            {"symbol": "QQQ", "last": 0},  # zero -> skipped
        ]}},
    })
    out = b.quote(["SPY", "QQQ"])
    assert out == {"SPY": Decimal("500.1")}


def test_quote_single_object(monkeypatch):
    b = _broker(monkeypatch, {
        "/v1/markets/quotes": {"quotes": {"quote": {"symbol": "SPY", "last": 499}}},
    })
    assert b.quote(["SPY"]) == {"SPY": Decimal("499")}


def test_place_market_dry_run_uses_preview(monkeypatch):
    captured = {}

    def orders(params):
        captured.update(params)
        return {"order": {"status": "ok", "cost": 5000}}

    b = _broker(monkeypatch, {("POST", "/v1/accounts/VA123/orders"): orders})
    o = Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10"))
    r = b.place_market(o, dry_run=True)
    assert r["status"] == "dry-run"
    assert captured["preview"] == "true"
    assert captured["class"] == "equity" and captured["type"] == "market"


def test_place_market_live(monkeypatch):
    b = _broker(monkeypatch, {("POST", "/v1/accounts/VA123/orders"): {"order": {"id": 987, "status": "ok"}}})
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10")), dry_run=False)
    assert r["status"] == "ok"
    assert r["order_id"] == "987"


def test_place_market_rejected(monkeypatch):
    b = _broker(monkeypatch, {("POST", "/v1/accounts/VA123/orders"): {"order": {"status": "rejected", "reason": "no funds"}}})
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10")), dry_run=False)
    assert r["status"] == "error"


def test_place_market_zero_qty_skipped(monkeypatch):
    b = _broker(monkeypatch, {})
    r = b.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("0.4")), dry_run=False)
    assert r["status"] == "skipped"


def test_margin_requirement_sums_margin_change(monkeypatch):
    calls = []

    def orders(params):
        calls.append(params["symbol"])
        return {"order": {"status": "ok", "margin_change": 750.0, "cost": 1500.0}}

    b = _broker(monkeypatch, {("POST", "/v1/accounts/VA123/orders"): orders})
    buys = [
        Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10")),
        Order(ticker="QQQ", side=Side.BUY, quantity=Decimal("5")),
        Order(ticker="GLD", side=Side.SELL, quantity=Decimal("3")),  # sell ignored
    ]
    total = b.margin_requirement(buys)
    assert total == Decimal("1500.0")  # 750 + 750, sell skipped
    assert calls == ["SPY", "QQQ"]


def test_margin_requirement_falls_back_to_cost(monkeypatch):
    b = _broker(monkeypatch, {("POST", "/v1/accounts/VA123/orders"): {"order": {"cost": 2000.0}}})
    total = b.margin_requirement([Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10"))])
    assert total == Decimal("2000.0")


def test_margin_requirement_none_on_error(monkeypatch):
    def boom(params):
        raise RuntimeError("preview failed")

    b = _broker(monkeypatch, {("POST", "/v1/accounts/VA123/orders"): boom})
    assert b.margin_requirement([Order(ticker="SPY", side=Side.BUY, quantity=Decimal("1"))]) is None


def test_requires_token():
    with pytest.raises(BrokerError, match="access_token required"):
        Tradier(access_token="")


def test_balances_zero_margin_bp_is_honored(monkeypatch):
    # stock_buying_power of 0 (maxed-out margin account) must be reported
    # as 0, not fall through to cash_available / total_cash as phantom BP.
    b = _broker(monkeypatch, {
        "/v1/accounts/VA123/balances": {"balances": {
            "total_equity": 10000, "total_cash": 5000,
            "margin": {"stock_buying_power": 0},
            "cash": {"cash_available": 5000},
        }},
    })
    assert b.balances().buying_power == Decimal("0")
