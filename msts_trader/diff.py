"""Build a Preview from targets + current positions + balances + quotes.

Logic mirrors msts-live's live runner for parity:
  - drift threshold 4%
  - execution scope (default "whole-book"): the threshold is a TRIGGER — if any
    line breaches it, snap the whole book to target (msts-live's all-or-nothing
    dead zone). "per-ticker" instead trades only the breaching lines.
  - exit-all for tickers not in targets
  - whole-NAV sizing (no margin-aware uniform scale in v1 — surfaces a warning instead)
  - dollar-based shares: qty = round(delta_$ / price, 2)
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from .models import Order, Position, Preview, RebalanceRow, Side, Target

DRIFT_THRESHOLD = Decimal("0.04")  # 4%
MIN_ORDER_DOLLARS = Decimal("1")  # ignore sub-$1 dust
MAX_SANE_GROSS = Decimal("5.0")  # >500% gross => almost certainly percentages, not weights
BP_SAFETY = Decimal("0.97")  # leave a 3% buying-power cushion for margin-aware sizing


def build_preview(
    targets: list[Target],
    positions: dict[str, Position],
    nav: Decimal,
    cash: Decimal,
    buying_power: Decimal,
    quotes: dict[str, Decimal],
    drift_threshold: Decimal = DRIFT_THRESHOLD,
    min_weight: Decimal | None = None,
    allocation: Decimal | None = None,
    drift_mode: str = "nav",
    rebalance_scope: str = "whole-book",
    sweep: bool = True,
    whole_shares: bool = False,
) -> Preview:
    warnings: list[str] = []
    blockers: list[str] = []
    rows: list[RebalanceRow] = []
    orders: list[Order] = []

    # Quantity precision: whole shares (for brokers/accounts without
    # fractional-API permission — e.g. an IBKR account that rejects fractional
    # orders with error 10243) vs the default 0.01-share granularity. Always
    # rounds DOWN so a buy never exceeds its target and a sell never exceeds
    # the held quantity.
    qexp = Decimal("1") if whole_shares else Decimal("0.01")

    if nav <= 0:
        blockers.append("Account NAV is zero or negative — cannot size orders.")
        return Preview(
            nav=nav, buying_power=buying_power, cash=cash, rows=[], orders=[], warnings=warnings, blockers=blockers
        )

    # Sizing base: weights apply to `allocation` dollars when given (sub-
    # portfolio sizing — e.g. run a $50k book inside a $200k account),
    # otherwise to the full NAV. Never above NAV: leverage should come from
    # the weights summing past 1.0, not from an oversized allocation.
    base = nav
    if allocation is not None and allocation > 0:
        if allocation > nav:
            warnings.append(
                f"--allocation ${allocation:,.0f} exceeds account NAV ${nav:,.2f} — using NAV. "
                f"(Use leveraged weights, not an oversized allocation, for gross >100%.)"
            )
        else:
            base = allocation
            warnings.append(f"Sizing against ${allocation:,.0f} allocation (account NAV ${nav:,.2f}).")

    target_map = {t.ticker: t.weight for t in targets}

    def _denom(target_dollars: Decimal, cur_dollars: Decimal) -> Decimal:
        # Drift denominator. "position": delta relative to the line itself.
        # "nav" (default): delta as fraction of the whole book.
        if drift_mode == "position":
            return max(abs(target_dollars), abs(cur_dollars), MIN_ORDER_DOLLARS)
        return base

    # Execution scope (orthogonal to drift_mode):
    #   "whole-book" (default): the drift gate is a TRIGGER for the whole book —
    #       if ANY line breaches it, every line is snapped to target (matches
    #       msts-live's all-or-nothing dead-zone, the higher-CAGR behavior).
    #       If nothing breaches, the book is frozen (no orders).
    #   "per-ticker": each line is gated individually — only the breaching lines
    #       trade, the rest keep their drifted value (lower turnover, the prior
    #       msts-trader behavior).
    # Pre-scan once to decide whether the whole book is "live" this run. An
    # actionable breach is a target line past both the drift gate and the $1 dust
    # floor, OR any holding the book wants to exit (weight 0 / not in targets).
    book_breached = False
    if rebalance_scope == "whole-book":
        for t in targets:
            tw = t.weight
            if min_weight is not None and Decimal(0) < tw < min_weight:
                continue  # ignored line — never trades, never triggers
            tgt_d = base * tw
            cur = positions.get(t.ticker)
            cur_d = cur.market_value if cur else Decimal(0)
            if tw == 0:
                if cur is not None and cur.quantity > 0 and abs(cur_d) >= MIN_ORDER_DOLLARS:
                    book_breached = True
                    break
                continue
            dd = tgt_d - cur_d
            if abs(dd) >= MIN_ORDER_DOLLARS and abs(dd) / _denom(tgt_d, cur_d) >= drift_threshold:
                book_breached = True
                break
        if sweep and not book_breached:  # a sweep exit also makes the book live
            for tkr, pos in positions.items():
                if tkr in target_map:
                    continue
                if pos.quantity > 0 and pos.market_value >= MIN_ORDER_DOLLARS:
                    book_breached = True
                    break

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
        target_dollars = base * target_w
        cur_pos = positions.get(tkr)
        cur_dollars = cur_pos.market_value if cur_pos else Decimal(0)
        delta_dollars = target_dollars - cur_dollars
        current_pct = (cur_dollars / base) if base else Decimal(0)

        row = RebalanceRow(
            ticker=tkr,
            current_pct=current_pct,
            target_pct=target_w,
            delta_dollars=delta_dollars,
            order=None,
        )

        # Below min-weight → don't trade it at all. The ticker stays in
        # target_map, so an existing position is also left alone (NOT swept
        # by the exit-all pass) — "ignore" means neither buy nor sell.
        # An explicit weight of 0 keeps its exit semantics.
        if min_weight is not None and Decimal(0) < target_w < min_weight:
            row.note = f"below min weight {min_weight} — ignored"
            rows.append(row)
            continue

        # Explicit weight 0 == "exit this position fully", IDENTICAL to dropping
        # the row from the book. It must bypass the drift/dust gates below — a
        # small holding (< drift_threshold of NAV) would otherwise be frozen as
        # "within drift" and never sold, silently keeping a position the book
        # said to close. Sell the whole held quantity (like the exit-all pass).
        if target_w == 0 and cur_pos is not None and cur_pos.quantity > 0:
            qty = cur_pos.quantity.quantize(qexp, rounding=ROUND_DOWN)
            if qty > 0:
                order = Order(
                    ticker=tkr,
                    side=Side.SELL,
                    quantity=qty,
                    estimated_price=(px if (px := quotes.get(tkr)) and px > 0 else cur_pos.price),
                    notional=cur_dollars,
                )
                row.order = order
                row.note = "exit (weight 0)"
                orders.append(order)
            else:
                row.note = "exit qty rounds to 0 (whole-share)"
            rows.append(row)
            continue

        # Drift gate, scope-aware. Per-ticker: skip THIS line if it's within
        # drift. Whole-book: skip every line only when NO line breached (book
        # frozen); once any line breached, snap this line to target too even if
        # it is individually within drift.
        within = abs(delta_dollars) / _denom(target_dollars, cur_dollars) < drift_threshold
        if rebalance_scope == "whole-book":
            if not book_breached:
                row.note = "within drift (book frozen)"
                rows.append(row)
                continue
        elif within:
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
        # Round DOWN: never let share rounding push a buy above its target,
        # so a fully-invested book doesn't spuriously exceed buying power.
        qty = (abs(delta_dollars) / px).quantize(qexp, rounding=ROUND_DOWN)
        if qty <= 0:
            row.note = "qty rounds to 0 (whole-share)" if whole_shares else "qty rounds to 0"
            rows.append(row)
            continue

        order = Order(
            ticker=tkr,
            side=side,
            quantity=qty,
            estimated_price=px,
            notional=qty * px,
            stop_pct=(t.stop_pct if side == Side.BUY else None),
        )
        row.order = order
        row.note = ""
        orders.append(order)
        rows.append(row)

    # 2) Tickers in positions but not in targets → exit.
    # The sweep treats the CSV as the COMPLETE book: anything held but unlisted is
    # liquidated. With sweep=False (--no-sweep) the engine touches ONLY the CSV's
    # tickers and leaves every other position untouched — for running a strategy
    # sleeve inside a mixed account. To CLOSE a rotated-out name under --no-sweep,
    # list it explicitly with weight 0 (handled in section 1 above).
    for tkr, pos in positions.items():
        if tkr in target_map:
            continue
        if pos.quantity <= 0:  # shorts left untouched in v1
            continue
        cur_dollars = pos.market_value
        current_pct = cur_dollars / base if base else Decimal(0)
        row = RebalanceRow(
            ticker=tkr,
            current_pct=current_pct,
            target_pct=Decimal(0),
            delta_dollars=-cur_dollars,
            order=None,
            note="exit (not in targets)",
        )
        # --no-sweep: surface the held-but-unlisted position so the operator sees
        # it's deliberately left alone, but generate NO order. List it with weight
        # 0 to actually close it.
        if not sweep:
            row.delta_dollars = Decimal(0)
            row.note = "kept — not in targets (--no-sweep)"
            rows.append(row)
            continue
        # Whole-share exits round DOWN — never try to sell more than is held
        # (a fractional residual on a whole-share-only account stays put; the
        # broker couldn't sell it anyway).
        qty = (
            pos.quantity.quantize(qexp, rounding=ROUND_DOWN) if whole_shares else pos.quantity.quantize(Decimal("0.01"))
        )
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

    # 3) Buying-power warning (scaling is applied separately by apply_margin_aware,
    #    which can use the broker's real margin numbers).
    gross_buys = sum((o.notional for o in orders if o.side == Side.BUY), Decimal(0))
    if gross_buys > buying_power:
        warnings.append(
            f"Gross buys ${gross_buys:,.0f} exceed buying power ${buying_power:,.0f} — "
            f"re-run with --margin-aware to scale to fit, or the broker's pre-flight "
            f"may scale orders down at submit."
        )

    # Execute SELLS before BUYS: proceeds settle/free buying power first, so a
    # rebalance never tries to buy before the funding sells go through. Within
    # each side, larger dollar moves first.
    orders.sort(key=lambda o: (0 if o.side == Side.SELL else 1, -abs(o.notional)))

    rows.sort(key=lambda r: (r.order is None, -abs(r.delta_dollars)))
    return Preview(
        nav=nav, buying_power=buying_power, cash=cash, rows=rows, orders=orders, warnings=warnings, blockers=blockers
    )


def apply_margin_aware(
    preview: Preview,
    *,
    buying_power: Decimal,
    real_margin: Decimal | None = None,
    bp_safety: Decimal = BP_SAFETY,
    add_warning: bool = True,
    whole_shares: bool = False,
) -> Decimal:
    """Scale all BUY orders by one factor so the book fits buying power.

    `real_margin` is the broker's *actual* total buying-power requirement for
    the buys (e.g. summed from Tastytrade order dry-runs — this captures
    leveraged-ETF margin rates that notional can't). When None, the notional
    value of the buys is used as a portable approximation.

    Weight-preserving: every buy is scaled by the same factor. No-op when the
    buys already fit (the common steady-state case). Mutates `preview` and
    returns the scale factor applied (Decimal(1) = no scaling). With real
    margin the caller may re-run this (the broker re-quotes the smaller book)
    to handle non-linear margin tiers — set `add_warning=False` then and emit
    one cumulative message.
    """
    buys = [o for o in preview.orders if o.side == Side.BUY]
    gross = sum((o.notional for o in buys), Decimal(0))
    sell_proceeds = sum((o.notional for o in preview.orders if o.side == Side.SELL), Decimal(0))
    # The FIT check uses the full available buying power — a book that fits
    # within 100% of BP is left alone (full deployment, no 3% trim). The
    # bp_safety cushion is applied ONLY when scaling DOWN an over-BP book, so
    # the scaled order set lands safely below the limit (slippage / fees).
    available = buying_power + sell_proceeds
    need = real_margin if real_margin is not None else gross

    # We're handling buying power now, so drop build_preview's generic
    # "re-run with --margin-aware" warning to avoid contradicting ourselves.
    preview.warnings = [w for w in preview.warnings if "re-run with --margin-aware" not in w]

    src = "real broker margin" if real_margin is not None else "estimated"
    if need <= 0 or available <= 0 or need <= available:
        if add_warning and gross > buying_power:  # only note it when it looked tight on raw BP
            preview.warnings.append(
                f"Margin-aware ({src}): buys fit ${available:,.0f} buying power "
                f"(incl. ${sell_proceeds:,.0f} sell proceeds) — no scaling needed."
            )
        return Decimal(1)  # already fits — nothing to scale

    scale = (available * bp_safety) / need
    qexp = Decimal("1") if whole_shares else Decimal("0.01")
    kept: list[Order] = []
    for o in preview.orders:
        if o.side == Side.BUY:
            o.quantity = (o.quantity * scale).quantize(qexp, rounding=ROUND_DOWN)
            o.notional = o.quantity * (o.estimated_price or Decimal(0))
            if o.quantity <= 0:
                continue
        kept.append(o)
    preview.orders = kept
    if add_warning:
        preview.warnings.append(
            f"Margin-aware ({src}): scaled all buys by {scale:.1%} to fit "
            f"${available:,.0f} buying power (weight-preserving)."
        )
    return scale
