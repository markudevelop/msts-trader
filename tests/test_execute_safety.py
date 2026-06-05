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
