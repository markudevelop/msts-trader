"""Post-trade verification — after a rebalance, prove the broker account actually reached the
target book.

First principle: a *converged* account is one where running the rebalance diff AGAIN produces no
orders — every leg is at target (within drift). So verification reuses the exact same diff engine
(`diff.build_preview`) against freshly-fetched post-fill positions; any residual order is a leg
that did NOT converge (partial fill, failed close, rejected order, or not-yet-settled).

This is broker-agnostic: it only consumes the Broker Protocol (balances/positions/quote) via the
Preview it is handed, so it works for every adapter (tastytrade, alpaca, ibkr, schwab, …).

`check_convergence` is pure (takes a Preview) and unit-tested; the broker round-trip lives in the
caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import Preview, RebalanceRow


@dataclass
class VerifyResult:
    ok: bool                       # True iff no leg still needs trading
    converged: int                 # legs at/within-drift of target
    residual: list[RebalanceRow]   # legs still off target (the diff would still trade them)
    nav: Decimal

    @property
    def residual_dollars(self) -> Decimal:
        return sum((abs(r.delta_dollars) for r in self.residual), Decimal(0))

    def summary(self) -> str:
        if self.ok:
            return f"✅ converged — {self.converged} leg(s) match target (within drift)"
        worst = sorted(self.residual, key=lambda r: -abs(r.delta_dollars))[:6]
        pct = (self.residual_dollars / self.nav) if self.nav else Decimal(0)
        legs = ", ".join(
            f"{r.ticker} {_reason(r)} Δ${r.delta_dollars:,.0f}" for r in worst
        )
        return (f"🔴 NOT converged — {len(self.residual)} leg(s) off target "
                f"({pct:.1%} of NAV): {legs}")


def _reason(r: RebalanceRow) -> str:
    """Why this leg is still off target — the unfilled side, or the diff's note."""
    if r.order is not None:
        return r.order.side.value
    return r.note or "off"


def check_convergence(post_fill_preview: Preview) -> VerifyResult:
    """Given a Preview built from POST-fill state with the SAME params as the rebalance,
    residual orders mark legs that did not reach target."""
    residual = [row for row in post_fill_preview.rows if row.order is not None]
    converged = sum(1 for row in post_fill_preview.rows if row.order is None)
    return VerifyResult(
        ok=not residual,
        converged=converged,
        residual=residual,
        nav=post_fill_preview.nav,
    )
