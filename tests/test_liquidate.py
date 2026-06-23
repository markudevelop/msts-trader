"""Liquidation planner + runner.

build_plan turns positions into close orders (longs -> SELL, shorts -> BUY,
largest first, with only/exclude filters). run_liquidation drives each order
through the chase engine; here it's exercised against a scripted FakeBroker with
sleep stubbed out, mirroring test_chase.py.
"""
from __future__ import annotations

from decimal import Decimal

from msts_trader.liquidate import build_plan, liquidation_config, run_liquidation
from msts_trader.models import Position, Side

NOSLEEP = lambda *a, **k: None  # noqa: E731


def _pos(ticker, qty, px):
    return Position(ticker=ticker, quantity=Decimal(str(qty)), price=Decimal(str(px)))


# --- build_plan ------------------------------------------------------------


def test_build_plan_longs_sell_shorts_buy_largest_first():
    positions = {
        "AAA": _pos("AAA", 10, 100),    # $1,000 long -> SELL
        "BBB": _pos("BBB", -5, 50),     # $250 short -> BUY to cover
        "CCC": _pos("CCC", 100, 30),    # $3,000 long -> SELL (largest)
    }
    plan = build_plan(positions)
    assert [o.ticker for o in plan.orders] == ["CCC", "AAA", "BBB"]  # largest notional first
    by = {o.ticker: o for o in plan.orders}
    assert by["AAA"].side == Side.SELL and by["AAA"].quantity == Decimal("10")
    assert by["BBB"].side == Side.BUY and by["BBB"].quantity == Decimal("5")
    assert plan.gross == Decimal("4250")


def test_build_plan_only_and_exclude():
    positions = {"AAA": _pos("AAA", 10, 100), "BBB": _pos("BBB", 5, 100), "CCC": _pos("CCC", 1, 100)}
    only = build_plan(positions, only=["aaa", "ccc"])
    assert {o.ticker for o in only.orders} == {"AAA", "CCC"}
    excl = build_plan(positions, exclude=["bbb"])
    assert {o.ticker for o in excl.orders} == {"AAA", "CCC"}
    assert ("BBB", "excluded") in excl.skipped


def test_build_plan_skips_flat_and_fractional_kept():
    positions = {"AAA": _pos("AAA", 0, 100), "DUST": _pos("DUST", "0.32", 95)}
    plan = build_plan(positions)
    assert [o.ticker for o in plan.orders] == ["DUST"]
    assert plan.orders[0].quantity == Decimal("0.32")
    assert ("AAA", "flat") in plan.skipped


# --- run_liquidation -------------------------------------------------------


class FakeBroker:
    """Fills every limit on the first rung; records calls."""

    name = "fake"
    supports_limit_chase = True
    supports_stops = False

    def __init__(self, mid="100"):
        self.mid = Decimal(mid)
        self.placed: list = []
        self.market_calls: list = []
        self._oid = 0

    def quote(self, tickers):
        return {list(tickers)[0]: self.mid}

    def place_limit(self, order, limit, dry_run=False):
        # Mirror the real tastytrade adapter: LIMIT orders are whole-share only,
        # so the quantity is floored. The fractional remainder is left for the
        # chase engine's market fallback.
        whole = int(Decimal(str(order.quantity)))
        if whole <= 0:
            return {"status": "skipped", "reason": "limit qty rounds to <1 share", "ticker": order.ticker}
        self._oid += 1
        oid = f"oid-{self._oid}"
        self.placed.append((order.ticker, float(whole), float(limit)))
        return {"status": "submitted", "order_id": oid, "ticker": order.ticker}

    def order_status(self, oid):
        return {"status": "filled", "filled_qty": self.placed[-1][1], "filled_avg_price": float(self.mid)}

    def place_market(self, order, dry_run=False):
        self.market_calls.append((order.ticker, float(order.quantity)))
        return {"status": "filled", "ticker": order.ticker, "order_id": "mkt"}

    def cancel_order(self, oid):
        return {"status": "CANCELLED", "order_id": oid}


def test_run_liquidation_fills_each_position():
    positions = {"AAA": _pos("AAA", 10, 100), "BBB": _pos("BBB", 20, 100)}
    plan = build_plan(positions)
    broker = FakeBroker()
    cfg = liquidation_config(retries=3, interval=1.0)
    results = run_liquidation(broker, plan, cfg, dry_run=False, sleep=NOSLEEP)
    assert len(results) == 2
    assert all(r["status"] == "FILLED" for r in results)
    # largest first: BBB before AAA
    assert [t for t, *_ in broker.placed] == ["BBB", "AAA"]


def test_run_liquidation_dry_run_sends_nothing():
    positions = {"AAA": _pos("AAA", 10, 100)}
    plan = build_plan(positions)
    broker = FakeBroker()
    results = run_liquidation(broker, plan, liquidation_config(), dry_run=True, sleep=NOSLEEP)
    assert results[0]["status"] == "dry-run"
    assert broker.placed == [] and broker.market_calls == []


def test_run_liquidation_fractional_remainder_goes_to_market():
    # 144.52 -> limit sells 144 whole shares, market mop-up sells 0.52
    positions = {"XLP": _pos("XLP", "144.52", 82)}
    plan = build_plan(positions)
    broker = FakeBroker(mid="82")
    results = run_liquidation(broker, plan, liquidation_config(retries=2, interval=1.0), dry_run=False, sleep=NOSLEEP)
    assert results[0].get("chase_fell_back") or results[0]["status"] == "FILLED"
    assert broker.placed[0][1] == 144.0  # limit was whole-share
    assert broker.market_calls and abs(broker.market_calls[0][1] - 0.52) < 1e-9


def test_liquidation_config_aggression_sign():
    # negative aggression = passive (rest above mid on a sell)
    cfg = liquidation_config(aggression=-0.001)
    assert cfg.aggression == Decimal("-0.001")
    assert cfg.fallback_to_market is True
    assert liquidation_config(fallback_to_market=False).fallback_to_market is False
