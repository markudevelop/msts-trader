"""Limit-chase engine + adapter primitives.

The engine (msts_trader/chase.py) is broker-agnostic, so most paths are
exercised against a scripted FakeBroker with sleep stubbed out. Paper-broker
tests cover the real simulated-fill primitives, and a small structural check
guards the protocol contract (a broker that declares supports_limit_chase must
implement place_limit + order_status).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from msts_trader import config
from msts_trader.chase import (
    FILLED,
    PARTIAL,
    UNKNOWN,
    WORKING,
    ChaseConfig,
    chase_fill,
    limit_from_mid,
)
from msts_trader.models import Order, Side

NOSLEEP = lambda *a, **k: None  # noqa: E731


def _order(ticker="SPY", side=Side.BUY, qty="10", px="100"):
    return Order(ticker=ticker, side=side, quantity=Decimal(qty), estimated_price=Decimal(px))


def _fast_cfg(**kw):
    base = dict(retries=5, reprice_interval=1.0, poll_interval=1.0)
    base.update(kw)
    return ChaseConfig(**base)


class FakeBroker:
    """Scripts fills per rung: an order placed on rung N reports FILLED once
    N >= fill_attempt; `partials` injects a partial fill on a given rung."""

    name = "fake"
    supports_limit_chase = True

    def __init__(self, *, mid="100", fill_attempt=None, partials=None,
                 cancel_ok=True, quote_none=False, market_status="FILLED"):
        self.mid = Decimal(mid)
        self.fill_attempt = fill_attempt
        self.partials = partials or {}
        self.cancel_ok = cancel_ok
        self.quote_none = quote_none
        self.market_status = market_status
        self.attempt = 0
        self.attempt_of: dict[str, int] = {}
        self.rung_qty: dict[str, Decimal] = {}
        self.placed: list = []
        self.cancelled: list = []
        self.market_calls: list = []

    def quote(self, tickers):
        return {} if self.quote_none else {list(tickers)[0]: self.mid}

    def place_limit(self, order, limit, dry_run=False):
        self.attempt += 1
        oid = f"oid-{self.attempt}"
        self.attempt_of[oid] = self.attempt
        self.rung_qty[oid] = Decimal(str(order.quantity))
        self.placed.append((oid, float(order.quantity), float(limit)))
        return {"status": "submitted", "order_id": oid, "ticker": order.ticker}

    def order_status(self, oid):
        a = self.attempt_of.get(oid)
        if a is None:  # cancelled / unknown — no (further) fills
            return {"status": UNKNOWN, "filled_qty": 0.0, "filled_avg_price": None}
        rq = self.rung_qty[oid]
        if self.fill_attempt is not None and a >= self.fill_attempt:
            return {"status": FILLED, "filled_qty": float(rq), "filled_avg_price": float(self.mid)}
        if a in self.partials:
            return {"status": PARTIAL, "filled_qty": float(self.partials[a]),
                    "filled_avg_price": float(self.mid)}
        return {"status": WORKING, "filled_qty": 0.0, "filled_avg_price": None}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        if not self.cancel_ok:
            return {"status": "error", "order_id": oid}
        self.attempt_of.pop(oid, None)  # cancelled orders stop reporting fills
        return {"status": "CANCELLED", "order_id": oid}

    def place_market(self, order, dry_run=False):
        self.market_calls.append(float(order.quantity))
        return {"status": self.market_status, "ticker": order.ticker,
                "quantity": float(order.quantity), "fill_price": float(self.mid), "order_id": "mkt"}


# ---- pure price helper ---------------------------------------------------
def test_limit_from_mid_pegs_and_nudges():
    assert limit_from_mid(Side.BUY, Decimal("100"), Decimal("0")) == Decimal("100.00")
    assert limit_from_mid(Side.SELL, Decimal("100"), Decimal("0")) == Decimal("100.00")
    # BUY pays up, SELL gives up
    assert limit_from_mid(Side.BUY, Decimal("100"), Decimal("0.01")) == Decimal("101.00")
    assert limit_from_mid(Side.SELL, Decimal("100"), Decimal("0.01")) == Decimal("99.00")


# ---- engine paths --------------------------------------------------------
def test_fills_on_first_rung_no_cancel_no_market():
    b = FakeBroker(fill_attempt=1)
    res = chase_fill(b, _order(qty="10"), _fast_cfg(), sleep=NOSLEEP)
    assert res["status"] == "FILLED"
    assert res["quantity"] == 10.0
    assert res["fill_price"] == 100.0
    assert len(b.placed) == 1
    assert b.cancelled == []        # a filled order is never cancelled
    assert b.market_calls == []     # no fallback needed


def test_reprices_then_fills_on_later_rung():
    b = FakeBroker(fill_attempt=3)
    res = chase_fill(b, _order(qty="10"), _fast_cfg(), sleep=NOSLEEP)
    assert res["status"] == "FILLED"
    assert res["quantity"] == 10.0
    assert len(b.placed) == 3       # repriced twice before filling
    assert len(b.cancelled) == 2    # each unfilled rung cancelled before reprice
    assert b.market_calls == []


def test_partial_then_fill_resubmits_only_remainder():
    b = FakeBroker(fill_attempt=2, partials={1: 3})
    res = chase_fill(b, _order(qty="10"), _fast_cfg(), sleep=NOSLEEP)
    assert res["status"] == "FILLED"
    assert res["quantity"] == 10.0
    # rung 2 is placed for the 7-share remainder, not the full 10
    assert b.placed[1][1] == 7.0


def test_exhausts_then_market_fallback():
    b = FakeBroker(fill_attempt=None)
    res = chase_fill(b, _order(qty="10"), _fast_cfg(retries=2), sleep=NOSLEEP)
    assert b.market_calls == [10.0]
    assert res.get("chase_fell_back") is True
    assert res["status"] == "FILLED"  # the fallback market filled it


def test_partial_then_fallback_for_remainder():
    b = FakeBroker(fill_attempt=None, partials={1: 4, 2: 4})
    res = chase_fill(b, _order(qty="10"), _fast_cfg(retries=2), sleep=NOSLEEP)
    # 4 filled on rung 1, then the 6-share remainder; rung 2 fills 4 more,
    # leaving 2 for the market fallback.
    assert b.market_calls == [2.0]
    assert res.get("chase_limit_filled") == 8.0


def test_no_fallback_unfilled_is_error():
    b = FakeBroker(fill_attempt=None)
    res = chase_fill(b, _order(qty="10"), _fast_cfg(retries=2, fallback_to_market=False),
                     sleep=NOSLEEP)
    assert res["status"] == "error"
    assert b.market_calls == []


def test_no_fallback_partial_reports_partial():
    b = FakeBroker(fill_attempt=None, partials={1: 3})
    res = chase_fill(b, _order(qty="10"), _fast_cfg(retries=1, fallback_to_market=False),
                     sleep=NOSLEEP)
    assert res["status"] == "PARTIAL"
    assert res["quantity"] == 3.0


def test_cancel_failure_aborts_to_avoid_double_fill():
    b = FakeBroker(fill_attempt=None, cancel_ok=False)
    res = chase_fill(b, _order(qty="10"), _fast_cfg(retries=3), sleep=NOSLEEP)
    assert res["status"] == "error"
    assert "double-fill" in res["reason"]
    assert b.market_calls == []     # never falls back after an abort


def test_no_quote_falls_through_to_market():
    b = FakeBroker(quote_none=True)
    chase_fill(b, _order(qty="10"), _fast_cfg(retries=3), sleep=NOSLEEP)
    assert b.placed == []           # never placed a limit without a price
    assert b.market_calls == [10.0]


def test_dry_run_shows_single_initial_limit_no_orders():
    b = FakeBroker(fill_attempt=1)
    res = chase_fill(b, _order(qty="10"), _fast_cfg(), dry_run=True, sleep=NOSLEEP)
    assert res["status"] == "dry-run"
    assert res["limit_price"] == 100.0   # the one initial limit at the current mid
    assert b.placed == []                # nothing sent
    assert b.market_calls == []


def test_dry_run_no_quote_reports_would_market():
    b = FakeBroker(quote_none=True)
    res = chase_fill(b, _order(qty="10"), _fast_cfg(), dry_run=True, sleep=NOSLEEP)
    assert res["status"] == "dry-run"
    assert "market" in res["reason"]
    assert b.placed == []


def test_missing_order_id_aborts():
    b = FakeBroker(fill_attempt=None)
    b.place_limit = lambda order, limit, dry_run=False: {"status": "submitted", "ticker": order.ticker}
    res = chase_fill(b, _order(qty="10"), _fast_cfg(), sleep=NOSLEEP)
    assert res["status"] == "error"
    assert "order_id" in res["reason"]
    assert b.market_calls == []          # never falls back to market when unmanageable


def test_overreported_fill_is_clamped_to_qty():
    # broker reports filling 50 on a 10-share order; total must not exceed 10
    b = FakeBroker(fill_attempt=1)
    b.rung_qty_override = Decimal("50")
    orig = b.order_status
    b.order_status = lambda oid: {**orig(oid), "filled_qty": 50.0}
    res = chase_fill(b, _order(qty="10"), _fast_cfg(), sleep=NOSLEEP)
    assert res["status"] == "FILLED"
    assert res["quantity"] == 10.0       # clamped, not 50


def test_zero_qty_skipped():
    b = FakeBroker(fill_attempt=1)
    res = chase_fill(b, _order(qty="0"), _fast_cfg(), sleep=NOSLEEP)
    assert res["status"] == "skipped"


# ---- paper broker primitives --------------------------------------------
def test_paper_marketable_limit_fills_immediately():
    from msts_trader.brokers.paper import Paper

    p = Paper()
    p.set_quote("SPY", Decimal("100"))
    res = chase_fill(p, _order("SPY", Side.BUY, "5"), _fast_cfg(reprice_interval=0.01,
                                                               poll_interval=0.01))
    assert res["status"] == "FILLED"
    assert p.positions()["SPY"].quantity == Decimal("5")


def test_paper_resting_limit_fills_when_mid_moves():
    from msts_trader.brokers.paper import Paper

    p = Paper()
    p.set_quote("SPY", Decimal("100"))
    placed = p.place_limit(_order("SPY", Side.BUY, "5", px="99"), Decimal("99"))
    oid = placed["order_id"]
    assert p.order_status(oid)["status"] == WORKING   # 99 < 100 mid: not marketable
    p.set_quote("SPY", Decimal("98"))                 # mid drops below the limit
    st = p.order_status(oid)
    assert st["status"] == FILLED
    assert p.positions()["SPY"].quantity == Decimal("5")


def test_paper_cancel_removes_resting_limit():
    from msts_trader.brokers.paper import Paper

    p = Paper()
    p.set_quote("SPY", Decimal("100"))
    placed = p.place_limit(_order("SPY", Side.BUY, "5", px="90"), Decimal("90"))
    oid = placed["order_id"]
    assert p.cancel_order(oid)["status"] == "CANCELLED"
    assert p.order_status(oid)["status"] == UNKNOWN


# ---- config + protocol ---------------------------------------------------
def test_config_accepts_chase_keys(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(
        'order_type = "limit-chase"\n'
        "chase_retries = 8\n"
        "chase_interval = 3.0\n"
        "chase_poll = 0.5\n"
        "chase_aggression = 0.001\n"
        "chase_fallback = false\n"
    )
    cfg = config.load(f)
    assert cfg["order_type"] == "limit-chase"
    assert cfg["chase_retries"] == 8


@pytest.mark.parametrize("modpath,clsname", [
    ("msts_trader.brokers.paper", "Paper"),
    ("msts_trader.brokers.tastytrade", "Tastytrade"),
    ("msts_trader.brokers.alpaca", "Alpaca"),
    ("msts_trader.brokers.tradier", "Tradier"),
    ("msts_trader.brokers.ibkr", "IBKR"),
    ("msts_trader.brokers.schwab", "Schwab"),
    ("msts_trader.brokers.hyperliquid", "Hyperliquid"),
])
def test_chase_capable_brokers_implement_contract(modpath, clsname):
    import importlib

    cls = getattr(importlib.import_module(modpath), clsname)
    assert cls.supports_limit_chase is True
    for method in ("place_limit", "order_status", "cancel_order"):
        assert callable(getattr(cls, method, None)), f"{clsname} missing {method}"


def test_every_chase_broker_declared_in_audit():
    """Every adapter that declares supports_limit_chase must implement all three
    chase methods. If a future adapter sets the flag but forgets a method (the
    protocol is structural — missing methods only fail at runtime), this trips."""
    from msts_trader.brokers import SUPPORTED

    missing = []
    for name in SUPPORTED:
        # find the class without instantiating (no creds / SDK needed)
        mod = __import__(f"msts_trader.brokers.{name}", fromlist=["x"])
        cls = next(v for v in vars(mod).values()
                   if isinstance(v, type) and getattr(v, "name", None) == name)
        if not getattr(cls, "supports_limit_chase", False):
            continue
        for m in ("place_limit", "order_status", "cancel_order"):
            if not callable(getattr(cls, m, None)):
                missing.append(f"{name}.{m}")
    assert not missing, f"chase-capable brokers missing methods: {missing}"


# ---- order_status normalization for SDK-gated adapters (no SDK needed) ----
def test_ibkr_order_status_normalizes():
    from types import SimpleNamespace

    from msts_trader.brokers.ibkr import IBKR
    from msts_trader.chase import FILLED, PARTIAL, WORKING

    def trade(status, filled, remaining, avg, perm="P1"):
        return SimpleNamespace(
            order=SimpleNamespace(permId=perm, orderId=1),
            orderStatus=SimpleNamespace(status=status, filled=filled, remaining=remaining,
                                        avgFillPrice=avg))

    def status_of(t):
        b = IBKR.__new__(IBKR)
        b._ib = SimpleNamespace(trades=lambda: [t])
        return b.order_status("P1")

    assert status_of(trade("Filled", 3, 0, 100.0))["status"] == FILLED
    assert status_of(trade("Submitted", 1, 2, 100.0))["status"] == PARTIAL  # partial = working+filled
    assert status_of(trade("PreSubmitted", 0, 3, 0))["status"] == WORKING


def test_schwab_order_status_normalizes():
    from types import SimpleNamespace

    from msts_trader.brokers.schwab import Schwab
    from msts_trader.chase import FILLED, PARTIAL, WORKING

    def status_of(payload):
        b = Schwab.__new__(Schwab)
        b._account_hash = "H"
        b._client = SimpleNamespace(
            get_order=lambda oid, h: SimpleNamespace(raise_for_status=lambda: None,
                                                     json=lambda: payload))
        return b.order_status("1")

    f = status_of({"status": "FILLED", "filledQuantity": 3,
                   "orderActivityCollection": [{"executionLegs": [{"quantity": 3, "price": 100.0}]}]})
    assert f["status"] == FILLED and f["filled_avg_price"] == 100.0
    assert status_of({"status": "WORKING", "filledQuantity": 1})["status"] == PARTIAL
    assert status_of({"status": "WORKING", "filledQuantity": 0})["status"] == WORKING


def test_hyperliquid_order_status_normalizes():
    from types import SimpleNamespace

    from msts_trader.brokers.hyperliquid import Hyperliquid
    from msts_trader.chase import FILLED, PARTIAL, WORKING

    def status_of(res):
        b = Hyperliquid.__new__(Hyperliquid)
        b._address = "0xabc"
        b._info = SimpleNamespace(query_order_by_oid=lambda addr, oid: res)
        return b.order_status("7")

    def wrap(inner, orig, rem):
        return {"order": {"status": inner, "order": {"origSz": orig, "sz": rem}}}

    assert status_of(wrap("filled", 3, 0))["status"] == FILLED
    assert status_of(wrap("open", 3, 1))["status"] == PARTIAL
    assert status_of(wrap("open", 3, 3))["status"] == WORKING


# ---- _execute routing ----------------------------------------------------
def _preview(orders):
    from msts_trader.models import Preview

    return Preview(nav=Decimal(0), buying_power=Decimal(0), cash=Decimal(0),
                   rows=[], orders=orders)


def test_execute_unsupported_broker_falls_back_to_market():
    from msts_trader.__main__ import _execute

    class MiniBroker:
        name = "mini"
        supports_stops = False
        supports_limit_chase = False

        def __init__(self):
            self.market = []

        def place_market(self, o, dry_run=False):
            self.market.append(o.ticker)
            return {"status": "FILLED", "ticker": o.ticker, "order_id": "m1"}

    b = MiniBroker()
    sent, failed, _ = _execute(b, _preview([_order("SPY", Side.BUY, "3")]),
                               order_type="limit-chase", chase_cfg=_fast_cfg())
    assert b.market == ["SPY"]      # warned + used market
    assert (sent, failed) == (1, 0)


def test_execute_routes_through_chase_on_supported_broker():
    from msts_trader.__main__ import _execute
    from msts_trader.brokers.paper import Paper

    p = Paper()
    p.set_quote("SPY", Decimal("100"))
    cfg = _fast_cfg(reprice_interval=0.01, poll_interval=0.01)
    sent, failed, results = _execute(p, _preview([_order("SPY", Side.BUY, "4")]),
                                     order_type="limit-chase", chase_cfg=cfg)
    assert (sent, failed) == (1, 0)
    assert results[0].get("chase") is True
    assert p.positions()["SPY"].quantity == Decimal("4")


# ---- P1: partial fill must survive a failed market fallback --------------
def test_partial_then_fallback_failure_preserves_fill_info():
    b = FakeBroker(fill_attempt=None, partials={1: 4}, market_status="error")
    res = chase_fill(b, _order(qty="10"), _fast_cfg(retries=1), sleep=NOSLEEP)
    assert res["status"] == "error"           # the leg did not complete
    assert res["chase_limit_filled"] == 4.0   # but 4 shares filled via the chase
    assert res["fill_price"] == 100.0         # carry an anchor so a stop can be placed


def test_reconcile_stops_protects_partial_chase_fill_on_error():
    from decimal import Decimal as D

    from msts_trader.__main__ import _reconcile_stops
    from msts_trader.models import Order, Position, Preview, Side

    class StopBroker:
        name = "sb"
        supports_stops = True

        def __init__(self, held):
            self._held = held
            self.placed = []

        def open_stops(self):
            return {}

        def positions(self):
            return self._held

        def place_stop(self, tkr, qty, stop_price, dry_run=False):
            self.placed.append((tkr, qty, float(stop_price)))
            return {"status": "ACCEPTED", "ticker": tkr}

        def cancel_order(self, oid):
            return {"status": "CANCELLED"}

    o = Order(ticker="SPY", side=Side.BUY, quantity=D("10"),
              estimated_price=D("100"), stop_pct=D("0.02"))
    preview = Preview(nav=D(0), buying_power=D(0), cash=D(0), rows=[], orders=[o])
    results = [{"ticker": "SPY", "status": "error", "chase_limit_filled": 4.0,
                "fill_price": 100.0, "reason": "fallback failed"}]

    # held 4 shares -> stop placed on the 4 actually held, 2% below 100
    b = StopBroker({"SPY": Position(ticker="SPY", quantity=D("4"), price=D("100"))})
    _reconcile_stops(b, preview, results)
    assert b.placed == [("SPY", D("4"), 98.0)]

    # nothing actually held -> NO stop (never protect phantom shares)
    b2 = StopBroker({})
    _reconcile_stops(b2, preview, results)
    assert b2.placed == []


# ---- P1: IBKR cancel must match permId or orderId ------------------------
def test_ibkr_cancel_matches_permid_and_orderid():
    from types import SimpleNamespace

    from msts_trader.brokers.ibkr import IBKR

    cancelled = []
    trade = SimpleNamespace(order=SimpleNamespace(permId="P9", orderId=42))
    b = IBKR.__new__(IBKR)
    b._ib = SimpleNamespace(openTrades=lambda: [trade],
                            cancelOrder=lambda o: cancelled.append(o),
                            sleep=lambda s: None)
    assert b.cancel_order("P9")["status"] == "CANCELLED"   # by permId (chase id)
    assert b.cancel_order("42")["status"] == "CANCELLED"   # by orderId (stop id)
    assert b.cancel_order("nope")["status"] == "error"
    assert len(cancelled) == 2


# ---- P2: multi / _rebalance_one must honor order_type --------------------
def test_rebalance_one_routes_order_type_to_execute(monkeypatch):
    from decimal import Decimal as D

    from msts_trader import __main__ as m
    from msts_trader.brokers.paper import Paper
    from msts_trader.models import Target

    p = Paper()
    p.set_quote("SPY", D("100"))
    captured = {}
    monkeypatch.setattr(m, "_execute", lambda b, preview, *, order_type="market", chase_cfg=None, targets=None:
                        (captured.update(order_type=order_type, cfg=chase_cfg) or (1, 0, [])))
    cfg = _fast_cfg()
    r = m._rebalance_one(p, [Target(ticker="SPY", weight=D("1.0"))],
                         threshold=0.0, max_notional=None, dry_run=False, force=True,
                         order_type="limit-chase", chase_cfg=cfg)
    assert captured["order_type"] == "limit-chase"
    assert captured["cfg"] is cfg
    assert r["status"] in ("executed", "partial")


def test_multi_routes_config_order_type(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from msts_trader import __main__ as m

    captured = {}

    def fake_rebalance_one(b, targets, **kw):
        captured["order_type"] = kw.get("order_type")
        captured["chase_cfg"] = kw.get("chase_cfg")
        return {"broker": b.name, "status": "executed", "sent": 1, "failed": 0}

    monkeypatch.setattr(m, "_rebalance_one", fake_rebalance_one)
    monkeypatch.setattr(m, "make", lambda name, **kw: type("B", (), {"name": name, "account_id": "X"})())
    monkeypatch.setattr(m, "broker_kwargs_from_env", lambda name: {})

    csv = tmp_path / "w.csv"
    csv.write_text("ticker,weight\nSPY,1.0\n")
    conf = tmp_path / "multi.toml"
    conf.write_text(
        'order_type = "limit-chase"\nchase_retries = 9\n'
        f'csv_file = "{csv.as_posix()}"\n'
        '[[account]]\nname = "a1"\nbroker = "paper"\n'
    )
    r = CliRunner().invoke(m.main, ["multi", "--config", str(conf), "--yes"])
    assert r.exit_code == 0, r.output
    assert captured["order_type"] == "limit-chase"
    assert captured["chase_cfg"].retries == 9
