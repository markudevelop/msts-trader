"""Pre-trade safety checks: order-value cap and stale-CSV guard."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from dateutil import parser as date_parser

from .models import Order, Side

# A CSV may carry an as-of timestamp in a comment line, e.g.:
#   # asof: 2026-06-05T15:45:00Z
_ASOF_RE = re.compile(r"#\s*asof\s*[:=]\s*(.+)", re.IGNORECASE)


def gross_buy_notional(orders: list[Order]) -> Decimal:
    return sum((o.notional for o in orders if o.side == Side.BUY), Decimal(0))


def check_max_notional(orders: list[Order], max_notional: Decimal | None) -> str | None:
    """Return a blocker message if gross buy notional exceeds the cap."""
    if not max_notional or max_notional <= 0:
        return None
    gross = gross_buy_notional(orders)
    if gross > max_notional:
        return f"Gross buys ${gross:,.0f} exceed the --max-notional safety cap ${max_notional:,.0f}. Refusing to trade."
    return None


def _asof_raw(csv_text: str) -> str | None:
    """The raw `# asof:` stamp text, or None when the CSV carries none."""
    for line in csv_text.splitlines():
        m = _ASOF_RE.match(line.strip())
        if m:
            return m.group(1).strip()
    return None


def parse_asof(csv_text: str) -> datetime | None:
    """Extract an `# asof: <timestamp>` comment from the CSV, if present."""
    raw = _asof_raw(csv_text)
    if raw is None:
        return None
    try:
        dt = date_parser.parse(raw)
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_stale(csv_text: str, max_stale_hours: float | None, now: datetime | None = None) -> str | None:
    """Return a blocker message if the CSV's as-of time is too old.

    No-op when max_stale_hours is unset or the CSV carries no as-of stamp.
    A stamp that is PRESENT but unparseable fails CLOSED when the guard is
    armed — a producer-side bug that corrupts the timestamp must not silently
    disable the very check the operator asked for.
    """
    if not max_stale_hours or max_stale_hours <= 0:
        return None
    asof = parse_asof(csv_text)
    if asof is None:
        raw = _asof_raw(csv_text)
        if raw is not None:
            return (
                f"CSV carries an as-of stamp ({raw!r}) that cannot be parsed, so the "
                f"staleness guard can't run. Fix the timestamp (e.g. ISO-8601) or drop "
                f"the max-stale-hours limit to trade without it."
            )
        return None
    now = now or datetime.now(timezone.utc)
    age_hours = (now - asof).total_seconds() / 3600.0
    if age_hours > max_stale_hours:
        return (
            f"CSV is stale: as-of {asof.isoformat()} is {age_hours:.1f}h old "
            f"(limit {max_stale_hours:.0f}h). Refusing to trade on stale weights."
        )
    return None
