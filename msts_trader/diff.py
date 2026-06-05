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


def build_preview(
    targets: list[Target],
    positions: dict[str, Position],
    nav: Decimal,
    cash: Decimal,
    buying_power: Decimal,
    quotes: dict[str, Decimal],
    drift_threshold: Decimal = DRIFT_THRESHOLD,
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
    if total > Decimal("1.05"):
        blockers.append(f"Target weights sum to {total:.4f} (>1.05). CSV looks malformed.")
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

    # 3) Buying-power sanity
    gross_buys = sum((o.notional for o in orders if o.side == Side.BUY), Decimal(0))
    if gross_buys > buying_power:
        warnings.append(
            f"Gross buys ${gross_buys:,.0f} exceed buying power ${buying_power:,.0f} — "
            f"Tastytrade BP pre-flight may scale down at submit."
        )

    rows.sort(key=lambda r: (r.order is None, -abs(r.delta_dollars)))
    return Preview(nav=nav, buying_power=buying_power, cash=cash, rows=rows, orders=orders, warnings=warnings, blockers=blockers)
