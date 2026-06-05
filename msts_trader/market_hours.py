"""Minimal market-hours check. ET timezone, US equity sessions.

Holidays list covers 2025-2027 — keep refreshed annually.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# NYSE/NASDAQ full closures. Half-days (early close 13:00 ET) are not enumerated
# here — minutes-to-close uses 16:00 always, the worst case for "is it safe to send a market order".
HOLIDAYS: set[date] = {
    # 2025
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


@dataclass
class MarketStatus:
    status: str  # "open" | "premarket" | "afterhours" | "closed"
    minutes_to_close: int | None
    next_open: datetime | None


def now_et() -> datetime:
    return datetime.now(tz=ET)


def is_holiday(d: date) -> bool:
    return d in HOLIDAYS


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def market_status(now: datetime | None = None) -> MarketStatus:
    now = now or now_et()
    today = now.date()
    if is_weekend(today) or is_holiday(today):
        return MarketStatus("closed", None, _next_open(now))

    t = now.timetz().replace(tzinfo=None)
    if time(4, 0) <= t < RTH_OPEN:
        return MarketStatus("premarket", None, None)
    if RTH_OPEN <= t < RTH_CLOSE:
        close_dt = datetime.combine(today, RTH_CLOSE, tzinfo=ET)
        mins = int((close_dt - now).total_seconds() // 60)
        return MarketStatus("open", mins, None)
    if RTH_CLOSE <= t < time(20, 0):
        return MarketStatus("afterhours", None, None)
    return MarketStatus("closed", None, _next_open(now))


def _next_open(now: datetime) -> datetime:
    d = now.date()
    for _ in range(10):
        d += timedelta(days=1)
        if not is_weekend(d) and not is_holiday(d):
            return datetime.combine(d, RTH_OPEN, tzinfo=ET)
    return datetime.combine(d, RTH_OPEN, tzinfo=ET)
