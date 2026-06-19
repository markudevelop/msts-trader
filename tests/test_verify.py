"""Tests for post-trade convergence verification (msts_trader.verify)."""
from decimal import Decimal

from msts_trader.models import Order, Preview, RebalanceRow, Side
from msts_trader.verify import check_convergence


def _preview(rows):
    return Preview(nav=Decimal("100000"), buying_power=Decimal("100000"),
                   cash=Decimal("0"), rows=rows, orders=[r.order for r in rows if r.order])


def _row(ticker, *, order=None, note=""):
    return RebalanceRow(ticker=ticker, current_pct=Decimal("0"), target_pct=Decimal("0.1"),
                        delta_dollars=Decimal("0") if order is None else order.notional,
                        order=order, note=note)


def _order(ticker, side, dollars):
    return Order(ticker=ticker, side=side, quantity=Decimal("1"),
                 estimated_price=Decimal(str(dollars)), notional=Decimal(str(dollars)))


def test_converged_when_no_residual_orders():
    # every leg within drift -> no orders -> converged
    res = check_convergence(_preview([_row("SPY", note="within drift"),
                                      _row("IWM", note="within drift")]))
    assert res.ok is True
    assert res.converged == 2
    assert res.residual == []
    assert "converged" in res.summary()


def test_residual_buy_flags_not_converged():
    # a leg the diff still wants to BUY = partial fill / not converged
    rows = [_row("SPY", note="within drift"),
            _row("IWM", order=_order("IWM", Side.BUY, 8000))]
    res = check_convergence(_preview(rows))
    assert res.ok is False
    assert len(res.residual) == 1 and res.residual[0].ticker == "IWM"
    assert res.residual_dollars == Decimal("8000")
    assert "NOT converged" in res.summary() and "IWM" in res.summary()


def test_residual_sell_flags_failed_close():
    # a leg the diff still wants to SELL = a failed close still held
    rows = [_row("XLP", order=_order("XLP", Side.SELL, 28000))]
    res = check_convergence(_preview(rows))
    assert res.ok is False
    assert res.residual[0].order.side == Side.SELL
    assert "BUY" not in res.summary()  # it's a SELL residual


def test_residual_dollars_sums_abs():
    rows = [_row("A", order=_order("A", Side.BUY, 5000)),
            _row("B", order=_order("B", Side.SELL, 3000))]
    res = check_convergence(_preview(rows))
    assert res.residual_dollars == Decimal("8000")
    assert res.ok is False
