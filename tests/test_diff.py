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
    # whole-book default: QQQ breaches the 4% gate, so the WHOLE book snaps to
    # target on a fresh account — all 15 sleeves (incl the 1.24% PDBC, which is
    # above the $1 dust floor) are established in one pass.
    assert len(p.orders) == 15
    ordered = {o.ticker for o in p.orders}
    assert "PDBC" in ordered
    assert "QQQ" in ordered and p.orders[0].quantity == Decimal("312.30")  # 0.3123*100k/100
    assert any("160% gross" in w or "leveraged book" in w.lower() for w in p.warnings)
    # under per-ticker scope the sub-4% PDBC sleeve stays frozen on a fresh account
    pt = build_preview(targets=targets, positions={}, nav=Decimal("100000"),
                       cash=Decimal("100000"), buying_power=Decimal("250000"),
                       quotes=quotes, rebalance_scope="per-ticker")
    assert len(pt.orders) == 14 and "PDBC" not in {o.ticker for o in pt.orders}


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


def test_sells_ordered_before_buys():
    # A rebalance with both buys and sells must list every SELL before any
    # BUY so proceeds fund the buys (required on cash accounts, harmless on
    # margin).
    targets = [Target(ticker="SPY", weight=Decimal("0.5")), Target(ticker="SHV", weight=Decimal("0.5"))]
    positions = {
        "GLD": Position(ticker="GLD", quantity=Decimal("100"), price=Decimal("200")),  # exit -> sell
        "EEM": Position(ticker="EEM", quantity=Decimal("100"), price=Decimal("50")),    # exit -> sell
    }
    p = build_preview(
        targets=targets, positions=positions,
        nav=Decimal("100000"), cash=Decimal("75000"), buying_power=Decimal("100000"),
        quotes={"SPY": Decimal("500"), "SHV": Decimal("110"), "GLD": Decimal("200"), "EEM": Decimal("50")},
    )
    sides = [o.side for o in p.orders]
    assert Side.SELL in sides and Side.BUY in sides
    last_sell = max(i for i, s in enumerate(sides) if s == Side.SELL)
    first_buy = min(i for i, s in enumerate(sides) if s == Side.BUY)
    assert last_sell < first_buy  # all sells precede all buys


def test_build_preview_warns_on_bp_overrun_but_does_not_scale():
    # build_preview itself no longer scales — it just warns. Scaling is
    # applied separately by apply_margin_aware.
    targets = [Target(ticker="SPY", weight=Decimal("1.5"))]
    p = build_preview(
        targets=targets, positions={},
        nav=Decimal("100000"), cash=Decimal("100000"), buying_power=Decimal("50000"),
        quotes={"SPY": Decimal("500")},
    )
    assert any("exceed buying power" in w for w in p.warnings)
    assert any("--margin-aware" in w for w in p.warnings)
    assert sum((o.notional for o in p.orders), Decimal(0)) > Decimal("50000")


def test_apply_margin_aware_notional_scales_to_fit():
    from msts_trader.diff import apply_margin_aware

    targets = [Target(ticker="SPY", weight=Decimal("1.0")), Target(ticker="QQQ", weight=Decimal("0.6"))]
    p = build_preview(
        targets=targets, positions={},
        nav=Decimal("100000"), cash=Decimal("100000"), buying_power=Decimal("80000"),
        quotes={"SPY": Decimal("500"), "QQQ": Decimal("400")},
    )
    apply_margin_aware(p, buying_power=Decimal("80000"))  # no real_margin -> notional
    gross = sum((o.notional for o in p.orders), Decimal(0))
    assert gross <= Decimal("80000") * Decimal("0.97") + 1  # fits with cushion
    by_t = {o.ticker: o for o in p.orders}
    ratio = by_t["SPY"].notional / by_t["QQQ"].notional
    assert Decimal("1.6") < ratio < Decimal("1.7")  # weights preserved (~1.0/0.6)
    assert any("estimated" in w.lower() for w in p.warnings)


def test_apply_margin_aware_uses_real_margin_when_given():
    from msts_trader.diff import apply_margin_aware

    # Real margin is HIGHER than notional (leveraged ETF) -> scales more.
    targets = [Target(ticker="TBT", weight=Decimal("1.0"))]
    p = build_preview(
        targets=targets, positions={},
        nav=Decimal("100000"), cash=Decimal("100000"), buying_power=Decimal("100000"),
        quotes={"TBT": Decimal("40")},
    )
    # notional ~ $100k fits BP $100k; but real margin $150k does NOT -> scales.
    apply_margin_aware(p, buying_power=Decimal("100000"), real_margin=Decimal("150000"))
    assert any("real broker margin" in w.lower() for w in p.warnings)
    assert sum((o.notional for o in p.orders), Decimal(0)) < Decimal("100000")


def test_sub_dollar_delta_is_dust_skipped():
    # A target whose dollar delta is under $1 is treated as dust and skipped.
    targets = [Target(ticker="SPY", weight=Decimal("0.50"))]
    positions = {"SPY": Position(ticker="SPY", quantity=Decimal("100"), price=Decimal("250.00"))}
    # current = 25000, target = 0.5*50000.50 = 25000.25 -> delta $0.25 < $1
    p = build_preview(
        targets=targets, positions=positions,
        nav=Decimal("50000.50"), cash=Decimal("25000"), buying_power=Decimal("25000"),
        quotes={"SPY": Decimal("250.00")},
        drift_threshold=Decimal("0"),  # disable drift gate so we reach the dust check
        rebalance_scope="per-ticker",  # dust gate is a per-line concept
    )
    assert p.orders == []
    assert any(r.ticker == "SPY" and r.note == "dust" for r in p.rows)


def test_apply_margin_aware_notes_fit_via_sells():
    from msts_trader.diff import apply_margin_aware

    # Buys exceed raw BP, but sell proceeds cover them -> no scaling, but a
    # clear "fits via sell proceeds" note is added.
    targets = [Target(ticker="SPY", weight=Decimal("1.0"))]
    positions = {"GLD": Position(ticker="GLD", quantity=Decimal("450"), price=Decimal("200"))}  # $90k sell
    p = build_preview(
        targets=targets, positions=positions,
        nav=Decimal("100000"), cash=Decimal("20000"), buying_power=Decimal("20000"),
        quotes={"SPY": Decimal("500"), "GLD": Decimal("200")},
    )
    # BP 20k + $90k sell proceeds -> available ~106k >= $100k buys -> fits
    apply_margin_aware(p, buying_power=Decimal("20000"))
    assert any("no scaling needed" in w for w in p.warnings)
    # not scaled
    spy = next(o for o in p.orders if o.ticker == "SPY")
    assert spy.quantity == Decimal("200.00")  # 100k / 500


def test_fully_invested_book_not_trimmed_on_cash_account():
    from msts_trader.diff import apply_margin_aware

    # 100% book (sum = 1.0) on a cash account where BP == NAV must deploy fully,
    # NOT get shaved to 97% by the safety cushion. (Regression: the cushion
    # was wrongly applied to the fit check.)
    targets = [Target(ticker="SPY", weight=Decimal("0.6")), Target(ticker="SHV", weight=Decimal("0.4"))]
    p = build_preview(
        targets=targets, positions={},
        nav=Decimal("50000"), cash=Decimal("50000"), buying_power=Decimal("50000"),
        quotes={"SPY": Decimal("500"), "SHV": Decimal("110")},
    )
    scaled = apply_margin_aware(p, buying_power=Decimal("50000"))
    assert scaled == Decimal(1)  # no scaling — fits within full BP
    gross = sum((o.notional for o in p.orders), Decimal(0))
    # Full deployment (buys round down, so never over BP; within a few $ of 50k).
    assert Decimal("49990") <= gross <= Decimal("50000")


def test_over_bp_book_scaled_with_safety_cushion():
    from msts_trader.diff import apply_margin_aware

    # A book that exceeds BP is scaled to land BELOW the limit (cushion), so a
    # market fill with mild slippage still clears.
    targets = [Target(ticker="SPY", weight=Decimal("1.2"))]
    p = build_preview(
        targets=targets, positions={},
        nav=Decimal("50000"), cash=Decimal("50000"), buying_power=Decimal("50000"),
        quotes={"SPY": Decimal("500")},
    )
    apply_margin_aware(p, buying_power=Decimal("50000"))
    gross = sum((o.notional for o in p.orders), Decimal(0))
    assert gross <= Decimal("50000") * Decimal("0.97") + 1  # cushioned below BP


def test_margin_aware_handles_zero_buy_book():
    # All-sells book (target all cash) + zero BP must not divide by zero.
    from msts_trader.diff import apply_margin_aware

    targets = [Target(ticker="SHV", weight=Decimal("0"))]
    positions = {"SPY": Position(ticker="SPY", quantity=Decimal("100"), price=Decimal("500"))}
    p = build_preview(
        targets=targets, positions=positions,
        nav=Decimal("50000"), cash=Decimal("0"), buying_power=Decimal("0"),
        quotes={"SPY": Decimal("500"), "SHV": Decimal("110")},
    )
    scale = apply_margin_aware(p, buying_power=Decimal("0"))  # no crash
    assert scale == Decimal(1)
    assert [o.side for o in p.orders] == [Side.SELL]  # only the SPY exit


def test_apply_margin_aware_noop_when_fits():
    from msts_trader.diff import apply_margin_aware

    targets = [Target(ticker="SPY", weight=Decimal("0.5"))]
    p = build_preview(
        targets=targets, positions={},
        nav=Decimal("100000"), cash=Decimal("100000"), buying_power=Decimal("100000"),
        quotes={"SPY": Decimal("500")},
    )
    before = [(o.ticker, o.quantity) for o in p.orders]
    apply_margin_aware(p, buying_power=Decimal("100000"))
    after = [(o.ticker, o.quantity) for o in p.orders]
    assert before == after  # nothing scaled
    assert not any("margin-aware" in w.lower() for w in p.warnings)


def test_short_position_not_in_targets_is_left_untouched():
    # A short (negative qty) not in targets must NOT generate a buy-to-cover
    # in v1 — shorts are unsupported, so we leave it alone.
    targets = [Target(ticker="SPY", weight=Decimal("1.0"))]
    positions = {
        "SPY": Position(ticker="SPY", quantity=Decimal("100"), price=Decimal("500")),
        "QQQ": Position(ticker="QQQ", quantity=Decimal("-10"), price=Decimal("400")),  # short
    }
    p = build_preview(
        targets=targets, positions=positions,
        nav=Decimal("50000"), cash=Decimal("0"), buying_power=Decimal("0"),
        quotes={"SPY": Decimal("500"), "QQQ": Decimal("400")},
    )
    assert not any(o.ticker == "QQQ" for o in p.orders)


def test_qty_rounding_to_zero_is_skipped():
    # A target whose dollar delta is above the drift gate but rounds to 0
    # shares at a very high price is skipped, not sent as a 0-qty order.
    targets = [Target(ticker="BRKA", weight=Decimal("0.10"))]
    # 10% of 50k = $5000 target; price $700,000 => 0.007 -> quantize(0.01) = 0.01... actually rounds up.
    # Use a price that makes qty round to exactly 0.00.
    p = build_preview(
        targets=targets, positions={},
        nav=Decimal("50000"), cash=Decimal("50000"), buying_power=Decimal("50000"),
        quotes={"BRKA": Decimal("200000000")},  # $5000 / 200M = 0.000025 -> 0.00
    )
    assert all(o.ticker != "BRKA" or o.quantity > 0 for o in p.orders)
    assert any(r.ticker == "BRKA" and "rounds to 0" in r.note for r in p.rows)


# ----- min_weight -----

def test_min_weight_drops_tiny_target_without_touching_position():
    # 0.005 < min_weight 0.01: no buy for the flat ticker, no sell for the
    # held one — "ignore" means neither side trades.
    targets = [
        Target(ticker="SPY", weight=Decimal("0.50")),
        Target(ticker="TINY", weight=Decimal("0.005")),   # flat, would be a buy
        Target(ticker="DUSTY", weight=Decimal("0.005")),  # held, must NOT be exit-swept
    ]
    positions = {"DUSTY": Position(ticker="DUSTY", quantity=Decimal("100"), price=Decimal("50"))}
    p = build_preview(
        targets=targets, positions=positions,
        nav=Decimal("50000"), cash=Decimal("50000"), buying_power=Decimal("50000"),
        quotes={"SPY": Decimal("500"), "TINY": Decimal("10"), "DUSTY": Decimal("50")},
        min_weight=Decimal("0.01"),
    )
    tickers = {o.ticker for o in p.orders}
    assert tickers == {"SPY"}
    assert sum(1 for r in p.rows if "below min weight" in r.note) == 2


def test_min_weight_none_keeps_all_targets():
    targets = [Target(ticker="TINY", weight=Decimal("0.005"))]
    p = build_preview(
        targets=targets, positions={},
        nav=Decimal("50000"), cash=Decimal("50000"), buying_power=Decimal("50000"),
        quotes={"TINY": Decimal("10")},
        drift_threshold=Decimal("0.001"),
    )
    assert [o.ticker for o in p.orders] == ["TINY"]


def test_min_weight_keeps_explicit_zero_exit_semantics():
    # weight == 0 is an explicit exit instruction, not a tiny weight — it
    # must still sell even when min_weight is set.
    targets = [Target(ticker="OUT", weight=Decimal("0"))]
    positions = {"OUT": Position(ticker="OUT", quantity=Decimal("100"), price=Decimal("50"))}
    p = build_preview(
        targets=targets, positions=positions,
        nav=Decimal("50000"), cash=Decimal("0"), buying_power=Decimal("0"),
        quotes={"OUT": Decimal("50")},
        min_weight=Decimal("0.01"),
    )
    assert len(p.orders) == 1
    assert p.orders[0].ticker == "OUT" and p.orders[0].side == Side.SELL


# ----- allocation (sub-portfolio sizing) -----

def test_allocation_sizes_against_dollar_base_not_nav():
    # $200k account, weights apply to a $50k sleeve: 50% SPY = $25k = 50 sh.
    targets = [Target(ticker="SPY", weight=Decimal("0.50"))]
    p = build_preview(
        targets=targets, positions={},
        nav=Decimal("200000"), cash=Decimal("200000"), buying_power=Decimal("200000"),
        quotes={"SPY": Decimal("500")},
        allocation=Decimal("50000"),
    )
    assert len(p.orders) == 1
    assert p.orders[0].quantity == Decimal("50.00")
    assert p.nav == Decimal("200000")  # display NAV stays the real account NAV
    assert any("allocation" in w.lower() for w in p.warnings)


def test_allocation_drift_relative_to_allocation():
    # Held $24.9k vs $25k target on a $50k allocation = 0.2% drift -> skip.
    # Against the full $200k NAV the same delta would be far below threshold
    # anyway, so check the converse: a 6% drift OF THE ALLOCATION trades even
    # though it is only 1.5% of NAV.
    targets = [Target(ticker="SPY", weight=Decimal("0.50"))]
    positions = {"SPY": Position(ticker="SPY", quantity=Decimal("44"), price=Decimal("500"))}  # 22k vs 25k = 6% of 50k
    p = build_preview(
        targets=targets, positions=positions,
        nav=Decimal("200000"), cash=Decimal("178000"), buying_power=Decimal("178000"),
        quotes={"SPY": Decimal("500")},
        allocation=Decimal("50000"),
    )
    assert len(p.orders) == 1 and p.orders[0].side == Side.BUY


def test_allocation_above_nav_falls_back_to_nav():
    targets = [Target(ticker="SPY", weight=Decimal("0.50"))]
    p = build_preview(
        targets=targets, positions={},
        nav=Decimal("50000"), cash=Decimal("50000"), buying_power=Decimal("50000"),
        quotes={"SPY": Decimal("500")},
        allocation=Decimal("999999"),
    )
    # sized against NAV: 25k @ 500 = 50 sh, and a warning explains why
    assert p.orders[0].quantity == Decimal("50.00")
    assert any("exceeds account NAV" in w for w in p.warnings)


# ----- whole-share mode (IBKR/accounts without fractional-API permission) -----

def test_whole_shares_rounds_buy_down(basic_targets, empty_positions, basic_quotes):
    # SHV 0.50 * 50k = 25k @ 110 = 227.27 sh -> 227 whole shares (rounds DOWN)
    p = build_preview(
        targets=basic_targets, positions=empty_positions, nav=_nav(),
        cash=_nav(), buying_power=_nav(), quotes=basic_quotes, whole_shares=True,
    )
    by_t = {o.ticker: o for o in p.orders}
    assert by_t["SPY"].quantity == Decimal("50")     # already whole
    assert by_t["SHV"].quantity == Decimal("227")    # 227.27 truncated
    # every order quantity is integral
    assert all(o.quantity == o.quantity.to_integral_value() for o in p.orders)


def test_whole_shares_off_keeps_fraction(basic_targets, empty_positions, basic_quotes):
    p = build_preview(
        targets=basic_targets, positions=empty_positions, nav=_nav(),
        cash=_nav(), buying_power=_nav(), quotes=basic_quotes,  # default: fractional allowed
    )
    by_t = {o.ticker: o for o in p.orders}
    assert by_t["SHV"].quantity == Decimal("227.27")


def test_whole_shares_rounds_exit_down():
    # Position not in targets -> full exit; fractional holding rounds DOWN so we
    # never try to sell more than a whole-share broker can handle.
    targets = [Target(ticker="SPY", weight=Decimal("1.0"))]
    positions = {
        "SPY": Position(ticker="SPY", quantity=Decimal("10"), price=Decimal("500")),
        "GLD": Position(ticker="GLD", quantity=Decimal("5.6"), price=Decimal("200")),  # exit
    }
    p = build_preview(
        targets=targets, positions=positions, nav=Decimal("50000"),
        cash=Decimal("0"), buying_power=Decimal("0"),
        quotes={"SPY": Decimal("500"), "GLD": Decimal("200")}, whole_shares=True,
    )
    gld = next(o for o in p.orders if o.ticker == "GLD")
    assert gld.side == Side.SELL and gld.quantity == Decimal("5")  # 5.6 -> 5


# ----------------------------------------------------------------- sweep ----
# SPY is exactly on target (no trade); GLD is held but NOT in the CSV.
_SWEEP_TGTS = [Target("SPY", Decimal("1.0"))]
_SWEEP_POS = {
    "SPY": Position("SPY", Decimal("100"), Decimal("500")),  # $50k == target, no trade
    "GLD": Position("GLD", Decimal("10"), Decimal("200")),   # $2k, not in targets
}
_SWEEP_Q = {"SPY": Decimal("500"), "GLD": Decimal("200")}


def _sweep_preview(**kw):
    return build_preview(targets=_SWEEP_TGTS, positions=_SWEEP_POS, nav=Decimal("50000"),
                         cash=Decimal("0"), buying_power=Decimal("0"), quotes=_SWEEP_Q, **kw)


def test_default_sweeps_unlisted_position():
    p = _sweep_preview()  # sweep defaults True
    gld = next(o for o in p.orders if o.ticker == "GLD")
    assert gld.side == Side.SELL and gld.quantity == Decimal("10")


def test_no_sweep_leaves_unlisted_position_untouched():
    p = _sweep_preview(sweep=False)
    assert p.orders == []                                     # nothing traded
    gld_row = next(r for r in p.rows if r.ticker == "GLD")
    assert gld_row.order is None                              # surfaced, but no order
    assert "kept" in gld_row.note and "not in targets" in gld_row.note


def test_no_sweep_still_exits_explicit_weight_zero():
    # Under --no-sweep, a rotated-out name is closed by listing it with weight 0.
    targets = [Target("SPY", Decimal("1.0")), Target("GLD", Decimal("0"))]
    p = build_preview(targets=targets, positions=_SWEEP_POS, nav=Decimal("50000"),
                      cash=Decimal("0"), buying_power=Decimal("0"), quotes=_SWEEP_Q, sweep=False)
    gld = next(o for o in p.orders if o.ticker == "GLD")
    assert gld.side == Side.SELL and gld.quantity == Decimal("10")


def test_whole_shares_buy_rounding_to_zero_is_skipped():
    # Delta clears the 4% drift threshold ($2500 = 5% of NAV) but the high
    # share price means it buys < 1 whole share -> dropped cleanly with a
    # whole-share note (not a fractional 0.83-share order IBKR would reject).
    targets = [Target(ticker="BRKA", weight=Decimal("0.05"))]  # 0.05 * 50k = $2500
    p = build_preview(
        targets=targets, positions={}, nav=Decimal("50000"),
        cash=Decimal("50000"), buying_power=Decimal("50000"),
        quotes={"BRKA": Decimal("3000")},  # $2500 / 3000 = 0.83 sh -> 0 whole
        whole_shares=True,
    )
    assert p.orders == []
    assert any("whole-share" in (r.note or "") for r in p.rows)


def test_whole_shares_margin_aware_rerounds_to_integer():
    from msts_trader.diff import apply_margin_aware
    targets = [Target(ticker="SPY", weight=Decimal("1.0"))]
    p = build_preview(
        targets=targets, positions={}, nav=Decimal("100000"),
        cash=Decimal("100000"), buying_power=Decimal("60000"),  # can't afford full 200 sh
        quotes={"SPY": Decimal("500")}, whole_shares=True,
    )
    apply_margin_aware(p, buying_power=Decimal("60000"), whole_shares=True)
    spy = next(o for o in p.orders if o.ticker == "SPY")
    assert spy.quantity == spy.quantity.to_integral_value()  # scaling kept it whole
