"""Stops support + position-relative drift mode."""
from decimal import Decimal

import pytest

from msts_trader.csv_parser import CSVParseError, parse_csv
from msts_trader.diff import build_preview
from msts_trader.models import Position, Side, Target


def _preview(targets, positions=None, nav="100000", quotes=None, **kw):
    return build_preview(
        targets=targets,
        positions=positions or {},
        nav=Decimal(nav),
        cash=Decimal(nav),
        buying_power=Decimal(nav),
        quotes=quotes or {},
        **kw,
    )


# ---------------------------------------------------------------- CSV ----

def test_csv_stop_pct_parsed():
    t = parse_csv("ticker,weight,stop_pct\nSPY,0.5,\nWGMI,0.018,0.015\n")
    by = {x.ticker: x for x in t}
    assert by["SPY"].stop_pct is None
    assert by["WGMI"].stop_pct == Decimal("0.015")


def test_csv_stop_pct_bounds():
    with pytest.raises(CSVParseError, match="outside"):
        parse_csv("ticker,weight,stop_pct\nSPY,0.5,0.9\n")
    with pytest.raises(CSVParseError, match="not a number"):
        parse_csv("ticker,weight,stop_pct\nSPY,0.5,abc\n")


def test_csv_without_stop_column_unchanged():
    t = parse_csv("ticker,weight\nSPY,0.5\n")
    assert t[0].stop_pct is None


# ------------------------------------------------------------ drift mode ----

def test_nav_mode_freezes_small_lines():
    # 1.8% line, no position: delta = 1.8% of NAV < 4% threshold -> frozen
    p = _preview([Target("WGMI", Decimal("0.018"))], quotes={"WGMI": Decimal("10")})
    assert p.orders == []
    assert "within drift" in p.rows[0].note


def test_position_mode_trades_small_lines():
    p = _preview([Target("WGMI", Decimal("0.018"))], quotes={"WGMI": Decimal("10")},
                 drift_mode="position")
    assert len(p.orders) == 1
    o = p.orders[0]
    assert o.side == Side.BUY
    assert o.notional == Decimal("1800.00")


def test_position_mode_respects_threshold_on_small_drift():
    # held 1.80%, target 1.83% -> drift 1.7% of the LINE < 4% -> skip
    pos = {"WGMI": Position("WGMI", Decimal("180"), Decimal("10"))}
    p = _preview([Target("WGMI", Decimal("0.0183"))], positions=pos,
                 quotes={"WGMI": Decimal("10")}, drift_mode="position")
    assert p.orders == []


# -------------------------------------------------------- rebalance scope ----
# SPY breaches (10% of NAV off), AGG is within drift (1% off). The scope decides
# whether AGG (within) is snapped to target alongside SPY or left alone.
_SCOPE_POS = {
    "SPY": Position("SPY", Decimal("400"), Decimal("100")),  # $40k, target $50k -> +10% NAV
    "AGG": Position("AGG", Decimal("490"), Decimal("100")),  # $49k, target $50k -> +1% NAV
}
_SCOPE_TGTS = [Target("SPY", Decimal("0.5")), Target("AGG", Decimal("0.5"))]
_SCOPE_QUOTES = {"SPY": Decimal("100"), "AGG": Decimal("100")}


def test_whole_book_snaps_all_when_one_breaches():
    # default scope: SPY breaches -> the WHOLE book snaps, so AGG (within) trades too
    p = _preview(_SCOPE_TGTS, positions=_SCOPE_POS, quotes=_SCOPE_QUOTES)
    by = {o.ticker: o for o in p.orders}
    assert set(by) == {"SPY", "AGG"}
    assert by["SPY"].side == Side.BUY and by["SPY"].quantity == Decimal("100")
    assert by["AGG"].side == Side.BUY and by["AGG"].quantity == Decimal("10")


def test_per_ticker_trades_only_breaching():
    # per-ticker: only SPY trades; AGG stays within drift
    p = _preview(_SCOPE_TGTS, positions=_SCOPE_POS, quotes=_SCOPE_QUOTES,
                 rebalance_scope="per-ticker")
    assert [o.ticker for o in p.orders] == ["SPY"]
    agg = next(r for r in p.rows if r.ticker == "AGG")
    assert agg.order is None and "within drift" in agg.note


def test_whole_book_frozen_when_nothing_breaches():
    # both lines within drift (1% off) -> whole book frozen, no orders
    pos = {
        "SPY": Position("SPY", Decimal("490"), Decimal("100")),
        "AGG": Position("AGG", Decimal("490"), Decimal("100")),
    }
    p = _preview(_SCOPE_TGTS, positions=pos, quotes=_SCOPE_QUOTES)
    assert p.orders == []
    assert all("within drift (book frozen)" in r.note for r in p.rows)


def test_exit_triggers_whole_book_snap():
    # In-target SPY is within drift, but a stray held name (not in targets) makes
    # the book live -> whole-book snaps SPY too AND exits the stray.
    pos = {
        "SPY": Position("SPY", Decimal("490"), Decimal("100")),   # within drift (1% off)
        "AGG": Position("AGG", Decimal("490"), Decimal("100")),   # within drift (1% off)
        "TLT": Position("TLT", Decimal("100"), Decimal("100")),   # $10k, not in targets
    }
    whole = _preview(_SCOPE_TGTS, positions=pos,
                     quotes={**_SCOPE_QUOTES, "TLT": Decimal("100")})
    by = {o.ticker: o.side for o in whole.orders}
    assert by.get("TLT") == Side.SELL          # stray exited
    assert by.get("SPY") == Side.BUY            # within-drift line snapped too
    # per-ticker: SPY stays put, only the stray exits
    per = _preview(_SCOPE_TGTS, positions=pos,
                   quotes={**_SCOPE_QUOTES, "TLT": Decimal("100")},
                   rebalance_scope="per-ticker")
    assert [o.ticker for o in per.orders] == ["TLT"]


# ------------------------------------------------------------- stop carry ----

def test_weight_zero_exits_small_position_like_missing_row():
    """Explicit weight 0 must fully exit even a sub-drift (<4% NAV) holding —
    identical to dropping the row — not freeze it as 'within drift'."""
    pos = {"AAA": Position("AAA", Decimal("10"), Decimal("100"))}   # $1000 = 1% of $100k NAV
    # weight 0 (explicit) -> full exit
    p0 = _preview([Target("AAA", Decimal("0"))], positions=pos, quotes={"AAA": Decimal("100")})
    assert len(p0.orders) == 1
    assert p0.orders[0].side == Side.SELL
    assert p0.orders[0].quantity == Decimal("10")
    # dropping the row entirely -> same full exit (the exit-all sweep)
    pmiss = _preview([Target("BBB", Decimal("1.0"))], positions=pos,
                     quotes={"AAA": Decimal("100"), "BBB": Decimal("50")})
    aaa = [o for o in pmiss.orders if o.ticker == "AAA"]
    assert len(aaa) == 1 and aaa[0].side == Side.SELL and aaa[0].quantity == Decimal("10")


def test_buy_order_carries_stop_pct():
    p = _preview([Target("WGMI", Decimal("0.018"), stop_pct=Decimal("0.015"))],
                 quotes={"WGMI": Decimal("10")}, drift_mode="position")
    assert p.orders[0].stop_pct == Decimal("0.015")


def test_sell_order_does_not_carry_stop_pct():
    pos = {"WGMI": Position("WGMI", Decimal("500"), Decimal("10"))}
    p = _preview([Target("WGMI", Decimal("0.01"), stop_pct=Decimal("0.015"))],
                 positions=pos, quotes={"WGMI": Decimal("10")}, drift_mode="position")
    assert p.orders[0].side == Side.SELL
    assert p.orders[0].stop_pct is None


# ------------------------------------------------------------ paper stops ----

def test_paper_stop_lifecycle(tmp_path, monkeypatch):
    import msts_trader.brokers.paper as paper_mod
    monkeypatch.setattr(paper_mod, "STATE_PATH", tmp_path / "paper.json")
    b = paper_mod.Paper()
    assert b.supports_stops
    res = b.place_stop("WGMI", Decimal("180"), Decimal("9.85"))
    assert res["status"] == "ACCEPTED"
    stops = b.open_stops()
    assert "WGMI" in stops and stops["WGMI"][0]["stop_price"] == Decimal("9.85")
    cancel = b.cancel_order(res["order_id"])
    assert cancel["status"] == "CANCELLED"
    assert b.open_stops() == {}


def test_paper_stop_dry_run(tmp_path, monkeypatch):
    import msts_trader.brokers.paper as paper_mod
    monkeypatch.setattr(paper_mod, "STATE_PATH", tmp_path / "paper.json")
    b = paper_mod.Paper()
    res = b.place_stop("WGMI", Decimal("10"), Decimal("9.85"), dry_run=True)
    assert res["dry_run"] is True
    assert b.open_stops() == {}


# --------------------------------------------------- reconcile edge cases ----

def _mk_cli_env(tmp_path, monkeypatch):
    import msts_trader.brokers.paper as paper_mod
    monkeypatch.setattr(paper_mod, "STATE_PATH", tmp_path / "paper.json")
    return paper_mod.Paper()


def test_partial_reduce_replaces_stop_for_remainder(tmp_path, monkeypatch):
    """Trim 100 -> 60 shares: old stop cancelled, NEW stop covers the 60."""
    from msts_trader.__main__ import _reconcile_stops
    from msts_trader.models import Order, Preview, Side
    b = _mk_cli_env(tmp_path, monkeypatch)
    b.set_quote("WGMI", Decimal("50"))
    b.place_market(Order("WGMI", Side.BUY, Decimal("100"), Decimal("50")))
    b.place_stop("WGMI", Decimal("100"), Decimal("49.25"))
    sell = Order("WGMI", Side.SELL, Decimal("40"), Decimal("50"), stop_pct=None)
    preview = Preview(nav=Decimal(100000), buying_power=Decimal(0), cash=Decimal(0),
                      rows=[], orders=[sell])
    # the target still wants a stop on WGMI (stop_pct comes from targets via orders)
    sell2 = Order("WGMI", Side.SELL, Decimal("40"), Decimal("50"))
    sell2.stop_pct = Decimal("0.015")
    preview.orders = [sell2]
    res = b.place_market(sell2)
    _reconcile_stops(b, preview, [res])
    stops = b.open_stops()
    assert "WGMI" in stops, "remainder left unprotected after partial reduce"
    assert stops["WGMI"][0]["quantity"] == Decimal("60")


def test_addon_buy_protects_whole_position(tmp_path, monkeypatch):
    """Hold 100 (stopped), buy 50 more: new stop covers all 150."""
    from msts_trader.__main__ import _reconcile_stops
    from msts_trader.models import Order, Preview, Side
    b = _mk_cli_env(tmp_path, monkeypatch)
    b.set_quote("WGMI", Decimal("50"))
    b.place_market(Order("WGMI", Side.BUY, Decimal("100"), Decimal("50")))
    b.place_stop("WGMI", Decimal("100"), Decimal("49.25"))
    buy = Order("WGMI", Side.BUY, Decimal("50"), Decimal("52"), stop_pct=Decimal("0.015"))
    preview = Preview(nav=Decimal(100000), buying_power=Decimal(0), cash=Decimal(0),
                      rows=[], orders=[buy])
    res = b.place_market(buy)
    _reconcile_stops(b, preview, [res])
    stops = b.open_stops()
    assert len(stops["WGMI"]) == 1, "stale stop not replaced"
    assert stops["WGMI"][0]["quantity"] == Decimal("150"), "add-on left old shares uncovered"


def test_full_exit_cancels_without_replacing(tmp_path, monkeypatch):
    from msts_trader.__main__ import _reconcile_stops
    from msts_trader.models import Order, Preview, Side
    b = _mk_cli_env(tmp_path, monkeypatch)
    b.set_quote("WGMI", Decimal("50"))
    b.place_market(Order("WGMI", Side.BUY, Decimal("100"), Decimal("50")))
    b.place_stop("WGMI", Decimal("100"), Decimal("49.25"))
    sell = Order("WGMI", Side.SELL, Decimal("100"), Decimal("50"))
    sell.stop_pct = Decimal("0.015")
    preview = Preview(nav=Decimal(100000), buying_power=Decimal(0), cash=Decimal(0),
                      rows=[], orders=[sell])
    res = b.place_market(sell)
    _reconcile_stops(b, preview, [res])
    assert b.open_stops() == {}, "full exit must not leave or re-place stops"


# ----------------------------------------- concern 1: no naked stops ----

def test_no_stop_when_buy_fill_unconfirmed(tmp_path, monkeypatch):
    """A broker that accepts a BUY but whose positions() does not (yet) show the
    shares must NOT get a protective stop — never anchor on the intended size."""
    from msts_trader.__main__ import _reconcile_stops
    from msts_trader.models import Order, Preview, Side, Target

    class UnconfirmedBroker:
        name = "uc"
        supports_stops = True

        def __init__(self):
            self.placed = []

        def open_stops(self):
            return {}

        def positions(self):
            return {}  # fill not yet reflected

        def place_stop(self, tkr, qty, stop_price, dry_run=False):
            self.placed.append((tkr, qty))
            return {"status": "ACCEPTED", "ticker": tkr}

        def cancel_order(self, oid):
            return {"status": "CANCELLED"}

    b = UnconfirmedBroker()
    buy = Order("WGMI", Side.BUY, Decimal("100"), Decimal("50"), stop_pct=Decimal("0.015"))
    preview = Preview(nav=Decimal(0), buying_power=Decimal(0), cash=Decimal(0), rows=[], orders=[buy])
    # broker reports "accepted" (not filled) and positions() is empty
    res = {"status": "accepted", "ticker": "WGMI", "side": "BUY", "order_id": "x"}
    _reconcile_stops(b, preview, [res], targets=[Target("WGMI", Decimal("0.5"), stop_pct=Decimal("0.015"))])
    assert b.placed == [], "placed a stop for shares not confirmed held (naked stop)"


# --------------------------------- concern 2: orphan + missing-stop sweep ----

def test_orphan_stop_cancelled_when_no_position(tmp_path, monkeypatch):
    """A resting stop with no live position (manual exit / leftover) is cancelled
    even though the ticker isn't traded this run."""
    from msts_trader.__main__ import _reconcile_stops
    from msts_trader.models import Preview
    b = _mk_cli_env(tmp_path, monkeypatch)
    b.place_stop("WGMI", Decimal("100"), Decimal("49.25"))   # stop, but no position
    assert "WGMI" in b.open_stops()
    preview = Preview(nav=Decimal(100000), buying_power=Decimal(0), cash=Decimal(0), rows=[], orders=[])
    _reconcile_stops(b, preview, [], targets=[])
    assert b.open_stops() == {}, "orphan stop with no position must be cancelled"


def test_is_clean_send_excludes_resting():
    from msts_trader.__main__ import _is_clean_send
    assert _is_clean_send("submitted") is True
    assert _is_clean_send("FILLED") is True
    assert _is_clean_send("ok") is True
    # error / skipped / resting are NOT clean completions
    assert _is_clean_send("error") is False
    assert _is_clean_send("skipped") is False
    assert _is_clean_send("resting") is False   # placed but unfilled (HL thin book)
    assert _is_clean_send("RESTING") is False


def test_backfill_stop_anchors_on_live_quote_not_cost():
    """A backfilled protective stop must anchor on the LIVE quote, not the
    position's price (which Tradier reports as average COST)."""
    from msts_trader.__main__ import _reconcile_stops
    from msts_trader.models import Preview, Target

    class _CostBasisBroker:
        name = "fake"
        supports_stops = True

        def __init__(self):
            self.placed = []

        def open_stops(self):
            return {}

        def positions(self):
            return {"WGMI": Position("WGMI", Decimal("100"), Decimal("50"))}  # price = avg COST 50

        def quote(self, tickers):
            return {"WGMI": Decimal("60")}  # live market 60 (≠ cost)

        def place_stop(self, tkr, qty, stop_price, dry_run=False):
            self.placed.append((tkr, qty, Decimal(str(stop_price))))
            return {"status": "submitted", "ticker": tkr}

        def cancel_order(self, oid):
            return {"status": "CANCELLED"}

    b = _CostBasisBroker()
    preview = Preview(nav=Decimal("100000"), buying_power=Decimal("0"), cash=Decimal("0"),
                      rows=[], orders=[])
    _reconcile_stops(b, preview, [], targets=[Target("WGMI", Decimal("0.5"), stop_pct=Decimal("0.02"))])
    assert len(b.placed) == 1
    _, _, stop_price = b.placed[0]
    assert stop_price == Decimal("60") * (Decimal("1") - Decimal("0.02"))  # 58.80 (quote), NOT 49.00 (cost)


def test_missing_stop_backfilled_for_held_untraded_name(tmp_path, monkeypatch):
    """A held position the target wants protected, with no open stop and no trade
    this run, gets a stop backfilled from the target book."""
    from msts_trader.__main__ import _reconcile_stops
    from msts_trader.models import Order, Preview, Side, Target
    b = _mk_cli_env(tmp_path, monkeypatch)
    b.set_quote("WGMI", Decimal("50"))
    b.place_market(Order("WGMI", Side.BUY, Decimal("100"), Decimal("50")))  # held, no stop
    assert b.open_stops() == {}
    preview = Preview(nav=Decimal(100000), buying_power=Decimal(0), cash=Decimal(0), rows=[], orders=[])
    _reconcile_stops(b, preview, [], targets=[Target("WGMI", Decimal("0.5"), stop_pct=Decimal("0.02"))])
    stops = b.open_stops()
    assert "WGMI" in stops, "missing stop on a held target name was not backfilled"
    assert stops["WGMI"][0]["quantity"] == Decimal("100")
    assert stops["WGMI"][0]["stop_price"] == Decimal("49.00")  # 50 * (1 - 0.02)


def test_apply_default_stop_backfills_blanks_only():
    """--stop-pct fills targets that lack a per-row stop; explicit per-row wins;
    exits (weight 0) are left alone."""
    from msts_trader.__main__ import _apply_default_stop
    from msts_trader.models import Target

    ts = [
        Target("AAA", Decimal("0.5")),                                  # no stop -> default
        Target("BBB", Decimal("0.3"), stop_pct=Decimal("0.01")),       # explicit -> kept
        Target("CCC", Decimal("0")),                                    # exit -> untouched
    ]
    out = {t.ticker: t.stop_pct for t in _apply_default_stop(ts, Decimal("0.02"))}
    assert out == {"AAA": Decimal("0.02"), "BBB": Decimal("0.01"), "CCC": None}
    # no default -> unchanged
    assert _apply_default_stop(ts, None) == ts


def test_stop_anchors_on_order_status_fill_price(tmp_path, monkeypatch):
    """A broker whose place_market returns no fill price (async ack) still gets
    its stop anchored on the REAL entry, pulled from order_status — not the
    position's avg/current price."""
    from msts_trader.__main__ import _execute
    from msts_trader.models import Order, Position, Preview, Side, Target

    class AsyncFillBroker:
        name = "af"
        supports_stops = True

        def __init__(self):
            self.placed = []

        def open_stops(self):
            return {}

        def place_market(self, o, dry_run=False):
            # async ack: confirms nothing, carries no fill price
            return {"status": "accepted", "ticker": o.ticker, "side": o.side.value,
                    "quantity": float(o.quantity), "order_id": "o1"}

        def positions(self):
            # position shows up (qty + a stale avg price of 500)
            return {"SPY": Position("SPY", Decimal("10"), Decimal("500"))}

        def order_status(self, oid):
            # the true fill came in at 501.23, not the 500 the position shows
            return {"status": "filled", "filled_qty": 10.0, "filled_avg_price": 501.23}

        def place_stop(self, tkr, qty, stop_price, dry_run=False):
            self.placed.append((tkr, qty, float(stop_price)))
            return {"status": "ACCEPTED", "ticker": tkr, "order_id": "s1"}

        def cancel_order(self, oid):
            return {"status": "CANCELLED"}

    b = AsyncFillBroker()
    buy = Order("SPY", Side.BUY, Decimal("10"), Decimal("500"), stop_pct=Decimal("0.02"))
    preview = Preview(nav=Decimal(0), buying_power=Decimal(0), cash=Decimal(0), rows=[], orders=[buy])
    _execute(b, preview, targets=[Target("SPY", Decimal("1.0"), stop_pct=Decimal("0.02"))])
    # 501.23 * (1 - 0.02) = 491.21, NOT 500 * 0.98 = 490.00
    assert b.placed == [("SPY", Decimal("10"), 491.21)]


def test_existing_correct_stop_not_churned(tmp_path, monkeypatch):
    """A held name with an already-correct stop and no trade this run is left
    untouched (no cancel/replace churn that would re-anchor the stop daily)."""
    from msts_trader.__main__ import _reconcile_stops
    from msts_trader.models import Order, Preview, Side, Target
    b = _mk_cli_env(tmp_path, monkeypatch)
    b.set_quote("WGMI", Decimal("50"))
    b.place_market(Order("WGMI", Side.BUY, Decimal("100"), Decimal("50")))
    placed = b.place_stop("WGMI", Decimal("100"), Decimal("49.25"))
    oid = placed["order_id"]
    preview = Preview(nav=Decimal(100000), buying_power=Decimal(0), cash=Decimal(0), rows=[], orders=[])
    _reconcile_stops(b, preview, [], targets=[Target("WGMI", Decimal("0.5"), stop_pct=Decimal("0.015"))])
    stops = b.open_stops()
    assert len(stops["WGMI"]) == 1
    assert stops["WGMI"][0]["order_id"] == oid, "correct stop was needlessly cancelled/replaced"


def test_no_stop_pct_anywhere_places_no_stops(tmp_path, monkeypatch):
    """Opt-in stops: a BUY whose target has no stop_pct (and no --stop-pct) must leave the
    account with ZERO stops. Held-forever positions stay unstopped."""
    from msts_trader.__main__ import _reconcile_stops
    from msts_trader.models import Order, Preview, Side, Target
    b = _mk_cli_env(tmp_path, monkeypatch)
    b.set_quote("AAA", Decimal("50"))
    buy = Order("AAA", Side.BUY, Decimal("100"), Decimal("50"))  # no stop_pct
    res = b.place_market(buy)
    preview = Preview(nav=Decimal(100000), buying_power=Decimal(0), cash=Decimal(0),
                      rows=[], orders=[buy])
    _reconcile_stops(b, preview, [res], targets=[Target("AAA", Decimal("0.5"))])  # target: no stop_pct
    assert b.open_stops() == {}, "stop placed despite no stop_pct anywhere"


def test_held_stop_survives_when_feed_has_no_stop_pct(tmp_path, monkeypatch):
    """A pre-existing stop on a still-held position is NOT stripped by a no-stop_pct rebalance
    (only orphan stops with no position are cancelled)."""
    from msts_trader.__main__ import _reconcile_stops
    from msts_trader.models import Order, Preview, Side, Target
    b = _mk_cli_env(tmp_path, monkeypatch)
    b.set_quote("AAA", Decimal("50"))
    b.place_market(Order("AAA", Side.BUY, Decimal("100"), Decimal("50")))
    b.place_stop("AAA", Decimal("100"), Decimal("49.25"))  # pre-existing stop
    preview = Preview(nav=Decimal(100000), buying_power=Decimal(0), cash=Decimal(0), rows=[], orders=[])
    _reconcile_stops(b, preview, [], targets=[Target("AAA", Decimal("0.5"))])  # no stop_pct in feed
    assert "AAA" in b.open_stops() and len(b.open_stops()["AAA"]) == 1, \
        "held position's existing stop was wrongly stripped"
