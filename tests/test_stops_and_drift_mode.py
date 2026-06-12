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
