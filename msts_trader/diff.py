"""Build a Preview from targets + current positions + balances + quotes.

Logic mirrors msts-live's live runner for parity:
  - drift threshold 4% (skip ticker if |Δ| / NAV < threshold)
  - exit-all for tickers not in targets
  - whole-NAV sizing (no margin-aware uniform scale in v1 — surfaces a warning instead)
  - dollar-based shares: qty = round(delta_$ / price, 2)
"""
from __future__ import annotations

from decimal import Decimal

from .models import Order, Position, Preview, RebalanceRow, Side, Target

DRIFT_THRESHOLD = Decimal("0.04")  # 4%
MIN_ORDER_DOLLARS = Decimal("1")   # ignore sub-$1 dust
MAX_SANE_GROSS = Decimal("5.0")    # >500% gross => almost certainly percentages, not weights
BP_SAFETY = Decimal("0.97")        # leave a 3% buying-power cushion for margin-aware sizing


def build_preview(
    targets: list[Target],
    positions: dict[str, Position],
    nav: Decimal,
    cash: Decimal,
    buying_power: Decimal,
    quotes: dict[str, Decimal],
    drift_threshold: Decimal = DRIFT_THRESHOLD,
    margin_aware: bool = False,
    bp_safety: Decimal = BP_SAFETY,
) -> Preview:
    warnings: list[str] = []
    blockers: list[str] = []
    rows: list[RebalanceRow] = []
    orders: list[Order] = []

    if nav <= 0:
        blockers.append("Account NAV is zero or negative — cannot size orders.")
        return Preview(nav=nav, buying_power=buying_power, cash=cash, rows=[], orders=[], warnings=warnings, blockers=blockers)

    target_map = {t.ticker: t.weight for t in targets}

    # Sanity: weight sum
    total = sum(target_map.values(), Decimal(0))
    # Weights are fractions of NAV. A book can intentionally sum to more
    # than 1.0 — that's leverage (e.g. 1.60 = 160% gross exposure, financed
    # on margin). Only block absurd totals that almost certainly mean the
    # weights were pasted as percentages.
    if total > MAX_SANE_GROSS:
        blockers.append(
            f"Target weights sum to {total:.2f} ({total * 100:.0f}% gross) — over "
            f"{MAX_SANE_GROSS:.0f}x. This usually means the weights were pasted as "
            f"percentages instead of fractions (e.g. 31.23 should be 0.3123)."
        )
    elif total > Decimal("1.01"):
        warnings.append(
            f"Leveraged book: {total * 100:.0f}% gross exposure ({total:.2f}x). "
            f"Each position is sized at weight x NAV; the amount over 100% is "
            f"financed on margin, so this needs a margin account with enough "
            f"buying power."
        )
    elif total < Decimal("0.5") and len(targets) > 1:
        warnings.append(f"Target weights sum to only {total:.4f} (<0.5). Cash drag is large — verify CSV.")

    # 1) Tickers in targets → buy/sell to hit weight
    for t in targets:
        tkr = t.ticker
        target_w = t.weight
        target_dollars = nav * target_w
        cur_pos = positions.get(tkr)
        cur_dollars = cur_pos.market_value if cur_pos else Decimal(0)
        delta_dollars = target_dollars - cur_dollars
        current_pct = (cur_dollars / nav) if nav else Decimal(0)

        row = RebalanceRow(
            ticker=tkr,
            current_pct=current_pct,
            target_pct=target_w,
            delta_dollars=delta_dollars,
            order=None,
        )

        if abs(delta_dollars) / nav < drift_threshold:
            row.note = "within drift"
            rows.append(row)
            continue

        if abs(delta_dollars) < MIN_ORDER_DOLLARS:
            row.note = "dust"
            rows.append(row)
            continue

        px = quotes.get(tkr)
        if px is None or px <= 0:
            row.note = "no quote — skipped"
            warnings.append(f"{tkr}: no quote available, order skipped")
            rows.append(row)
            continue

        side = Side.BUY if delta_dollars > 0 else Side.SELL
        qty = (abs(delta_dollars) / px).quantize(Decimal("0.01"))
        if qty <= 0:
            row.note = "qty rounds to 0"
            rows.append(row)
            continue

        order = Order(
            ticker=tkr,
            side=side,
            quantity=qty,
            estimated_price=px,
            notional=qty * px,
        )
        row.order = order
        row.note = ""
        orders.append(order)
        rows.append(row)

    # 2) Tickers in positions but not in targets → exit
    for tkr, pos in positions.items():
        if tkr in target_map:
            continue
        if pos.quantity <= 0:  # shorts left untouched in v1
            continue
        cur_dollars = pos.market_value
        current_pct = cur_dollars / nav if nav else Decimal(0)
        row = RebalanceRow(
            ticker=tkr,
            current_pct=current_pct,
            target_pct=Decimal(0),
            delta_dollars=-cur_dollars,
            order=None,
            note="exit (not in targets)",
        )
        qty = pos.quantity.quantize(Decimal("0.01"))
        if qty > 0:
            order = Order(
                ticker=tkr,
                side=Side.SELL,
                quantity=qty,
                estimated_price=pos.price,
                notional=cur_dollars,
            )
            row.order = order
            orders.append(order)
        rows.append(row)

    # 3) Buying-power handling
    gross_buys = sum((o.notional for o in orders if o.side == Side.BUY), Decimal(0))
    sell_proceeds = sum((o.notional for o in orders if o.side == Side.SELL), Decimal(0))
    # Sells run first (below), so their proceeds are available to fund buys.
    available_bp = (buying_power + sell_proceeds) * bp_safety

    if margin_aware and gross_buys > available_bp and gross_buys > 0 and available_bp > 0:
        # Scale every BUY by one uniform factor so the whole book fits the
        # available buying power — preserving relative weights, instead of
        # letting the broker reject the tail of the order set piecemeal.
        scale = available_bp / gross_buys
        for o in orders:
            if o.side == Side.BUY:
                o.quantity = (o.quantity * scale).quantize(Decimal("0.01"))
                o.notional = o.quantity * (o.estimated_price or Decimal(0))
        # drop buys that scaled to zero
        orders = [o for o in orders if not (o.side == Side.BUY and o.quantity <= 0)]
        gross_buys = sum((o.notional for o in orders if o.side == Side.BUY), Decimal(0))
        warnings.append(
            f"Margin-aware: scaled all buys by {scale:.1%} to fit ${available_bp:,.0f} "
            f"buying power (weight-preserving)."
        )
    elif gross_buys > buying_power:
        warnings.append(
            f"Gross buys ${gross_buys:,.0f} exceed buying power ${buying_power:,.0f} — "
            + ("re-run with --margin-aware to scale to fit, or " if not margin_aware else "")
            + "the broker's pre-flight may scale orders down at submit."
        )

    # Execute SELLS before BUYS: proceeds settle/free buying power first, so a
    # rebalance never tries to buy before the funding sells go through. Within
    # each side, larger dollar moves first.
    orders.sort(key=lambda o: (0 if o.side == Side.SELL else 1, -abs(o.notional)))

    rows.sort(key=lambda r: (r.order is None, -abs(r.delta_dollars)))
    return Preview(nav=nav, buying_power=buying_power, cash=cash, rows=rows, orders=orders, warnings=warnings, blockers=blockers)
