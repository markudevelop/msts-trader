from __future__ import annotations

from decimal import Decimal

from msts_trader.diff import DRIFT_THRESHOLD, build_preview
from msts_trader.models import Position, Side, Target


def _nav() -> Decimal:
    return Decimal("50000")


def test_first_buy_when_flat(basic_targets, empty_positions, basic_quotes):
    p = build_preview(
        targets=basic_targets,
        positions=empty_positions,
        nav=_nav(),
        cash=_nav(),
        buying_power=_nav(),
        quotes=basic_quotes,
    )
    # SPY 0.50 * 50k = 25k @ 500 = 50 sh
    # SHV 0.50 * 50k = 25k @ 110 = 227.27 sh
    by_t = {o.ticker: o for o in p.orders}
    assert by_t["SPY"].side == Side.BUY
    assert by_t["SPY"].quantity == Decimal("50.00")
    assert by_t["SHV"].side == Side.BUY
    assert by_t["SHV"].quantity == Decimal("227.27")
    assert not p.blockers


def test_within_drift_skips_order():
    targets = [Target(ticker="SPY", weight=Decimal("0.50"))]
    positions = {"SPY": Position(ticker="SPY", quantity=Decimal("50"), price=Decimal("502"))}  # mv=25100 vs target 25000 => 0.2%
    p = build_preview(
        targets=targets,
        positions=positions,
        nav=Decimal("50000"),
        cash=Decimal("25000"),
        buying_power=Decimal("25000"),
        quotes={"SPY": Decimal("502")},
    )
    assert p.orders == []
    assert any("within drift" in r.note for r in p.rows)


def test_drift_above_threshold_triggers_order():
    targets = [Target(ticker="SPY", weight=Decimal("0.50"))]
    positions = {"SPY": Position(ticker="SPY", quantity=Decimal("40"), price=Decimal("500"))}  # 20k vs target 25k => 10%
    p = build_preview(
        targets=targets,
        positions=positions,
        nav=Decimal("50000"),
        cash=Decimal("30000"),
        buying_power=Decimal("30000"),
        quotes={"SPY": Decimal("500")},
    )
    assert len(p.orders) == 1
    assert p.orders[0].side == Side.BUY
    assert p.orders[0].quantity == Decimal("10.00")


def test_exits_positions_not_in_targets():
    targets = [Target(ticker="SPY", weight=Decimal("1.0"))]
    positions = {
        "SPY": Position(ticker="SPY", quantity=Decimal("100"), price=Decimal("500")),
        "GLD": Position(ticker="GLD", quantity=Decimal("5"), price=Decimal("200")),
    }
    p = build_preview(
        targets=targets,
        positions=positions,
        nav=Decimal("51000"),
        cash=Decimal("0"),
        buying_power=Decimal("0"),
        quotes={"SPY": Decimal("500"), "GLD": Decimal("200")},
    )
    sells = [o for o in p.orders if o.side == Side.SELL]
    assert any(o.ticker == "GLD" and o.quantity == Decimal("5.00") for o in sells)


def test_no_quote_skips_with_warning():
    targets = [Target(ticker="SPY", weight=Decimal("0.5")), Target(ticker="UNK", weight=Decimal("0.5"))]
    p = build_preview(
        targets=targets,
        positions={},
        nav=Decimal("50000"),
        cash=Decimal("50000"),
        buying_power=Decimal("50000"),
        quotes={"SPY": Decimal("500")},  # UNK missing
    )
    assert any(o.ticker == "SPY" for o in p.orders)
    assert not any(o.ticker == "UNK" for o in p.orders)
    assert any("no quote" in w for w in p.warnings)


def test_weights_over_one_blocks():
    targets = [Target(ticker="SPY", weight=Decimal("0.7")), Target(ticker="GLD", weight=Decimal("0.6"))]
    p = build_preview(
        targets=targets,
        positions={},
        nav=Decimal("50000"),
        cash=Decimal("50000"),
        buying_power=Decimal("50000"),
        quotes={"SPY": Decimal("500"), "GLD": Decimal("200")},
    )
    assert p.has_blockers
    assert any("malformed" in b for b in p.blockers)


def test_zero_nav_blocks():
    p = build_preview(
        targets=[Target(ticker="SPY", weight=Decimal("1.0"))],
        positions={},
        nav=Decimal("0"),
        cash=Decimal("0"),
        buying_power=Decimal("0"),
        quotes={"SPY": Decimal("500")},
    )
    assert p.has_blockers
    assert p.orders == []


def test_bp_overrun_warns_not_blocks():
    targets = [Target(ticker="SPY", weight=Decimal("0.9"))]
    p = build_preview(
        targets=targets,
        positions={},
        nav=Decimal("50000"),
        cash=Decimal("50000"),
        buying_power=Decimal("10000"),  # way under needed
        quotes={"SPY": Decimal("500")},
    )
    assert not p.has_blockers
    assert any("buying power" in w for w in p.warnings)


def test_drift_threshold_constant_is_4pct():
    assert DRIFT_THRESHOLD == Decimal("0.04")
