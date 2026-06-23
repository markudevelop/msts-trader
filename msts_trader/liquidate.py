"""Liquidate (flatten) an account — drive every position to zero for best fills.

The deliberate inverse of `rebalance`: instead of snapping to target weights,
this sells every long and buys back every short until the book is flat. Each
line is worked through the patient limit-chase (`chase.chase_fill`): a LIMIT
pegged to the live mid, repriced toward the fill side over several rungs, then a
MARKET mop-up for the unfilled / fractional remainder. The point is execution
QUALITY — capture the spread instead of crossing it with a plain market order —
while still guaranteeing the position actually closes.

Notes specific to flattening:
- Largest positions go first, so the biggest risk comes off the book soonest.
- Tastytrade LIMIT orders are whole-share only; the fractional remainder of a
  position (e.g. 144.52 -> 144 by limit, 0.52 by market) is closed by the chase
  engine's market fallback, which DOES allow fractional. Purely-fractional dust
  (e.g. 0.32 shares) skips straight to the market fallback.
- Resting protective stops on a name being sold are cancelled first; a broker
  rejects a sell of shares reserved by an open stop order.

RTH-only for live execution (the chase + market fallback assume the regular
session); a dry-run preview works any time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from .chase import ChaseConfig, chase_fill
from .models import Order, Position, Side


@dataclass
class LiquidationPlan:
    """The set of close orders plus what was deliberately left alone."""

    orders: list[Order] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (ticker, reason)
    gross: Decimal = Decimal(0)  # total |market value| being liquidated


def build_plan(
    positions: dict[str, Position],
    *,
    only: list[str] | None = None,
    exclude: list[str] | None = None,
) -> LiquidationPlan:
    """Turn current positions into close orders.

    Long (qty > 0) -> SELL; short (qty < 0) -> BUY-to-cover. Quantity is the
    absolute size; the adapter picks SELL_TO_CLOSE / BUY_TO_CLOSE from the live
    position. `only` restricts to a whitelist; `exclude` keeps named tickers.
    Orders are sorted largest-notional first.
    """
    only_set = {t.upper() for t in only} if only else None
    excl_set = {t.upper() for t in (exclude or [])}

    plan = LiquidationPlan()
    for tkr, p in positions.items():
        u = tkr.upper()
        if only_set is not None and u not in only_set:
            continue
        if u in excl_set:
            plan.skipped.append((tkr, "excluded"))
            continue
        qty = Decimal(str(p.quantity))
        if qty == 0:
            plan.skipped.append((tkr, "flat"))
            continue
        side = Side.SELL if qty > 0 else Side.BUY
        notional = abs(Decimal(str(p.market_value)))
        plan.orders.append(
            Order(ticker=tkr, side=side, quantity=abs(qty), estimated_price=p.price, notional=notional)
        )
        plan.gross += notional

    plan.orders.sort(key=lambda o: -(o.notional or Decimal(0)))
    return plan


def liquidation_config(
    *,
    retries: int = 6,
    interval: float = 8.0,
    poll_interval: float = 1.0,
    aggression: float = 0.0,
    fallback_to_market: bool = True,
) -> ChaseConfig:
    """Patient-by-default chase tuning for a flatten.

    aggression: fraction of the mid to give up toward the fill side. 0 pegs the
    pure mid (default). NEGATIVE is more passive (rest above the mid on a sell —
    better price, slower fill); POSITIVE crosses toward the touch (faster, worse
    price). The market fallback still guarantees completion.
    """
    return ChaseConfig(
        retries=retries,
        reprice_interval=interval,
        poll_interval=poll_interval,
        aggression=Decimal(str(aggression)),
        fallback_to_market=fallback_to_market,
    )


def _precancel_stops(broker, sell_tickers: set[str], *, log) -> None:
    """Cancel resting protective stops on names about to be sold (else the sell
    bounces on shares reserved by the open stop). Best-effort; never raises."""
    if not sell_tickers or not getattr(broker, "supports_stops", False):
        return
    try:
        open_stops = broker.open_stops()
    except Exception as e:  # pragma: no cover - defensive
        log(f"  pre-cancel skipped: open_stops failed ({e}) — sells may bounce on resting stops")
        return
    for tkr in sell_tickers:
        for st in open_stops.get(tkr, []):
            try:
                broker.cancel_order(st["order_id"])
                log(f"  pre-cancel stop {tkr} (frees shares to sell)")
            except Exception as e:
                log(f"  pre-cancel stop failed for {tkr}: {e}")


def run_liquidation(
    broker,
    plan: LiquidationPlan,
    cfg: ChaseConfig,
    *,
    dry_run: bool = False,
    pace: float = 0.0,
    log=None,
    sleep=time.sleep,
) -> list[dict]:
    """Work every order in `plan` through the chase engine, largest first.

    Returns one chase-result dict per order. `pace` inserts a delay between
    names so the flatten can be spread out. `sleep` is injectable for tests.
    """
    say = log if callable(log) else (lambda *a, **k: None)

    if not dry_run:
        _precancel_stops(broker, {o.ticker for o in plan.orders if o.side == Side.SELL}, log=say)

    results: list[dict] = []
    total = len(plan.orders)
    for i, o in enumerate(plan.orders, 1):
        if pace and i > 1 and not dry_run:
            sleep(pace)
        say(f"[{i}/{total}] {o.ticker} {o.side.value} {o.quantity} ...")
        try:
            res = chase_fill(broker, o, cfg, dry_run=dry_run, log=say, sleep=sleep)
        except Exception as e:  # pragma: no cover - defensive
            res = {"status": "error", "reason": str(e), "ticker": o.ticker}
        results.append(res)
    return results
