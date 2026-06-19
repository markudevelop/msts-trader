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


# ------------------------------------------------------------- stop carry ----

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
