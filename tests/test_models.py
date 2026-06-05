from __future__ import annotations

from decimal import Decimal

from msts_trader.models import (
    Order,
    Position,
    Preview,
    RebalanceRow,
    Side,
    Target,
)


def test_position_market_value():
    p = Position(ticker="SPY", quantity=Decimal("10"), price=Decimal("500"))
    assert p.market_value == Decimal("5000")


def test_side_enum_values():
    assert Side.BUY.value == "BUY"
    assert Side.SELL.value == "SELL"


def test_preview_has_blockers_flag():
    p = Preview(
        nav=Decimal("1000"),
        buying_power=Decimal("1000"),
        cash=Decimal("1000"),
        rows=[],
        orders=[],
        warnings=[],
        blockers=["something broke"],
    )
    assert p.has_blockers is True


def test_preview_no_blockers_when_empty():
    p = Preview(
        nav=Decimal("1000"),
        buying_power=Decimal("1000"),
        cash=Decimal("1000"),
        rows=[],
        orders=[],
        warnings=["mild"],
        blockers=[],
    )
    assert p.has_blockers is False


def test_target_holds_decimal_weight():
    t = Target(ticker="SPY", weight=Decimal("0.42"))
    assert t.weight == Decimal("0.42")
    assert t.ticker == "SPY"


def test_order_carries_notional():
    o = Order(
        ticker="SPY",
        side=Side.BUY,
        quantity=Decimal("10"),
        estimated_price=Decimal("500"),
        notional=Decimal("5000"),
    )
    assert o.notional == Decimal("5000")
    assert o.estimated_price == Decimal("500")


def test_rebalance_row_default_note():
    r = RebalanceRow(
        ticker="SPY",
        current_pct=Decimal("0.5"),
        target_pct=Decimal("0.42"),
        delta_dollars=Decimal("-100"),
        order=None,
    )
    assert r.note == ""
