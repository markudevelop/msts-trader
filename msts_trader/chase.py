"""Broker-agnostic limit-chase execution.

Works each order as a LIMIT pegged to the live mid: re-quote, reprice toward
the fill side, poll for a fill, and repeat up to a retry cap — then fall back
to a MARKET order so the rebalance always completes. Ported in spirit from the
extended-hours chase in the msts-live tastytrade runner, but simplified to
mid-tracking (no bid/ask ladder) so it runs on every adapter's existing
``quote()`` API.

RTH-only by design: the market fallback assumes the regular session, and the
rebalance command already refuses to run pre/after-hours. The point here is
execution quality during RTH — peg near the mid instead of crossing the whole
spread with a plain market order.

Safety properties carried over from the original:
- cancel-before-reprice: the prior limit is cancelled before the next is
  placed, and if a cancel FAILS the chase aborts (no double live order).
- partial-fill aware: only the unfilled remainder is ever re-submitted.
- never leaves a resting order behind.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from decimal import Decimal

from .models import Order, Side

# Normalized order_status() vocabulary every chase-capable adapter returns.
WORKING = "working"
PARTIAL = "partial"
FILLED = "filled"
CANCELLED = "cancelled"
REJECTED = "rejected"
UNKNOWN = "unknown"

_TICK = Decimal("0.01")
_PLACE_FAILED = ("error", "skipped", "rejected")
# How many status polls to spend confirming a cancelled rung actually reached a
# terminal state before its remainder is re-placed (cancel is an async REQUEST
# at most brokers — a fill can land inside the cancel window).
_CANCEL_CONFIRM_POLLS = 5


@dataclass(frozen=True)
class ChaseConfig:
    """Tunables for the mid-tracking limit chase."""

    retries: int = 5  # reprice attempts before fallback
    reprice_interval: float = 5.0  # seconds to wait for a fill per rung
    poll_interval: float = 1.0  # status-poll cadence within a rung
    aggression: Decimal = Decimal("0")  # fraction past mid toward the fill side
    fallback_to_market: bool = True  # market order for the unfilled remainder


def limit_from_mid(side: Side, mid: Decimal, aggression: Decimal) -> Decimal:
    """Limit price = mid nudged `aggression` toward the fill side, rounded to a
    cent. BUY pays up, SELL gives up; aggression 0 is the pure mid."""
    factor = (Decimal(1) + aggression) if side == Side.BUY else (Decimal(1) - aggression)
    px = (mid * factor).quantize(_TICK)
    if px <= 0:
        px = mid.quantize(_TICK)
    return px


def _mid(broker, order: Order):
    """Current mid for the order's ticker, or None if unavailable."""
    try:
        quotes = broker.quote([order.ticker])
    except Exception:
        return None
    raw = quotes.get(order.ticker) or quotes.get(order.ticker.upper())
    if not raw:
        return None
    mid = Decimal(str(raw))
    return mid if mid > 0 else None


def chase_fill(broker, order: Order, cfg: ChaseConfig, *, dry_run: bool = False, log=None, sleep=time.sleep) -> dict:
    """Work `order` as a limit chase on `broker`. Returns a place_market-style
    result dict (status/ticker/order_id/...). `sleep` is injectable for tests."""
    say = log if callable(log) else (lambda *a, **k: None)
    cfg = cfg or ChaseConfig()
    side = order.side
    qty_total = Decimal(str(order.quantity))
    if qty_total <= 0:
        return {"status": "skipped", "reason": "qty<=0", "ticker": order.ticker}

    # Dry-run: don't simulate a multi-rung ladder against a single frozen quote
    # (the reprices only mean anything once the mid actually moves). Show the
    # one initial limit we'd place right now and stop there.
    if dry_run:
        mid = _mid(broker, order)
        if mid is None:
            say(f"[yellow]  ⚠ chase {order.ticker}: no quote — would place a MARKET order[/yellow]")
            return {
                "status": "dry-run",
                "ticker": order.ticker,
                "side": side.value,
                "quantity": float(qty_total),
                "dry_run": True,
                "reason": "chase: no quote; would fall back to market",
            }
        limit = limit_from_mid(side, mid, cfg.aggression)
        say(
            f"  [chase DRY] {order.ticker}: would place initial LIMIT {side.value} "
            f"{qty_total} @ {limit} (mid {mid}), then chase up to {cfg.retries} rungs"
        )
        return {
            "status": "dry-run",
            "ticker": order.ticker,
            "side": side.value,
            "quantity": float(qty_total),
            "limit_price": float(limit),
            "dry_run": True,
        }

    filled_qty = Decimal(0)
    total_cost = Decimal(0)
    filled_oid = None
    last_oid = None

    polls = max(1, round(cfg.reprice_interval / cfg.poll_interval)) if cfg.poll_interval > 0 else 1

    def _remaining() -> Decimal:
        return qty_total - filled_qty

    def _record(qty, avg, oid) -> None:
        """Fold one rung's fill into the totals, clamped to the room left so a
        broker over-reporting filled_qty can never push the total past
        qty_total. Recorded once per rung (filled_qty stays monotonic)."""
        nonlocal filled_qty, total_cost, filled_oid
        q = Decimal(str(qty or 0))
        room = qty_total - filled_qty
        if q > room:
            q = room
        if q <= 0:
            return
        filled_qty += q
        if avg:
            total_cost += q * Decimal(str(avg))
        filled_oid = oid

    def _cancel(oid) -> bool:
        try:
            res = broker.cancel_order(oid)
        except Exception:
            return False
        if isinstance(res, dict) and str(res.get("status") or "").lower() in ("error", "rejected"):
            return False
        return True

    def _abort_double_fill(oid):
        return {
            "status": "error",
            "ticker": order.ticker,
            "side": side.value,
            "reason": (
                f"chase: cancel FAILED for {oid} ({order.ticker}); aborting before reprice to avoid a double-fill"
            ),
            "filled_quantity": float(filled_qty),
            # A live order may remain at the broker — self-heal must NOT
            # re-trade this leg (a second order on top would double-fill).
            "order_live": True,
        }

    def _final_fill_after_cancel(oid, prev_filled, prev_avg):
        """After a cancel request, poll until the rung reaches a terminal state
        (FILLED/CANCELLED/REJECTED) so its true fill is known before any
        remainder is re-placed. A fill landing inside the cancel window is
        captured here instead of being re-traded. Returns (terminal, filled,
        avg); terminal=False means the order never confirmed dead — the caller
        must abort rather than risk a double-fill."""
        filled, avg = prev_filled, prev_avg
        unknown_streak = 0
        for _ in range(_CANCEL_CONFIRM_POLLS):
            try:
                st = broker.order_status(oid)
            except Exception:
                # Client-side failure, not an authoritative read — keep polling;
                # if it never recovers the loop exhausts and the caller aborts.
                sleep(cfg.poll_interval)
                continue
            ff = Decimal(str(st.get("filled_qty") or 0))
            if ff > filled:
                filled, avg = ff, st.get("filled_avg_price") or avg
            status = st.get("status")
            if status in (FILLED, CANCELLED, REJECTED):
                return True, filled, avg
            if status == UNKNOWN:
                # Some adapters can't see an order once it leaves the open set
                # (e.g. IBKR queries open trades only). After an ACCEPTED cancel,
                # two consecutive UNKNOWNs mean it's gone — one alone could be a
                # transient read failure racing a fill.
                unknown_streak += 1
                if unknown_streak >= 2:
                    return True, filled, avg
            else:
                unknown_streak = 0
            sleep(cfg.poll_interval)
        return False, filled, avg

    for attempt in range(1, cfg.retries + 1):
        rem = _remaining()
        if rem <= 0:
            break

        mid = _mid(broker, order)
        if mid is None:
            # The user picked limit-chase specifically to control the spread —
            # losing the quote and crossing with a market order is worth shouting about.
            say(
                f"[yellow]  ⚠ chase {order.ticker}: no quote on attempt {attempt} — "
                f"falling back to a MARKET order (spread NOT controlled)[/yellow]"
            )
            break
        limit = limit_from_mid(side, mid, cfg.aggression)

        # cancel-before-reprice (abort if a cancel fails)
        if last_oid is not None:
            if not _cancel(last_oid):
                return _abort_double_fill(last_oid)
            last_oid = None

        rem_order = replace(order, quantity=rem, estimated_price=limit)
        try:
            placed = broker.place_limit(rem_order, limit, dry_run=False)
        except Exception as e:
            return {
                "status": "error",
                "ticker": order.ticker,
                "side": side.value,
                "reason": f"chase: place_limit failed: {e}",
                "filled_quantity": float(filled_qty),
            }
        if str(placed.get("status") or "").lower() in _PLACE_FAILED:
            say(
                f"  chase {order.ticker}: place_limit {placed.get('status')} "
                f"({placed.get('reason', '')}) — falling through to market"
            )
            last_oid = None
            break
        last_oid = placed.get("order_id")
        if not last_oid:
            # Placed but unidentifiable: we can neither poll nor cancel it, so a
            # market fallback could double the position. Abort loudly instead.
            return {
                "status": "error",
                "ticker": order.ticker,
                "side": side.value,
                "reason": (
                    "chase: place_limit returned no order_id — order may be "
                    "live but unmanageable; aborting (check the broker manually)"
                ),
                "filled_quantity": float(filled_qty),
                "order_live": True,
            }
        say(f"  chase {order.ticker} {attempt}/{cfg.retries} {side.value} {rem} @ {limit}  id={last_oid}")

        # poll this rung for a fill (rung_filled is cumulative for THIS order)
        rung_filled = Decimal(0)
        rung_avg = None
        rung_status = WORKING
        for _ in range(polls):
            sleep(cfg.poll_interval)
            try:
                st = broker.order_status(last_oid)
            except Exception:
                continue
            rung_status = st.get("status", UNKNOWN)
            rung_filled = Decimal(str(st.get("filled_qty") or 0))
            rung_avg = st.get("filled_avg_price")
            if rung_status == FILLED or rung_filled >= rem or rung_status in (CANCELLED, REJECTED):
                break

        if rung_status == FILLED or rung_filled >= rem:
            # FILLED with no per-fill detail (some adapters lag) → assume the rung qty.
            _record(rung_filled if rung_filled > 0 else rem, rung_avg, last_oid)
            last_oid = None
            break

        # not (fully) filled — cancel the rung, then confirm it actually died
        # and capture its final fill BEFORE any remainder is re-placed
        if last_oid is not None:
            oid = last_oid
            ok = _cancel(oid)
            if not ok:
                _record(rung_filled, rung_avg, oid)
                last_oid = None
                return _abort_double_fill(oid)
            terminal, final_filled, final_avg = _final_fill_after_cancel(oid, rung_filled, rung_avg)
            _record(final_filled, final_avg, oid)
            last_oid = None
            if not terminal:
                return {
                    "status": "error",
                    "ticker": order.ticker,
                    "side": side.value,
                    "reason": (
                        f"chase: cancel of {oid} ({order.ticker}) never confirmed terminal — "
                        "aborting before reprice to avoid a double-fill (check the broker manually)"
                    ),
                    "filled_quantity": float(filled_qty),
                    "order_live": True,
                }
            if _remaining() <= 0:
                break

    # never leave a resting order behind
    if last_oid is not None:
        _cancel(last_oid)
        last_oid = None

    avg_fill = float(total_cost / filled_qty) if filled_qty > 0 and total_cost > 0 else None

    if _remaining() <= 0:  # fully filled by the chase
        out = {
            "status": "FILLED",
            "ticker": order.ticker,
            "side": side.value,
            "quantity": float(filled_qty),
            "order_id": filled_oid,
            "chase": True,
        }
        if avg_fill is not None:
            out["fill_price"] = avg_fill
        return out

    rem = _remaining()
    if cfg.fallback_to_market:
        say(f"  chase {order.ticker}: unfilled {rem} after {cfg.retries} rungs — market fallback")
        try:
            fb = broker.place_market(replace(order, quantity=rem), dry_run=False)
        except Exception as e:
            fb = {"status": "error", "ticker": order.ticker, "reason": f"chase market fallback failed: {e}"}
        fb["chase_fell_back"] = True
        if filled_qty > 0:
            fb["chase_limit_filled"] = float(filled_qty)
            # Carry a usable entry price + the order id of the filled rung so
            # stop reconciliation can still protect the shares that DID fill,
            # even when the fallback market order itself errored (status="error").
            if not fb.get("fill_price") and avg_fill is not None:
                fb["fill_price"] = avg_fill
            fb.setdefault("order_id", filled_oid)
        return fb

    # no fallback: report what (if anything) the chase filled
    if filled_qty > 0:
        out = {
            "status": "PARTIAL",
            "ticker": order.ticker,
            "side": side.value,
            "quantity": float(filled_qty),
            "order_id": filled_oid,
            "chase": True,
            "reason": f"chase filled {filled_qty}/{qty_total}, fallback disabled",
        }
        if avg_fill is not None:
            out["fill_price"] = avg_fill
        return out
    return {
        "status": "error",
        "ticker": order.ticker,
        "side": side.value,
        "reason": f"chase: unfilled after {cfg.retries} rungs (fallback disabled)",
    }
