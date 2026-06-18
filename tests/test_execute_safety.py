"""Order-execution safety: market orders must never be retried.

A market order is not idempotent — retrying after a transient error that
occurred *after* the broker accepted the order would double the fill.
This pins that place_market is called exactly once per order.
"""
from __future__ import annotations

from decimal import Decimal

from msts_trader import __main__ as m
from msts_trader.models import Order, Preview, Side


class _CountingBroker:
    name = "paper"
    account_id = "X"

    def __init__(self):
        self.calls = 0

    def place_market(self, order, dry_run=False):
        self.calls += 1
        # Simulate a transient-looking error AFTER "acceptance".
        raise Exception("request timed out")


def test_place_market_not_retried_on_transient_error(monkeypatch):
    monkeypatch.setattr(m, "_QUIET", True)
    broker = _CountingBroker()
    o = Order(ticker="SPY", side=Side.BUY, quantity=Decimal("1"), estimated_price=Decimal("500"), notional=Decimal("500"))
    preview = Preview(
        nav=Decimal("1000"), buying_power=Decimal("1000"), cash=Decimal("1000"),
        rows=[], orders=[o], warnings=[], blockers=[],
    )
    sent, failed, results = m._execute(broker, preview)
    assert broker.calls == 1  # exactly one submission attempt — no retry
    assert sent == 0 and failed == 1
    assert "timed out" in results[0]["reason"]


class _StopBroker:
    """Minimal stop-capable broker (broker-agnostic surface: supports_stops + open_stops +
    cancel_order + place_market + positions). Records the ORDER of broker actions."""
    name = "fake"
    account_id = "X"
    supports_stops = True

    def __init__(self):
        self.events = []
        self._stops = {"AAPL": [{"order_id": "s1", "quantity": Decimal("10"), "stop_price": Decimal("90")}]}

    def open_stops(self):
        return {k: list(v) for k, v in self._stops.items()}

    def cancel_order(self, order_id):
        self.events.append(("cancel", order_id))
        self._stops = {k: [s for s in v if s["order_id"] != order_id] for k, v in self._stops.items()}
        return {"status": "CANCELLED", "order_id": order_id}

    def place_market(self, order, dry_run=False):
        self.events.append((order.side.value, order.ticker))
        return {"status": "ok", "ticker": order.ticker, "order_id": "o1"}

    def positions(self):
        return {}


def test_pre_cancels_resting_stop_before_sell_broker_agnostic(monkeypatch):
    """The 2026-06-18 fix: a resting stop must be cancelled BEFORE the exit sell (brokers reject a
    sell of shares reserved by an open stop). This lives in generic _execute, so it must hold for
    ANY supports_stops broker — not just tastytrade."""
    monkeypatch.setattr(m, "_QUIET", True)
    broker = _StopBroker()
    o = Order(ticker="AAPL", side=Side.SELL, quantity=Decimal("10"), estimated_price=Decimal("100"), notional=Decimal("1000"))
    preview = Preview(
        nav=Decimal("1000"), buying_power=Decimal("1000"), cash=Decimal("1000"),
        rows=[], orders=[o], warnings=[], blockers=[],
    )
    m._execute(broker, preview)
    assert ("cancel", "s1") in broker.events, "resting stop was never cancelled"
    assert ("SELL", "AAPL") in broker.events, "sell never placed"
    # the cancel must come BEFORE the sell
    assert broker.events.index(("cancel", "s1")) < broker.events.index(("SELL", "AAPL"))
