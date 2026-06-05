from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from msts_trader.models import Order, Side
from msts_trader.safety import check_max_notional, check_stale, gross_buy_notional, parse_asof


def _buy(tkr, notional):
    return Order(ticker=tkr, side=Side.BUY, quantity=Decimal("1"), estimated_price=Decimal("1"), notional=Decimal(str(notional)))


def test_gross_buy_only_counts_buys():
    orders = [_buy("A", 1000), Order(ticker="B", side=Side.SELL, quantity=Decimal("1"), notional=Decimal("500"))]
    assert gross_buy_notional(orders) == Decimal("1000")


def test_max_notional_blocks_when_over():
    msg = check_max_notional([_buy("A", 70000)], Decimal("60000"))
    assert msg and "exceed" in msg


def test_max_notional_ok_when_under():
    assert check_max_notional([_buy("A", 50000)], Decimal("60000")) is None


def test_max_notional_disabled_when_none():
    assert check_max_notional([_buy("A", 999999)], None) is None


def test_parse_asof_iso():
    dt = parse_asof("# asof: 2026-06-05T15:45:00Z\nticker,weight\nSPY,1.0\n")
    assert dt is not None and dt.year == 2026 and dt.month == 6


def test_parse_asof_absent():
    assert parse_asof("ticker,weight\nSPY,1.0\n") is None


def test_check_stale_blocks_old():
    old = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    msg = check_stale(f"# asof: {old}\nticker,weight\nSPY,1\n", 36)
    assert msg and "stale" in msg


def test_check_stale_ok_fresh():
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert check_stale(f"# asof: {fresh}\nticker,weight\nSPY,1\n", 36) is None


def test_check_stale_noop_without_asof():
    assert check_stale("ticker,weight\nSPY,1\n", 36) is None


def test_check_stale_noop_without_limit():
    old = (datetime.now(timezone.utc) - timedelta(hours=999)).isoformat()
    assert check_stale(f"# asof: {old}\nx,1\n", None) is None
