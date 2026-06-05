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


def test_leveraged_book_warns_not_blocks():
    # A real leveraged book sums to >1 (here 1.30 = 130% gross). It must NOT
    # be blocked — it should warn that it's leveraged and proceed.
    targets = [Target(ticker="SPY", weight=Decimal("0.7")), Target(ticker="GLD", weight=Decimal("0.6"))]
    p = build_preview(
        targets=targets,
        positions={},
        nav=Decimal("50000"),
        cash=Decimal("50000"),
        buying_power=Decimal("100000"),
        quotes={"SPY": Decimal("500"), "GLD": Decimal("200")},
    )
    assert not p.has_blockers
    assert any("leveraged book" in w.lower() for w in p.warnings)
    # Sizing is weight x NAV: SPY 0.7*50k=35k@500=70sh, GLD 0.6*50k=30k@200=150sh
    by_t = {o.ticker: o for o in p.orders}
    assert by_t["SPY"].quantity == Decimal("70.00")
    assert by_t["GLD"].quantity == Decimal("150.00")


def test_real_160pct_leveraged_book(basic_quotes):
    # The user's actual production weights sum to ~1.60 (160% gross / 1.6x).
    weights = {
        "QQQ": "0.3123", "GLD": "0.2537", "TBT": "0.1480", "SPY": "0.1178",
        "EEM": "0.1125", "ORR": "0.1080", "XLP": "0.0948", "EWJ": "0.0810",
        "SMH": "0.0675", "XLK": "0.0675", "USDU": "0.0671", "IWM": "0.0553",
        "DXJ": "0.0540", "UUP": "0.0503", "PDBC": "0.0124",
    }
    targets = [Target(ticker=t, weight=Decimal(w)) for t, w in weights.items()]
    quotes = {t: Decimal("100") for t in weights}
    p = build_preview(
        targets=targets,
        positions={},
        nav=Decimal("100000"),
        cash=Decimal("100000"),
        buying_power=Decimal("250000"),
        quotes=quotes,
    )
    assert not p.has_blockers
    # 14 of 15 size up; PDBC (1.24% of NAV) is below the 4% drift gate on a
    # fresh account, so it's skipped. Use a lower --threshold for initial setup.
    assert len(p.orders) == 14
    ordered = {o.ticker for o in p.orders}
    assert "PDBC" not in ordered
    assert "QQQ" in ordered and p.orders[0].quantity == Decimal("312.30")  # 0.3123*100k/100
    assert any("160% gross" in w or "leveraged book" in w.lower() for w in p.warnings)


def test_leveraged_book_low_threshold_captures_small_sleeve():
    # With a tighter threshold, even the 1.24% PDBC sleeve is established.
    targets = [Target(ticker="PDBC", weight=Decimal("0.0124")), Target(ticker="QQQ", weight=Decimal("0.3123"))]
    p = build_preview(
        targets=targets,
        positions={},
        nav=Decimal("100000"),
        cash=Decimal("100000"),
        buying_power=Decimal("250000"),
        quotes={"PDBC": Decimal("100"), "QQQ": Decimal("100")},
        drift_threshold=Decimal("0.01"),
    )
    ordered = {o.ticker for o in p.orders}
    assert "PDBC" in ordered


def test_absurd_gross_blocks_as_percentages():
    # Weights summing past 5x almost certainly means percentages were pasted.
    targets = [Target(ticker="SPY", weight=Decimal("3.0")), Target(ticker="GLD", weight=Decimal("3.0"))]
    p = build_preview(
        targets=targets,
        positions={},
        nav=Decimal("50000"),
        cash=Decimal("50000"),
        buying_power=Decimal("50000"),
        quotes={"SPY": Decimal("500"), "GLD": Decimal("200")},
    )
    assert p.has_blockers
    assert any("percentages" in b.lower() for b in p.blockers)


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
