"""Re-confirm pass: with real broker margin, scaling iterates until the
book actually fits (handles non-linear margin tiers)."""
from __future__ import annotations

from decimal import Decimal

from msts_trader import __main__ as m
from msts_trader.diff import build_preview
from msts_trader.models import Side, Target


class _TierBroker:
    """Reports margin = notional × rate, where the rate is higher while the
    book is large (a crude non-linear tier) and drops once it's smaller — so a
    single linear scale undershoots and a second pass is needed.
    """

    name = "fake"
    account_id = "X"

    def margin_requirement(self, orders):
        gross = sum((o.notional for o in orders if o.side == Side.BUY), Decimal(0))
        rate = Decimal("1.5") if gross > Decimal("70000") else Decimal("1.0")
        return gross * rate


def _preview():
    return build_preview(
        targets=[Target(ticker="SPY", weight=Decimal("1.0"))],
        positions={},
        nav=Decimal("100000"), cash=Decimal("100000"), buying_power=Decimal("100000"),
        quotes={"SPY": Decimal("500")},
    )


def test_reconfirm_runs_multiple_passes(monkeypatch):
    monkeypatch.setattr(m, "_QUIET", True)
    p = _preview()  # 200 sh SPY, $100k notional
    # pass 1: margin = 100k*1.5 = 150k > 97k avail -> scale ~0.647 -> ~$64.7k book
    # pass 2: now <70k so rate 1.0; margin 64.7k <= 97k -> fits, stop.
    m._apply_margin_aware(_TierBroker(), p, Decimal("100000"))
    gross = sum((o.notional for o in p.orders), Decimal(0))
    assert gross <= Decimal("100000")  # fits real buying power
    msgs = [w for w in p.warnings if "margin-aware" in w.lower()]
    assert msgs and "real broker margin" in msgs[-1]
    assert "passes" in msgs[-1]  # multi-pass cumulative message


def test_single_summary_message_only(monkeypatch):
    monkeypatch.setattr(m, "_QUIET", True)
    p = _preview()
    m._apply_margin_aware(_TierBroker(), p, Decimal("100000"))
    scale_msgs = [w for w in p.warnings if "scaled all buys" in w]
    assert len(scale_msgs) == 1  # exactly one cumulative message, not one per pass


def test_notional_broker_single_pass(monkeypatch):
    monkeypatch.setattr(m, "_QUIET", True)

    class _NoMargin:
        name = "n"
        account_id = "X"
        # no margin_requirement method -> notional path

    p = build_preview(
        targets=[Target(ticker="SPY", weight=Decimal("1.0")), Target(ticker="QQQ", weight=Decimal("0.6"))],
        positions={}, nav=Decimal("100000"), cash=Decimal("100000"), buying_power=Decimal("80000"),
        quotes={"SPY": Decimal("500"), "QQQ": Decimal("400")},
    )
    m._apply_margin_aware(_NoMargin(), p, Decimal("80000"))
    gross = sum((o.notional for o in p.orders), Decimal(0))
    assert gross <= Decimal("80000")
    msgs = [w for w in p.warnings if "scaled all buys" in w]
    assert len(msgs) == 1
    assert "estimated" in msgs[0] and "passes" not in msgs[0]  # single pass
