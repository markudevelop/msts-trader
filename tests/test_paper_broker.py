from __future__ import annotations

from decimal import Decimal

import pytest

from msts_trader.brokers import make
from msts_trader.brokers.base import Broker
from msts_trader.brokers.paper import Paper
from msts_trader.models import Order, Side


def test_implements_broker_protocol():
    p = Paper(starting_cash="10000")
    assert isinstance(p, Broker)
    assert p.name == "paper"
    assert p.account_id == "PAPER"
    assert p.supports_fractional is True


def test_initial_balances():
    p = Paper(starting_cash="75000")
    b = p.balances()
    assert b.cash == Decimal("75000")
    assert b.nav == Decimal("75000")
    assert b.buying_power == Decimal("75000")
    assert p.positions() == {}


def test_buy_decrements_cash_and_creates_position():
    p = Paper(starting_cash="50000")
    p.set_quote("SPY", Decimal("500"))
    o = Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10"), estimated_price=Decimal("500"))
    r = p.place_market(o)
    assert r["status"] == "FILLED"

    b = p.balances()
    assert b.cash == Decimal("45000")  # 50k - (10 * 500)
    pos = p.positions()
    assert pos["SPY"].quantity == Decimal("10")
    assert b.nav == Decimal("50000")  # 45k cash + 10 * 500 mv


def test_sell_credits_cash_and_reduces_position():
    p = Paper(starting_cash="50000")
    p.set_quote("SPY", Decimal("500"))
    p.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10"), estimated_price=Decimal("500")))
    p.place_market(Order(ticker="SPY", side=Side.SELL, quantity=Decimal("4"), estimated_price=Decimal("510")))

    b = p.balances()
    # 50k - 5000 (buy) + 2040 (sell 4 @ 510) = 47,040
    assert b.cash == Decimal("47040")
    assert p.positions()["SPY"].quantity == Decimal("6")


def test_sell_to_zero_removes_position():
    p = Paper(starting_cash="50000")
    p.set_quote("SPY", Decimal("500"))
    p.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10"), estimated_price=Decimal("500")))
    p.place_market(Order(ticker="SPY", side=Side.SELL, quantity=Decimal("10"), estimated_price=Decimal("510")))
    assert "SPY" not in p.positions()


def test_buy_rejects_when_insufficient_cash():
    p = Paper(starting_cash="500")
    r = p.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10"), estimated_price=Decimal("500")))
    assert r["status"] == "error"
    assert "insufficient cash" in r["reason"]
    assert "SPY" not in p.positions()


def test_sell_rejects_when_insufficient_position():
    p = Paper(starting_cash="50000")
    r = p.place_market(Order(ticker="SPY", side=Side.SELL, quantity=Decimal("5"), estimated_price=Decimal("500")))
    assert r["status"] == "error"
    assert "insufficient SPY" in r["reason"]


def test_dry_run_does_not_mutate():
    p = Paper(starting_cash="50000")
    p.set_quote("SPY", Decimal("500"))
    r = p.place_market(
        Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10"), estimated_price=Decimal("500")),
        dry_run=True,
    )
    assert r["status"] == "dry-run"
    assert r["dry_run"] is True
    assert p.balances().cash == Decimal("50000")
    assert p.positions() == {}


def test_state_persists_across_instances(isolate_paper_state):
    p1 = Paper(starting_cash="50000")
    p1.set_quote("SPY", Decimal("500"))
    p1.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal("5"), estimated_price=Decimal("500")))

    p2 = Paper()
    assert p2.balances().cash == Decimal("47500")
    assert p2.positions()["SPY"].quantity == Decimal("5")


def test_make_factory_returns_paper():
    p = make("paper", starting_cash="42000")
    assert p.name == "paper"
    assert p.balances().cash == Decimal("42000")


def test_quote_returns_last_seen_price():
    p = Paper(starting_cash="50000")
    p.set_quote("SPY", Decimal("499"))
    assert p.quote(["spy", "GLD"]) == {"SPY": Decimal("499")}


@pytest.mark.parametrize("qty", ["0", "-1"])
def test_rejects_zero_or_negative_qty(qty):
    p = Paper(starting_cash="10000")
    r = p.place_market(Order(ticker="SPY", side=Side.BUY, quantity=Decimal(qty), estimated_price=Decimal("500")))
    assert r["status"] == "skipped"


def test_lowercase_order_ticker_is_normalized():
    # A lowercase ticker must book under the uppercase key so positions()
    # finds its price in last_prices (which quote()/set_quote() uppercase).
    p = Paper(starting_cash="50000")
    p.set_quote("SPY", Decimal("500"))
    r = p.place_market(Order(ticker="spy", side=Side.BUY, quantity=Decimal("10"), estimated_price=Decimal("500")))
    assert r["status"] == "FILLED" and r["ticker"] == "SPY"
    pos = p.positions()
    assert set(pos) == {"SPY"}
    assert pos["SPY"].price == Decimal("500")
    assert p.balances().nav == Decimal("50000")
