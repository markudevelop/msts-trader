"""IBKR margin_requirement via a faked ib_insync socket (no live connection)."""
from __future__ import annotations

import pytest

pytest.importorskip("ib_insync")  # optional dep — skip when the SDK is not installed

from decimal import Decimal
from types import SimpleNamespace

from msts_trader.brokers.ibkr import IBKR
from msts_trader.models import Order, Side


class _FakeIB:
    def __init__(self, margins, fail=False):
        self._margins = list(margins)  # initMarginChange per whatIf call
        self._fail = fail
        self._i = 0

    def qualifyContracts(self, *a, **k):
        return None

    def whatIfOrder(self, contract, order):
        if self._fail:
            raise RuntimeError("what-if failed")
        m = self._margins[self._i]
        self._i += 1
        return SimpleNamespace(initMarginChange=m)


def _ibkr(fake):
    b = IBKR.__new__(IBKR)
    b._ib = fake
    b.account_id = "U1"
    return b


def test_margin_requirement_sums_init_margin():
    b = _ibkr(_FakeIB(["5000", "9000"]))  # TBT-style high margin
    buys = [
        Order(ticker="SPY", side=Side.BUY, quantity=Decimal("10")),
        Order(ticker="TBT", side=Side.BUY, quantity=Decimal("100")),
        Order(ticker="GLD", side=Side.SELL, quantity=Decimal("5")),  # ignored
    ]
    assert b.margin_requirement(buys) == Decimal("14000")


def test_margin_requirement_none_on_failure():
    b = _ibkr(_FakeIB([], fail=True))
    assert b.margin_requirement([Order(ticker="SPY", side=Side.BUY, quantity=Decimal("1"))]) is None


def test_margin_requirement_none_when_field_missing():
    b = _ibkr(_FakeIB([float("nan")]))  # nan -> _f returns None -> overall None
    assert b.margin_requirement([Order(ticker="SPY", side=Side.BUY, quantity=Decimal("1"))]) is None
