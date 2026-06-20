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

from .models import Preview, RebalanceRow, Side


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


def converged_within_buying_power(post_fill_preview: Preview) -> VerifyResult:
    """Like `check_convergence`, but a residual BUY that current buying power cannot
    fund is treated as CONVERGED — the book is as-deployed-as-possible, not a failure.

    Without this, a fully-invested / margin-limited book reads "not converged"
    forever: the post-trade diff wants the full target, but margin-aware had scaled
    the buys down at execution, so the gap never closes and self-heal re-submits
    unaffordable buys every pass.

    Rule (a yes/no convergence check, not execution sizing): fund the largest
    residual buys first from available buying power (broker BP + residual sell
    proceeds). A buy that can't be funded from what remains is at its max
    deployable size → EXCUSE it (drop its order, mark the row converged). A buy
    that still fits means more *could* be deployed → it stays a real residual.
    SELL residuals (a failed close still held) are NEVER excused. Mutates the
    preview: excused orders are removed from both `rows` and `orders`, so self-heal
    won't re-submit a buy the account cannot afford.
    """
    sell_proceeds = sum((row.order.notional for row in post_fill_preview.rows
                         if row.order is not None and row.order.side == Side.SELL), Decimal(0))
    available = post_fill_preview.buying_power + sell_proceeds
    buy_rows = [row for row in post_fill_preview.rows
                if row.order is not None and row.order.side == Side.BUY]
    excused: list = []
    for row in sorted(buy_rows, key=lambda r: -r.order.notional):
        if row.order.notional <= available:
            available -= row.order.notional   # affordable → genuine residual, keep it
        else:
            excused.append(row.order)
            row.order = None
            row.note = "within drift (buying-power limited)"
    if excused:
        ex_ids = {id(o) for o in excused}
        post_fill_preview.orders = [o for o in post_fill_preview.orders if id(o) not in ex_ids]
    return check_convergence(post_fill_preview)
