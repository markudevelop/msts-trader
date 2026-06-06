"""Re-confirm pass: with real broker margin, scaling iterates until the
book actually fits (handles non-linear margin tiers)."""
from __future__ import annotations

from decimal import Decimal

from msts_trader import __main__ as m
from msts_trader.diff import build_preview
from msts_trader.models import Side, Target


class _TierBroker:
    """Realistic non-linear margin: a long position's margin requirement is a
    fraction of notional (<= 1.0), and the rate is higher for the larger book
    and lower once it shrinks past a tier boundary — so the first linear scale
    (using the high rate) over-shoots and a confirming second pass runs.
    """

    name = "fake"
    account_id = "X"

    def margin_requirement(self, orders):
        gross = sum((o.notional for o in orders if o.side == Side.BUY), Decimal(0))
        rate = Decimal("0.9") if gross > Decimal("110000") else Decimal("0.6")
        return gross * rate


def _preview():
    # Notional $200k > BP $100k so the notional pre-check does NOT short-circuit
    # and the real-margin path runs.
    return build_preview(
        targets=[Target(ticker="SPY", weight=Decimal("2.0"))],
        positions={},
        nav=Decimal("100000"), cash=Decimal("100000"), buying_power=Decimal("100000"),
        quotes={"SPY": Decimal("500")},
    )


def test_reconfirm_runs_multiple_passes(monkeypatch):
    monkeypatch.setattr(m, "_QUIET", True)
    p = _preview()  # 400 sh SPY, $200k notional
    before = sum((o.notional for o in p.orders), Decimal(0))
    m._apply_margin_aware(_TierBroker(), p, Decimal("100000"))
    after = sum((o.notional for o in p.orders), Decimal(0))
    assert after < before  # buys were scaled down
    msgs = [w for w in p.warnings if "scaled all buys" in w]
    assert msgs and "real broker margin" in msgs[-1]
    assert "passes" in msgs[-1]  # multi-pass cumulative message


def test_single_summary_message_only(monkeypatch):
    monkeypatch.setattr(m, "_QUIET", True)
    p = _preview()
    m._apply_margin_aware(_TierBroker(), p, Decimal("100000"))
    scale_msgs = [w for w in p.warnings if "scaled all buys" in w]
    assert len(scale_msgs) == 1  # exactly one cumulative message, not one per pass


def test_no_broker_query_when_notional_fits(monkeypatch):
    # Default-on margin-aware must cost nothing when the book already fits:
    # the per-order margin dry-run must NOT be called.
    monkeypatch.setattr(m, "_QUIET", True)

    class _CountingBroker:
        name = "c"
        account_id = "X"

        def __init__(self):
            self.calls = 0

        def margin_requirement(self, orders):
            self.calls += 1
            return Decimal("0")

    b = _CountingBroker()
    p = build_preview(
        targets=[Target(ticker="SPY", weight=Decimal("0.5"))],
        positions={}, nav=Decimal("100000"), cash=Decimal("100000"), buying_power=Decimal("100000"),
        quotes={"SPY": Decimal("500")},
    )  # $50k buys, $100k BP -> fits
    m._apply_margin_aware(b, p, Decimal("100000"))
    assert b.calls == 0  # no margin dry-run when it obviously fits
    assert not any("margin-aware" in w.lower() for w in p.warnings)


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
