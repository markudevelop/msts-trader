from __future__ import annotations

import pytest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from msts_trader.market_hours import ET, close_time_for, is_holiday, is_weekend, market_status


def _et(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


def test_weekend_detected():
    assert is_weekend(date(2026, 6, 6))  # Saturday
    assert is_weekend(date(2026, 6, 7))  # Sunday
    assert not is_weekend(date(2026, 6, 5))  # Friday


def test_known_holidays():
    assert is_holiday(date(2026, 12, 25))  # Christmas
    assert is_holiday(date(2026, 7, 3))  # July 4 observed (Friday)
    assert is_holiday(date(2026, 11, 26))  # Thanksgiving
    assert not is_holiday(date(2026, 12, 26))


def test_open_during_rth():
    ms = market_status(_et(2026, 6, 5, 10, 30))  # Fri 10:30 ET
    assert ms.status == "open"
    assert ms.minutes_to_close is not None
    assert ms.minutes_to_close > 0


def test_minutes_to_close_math():
    ms = market_status(_et(2026, 6, 5, 15, 45))  # 15 min before 16:00
    assert ms.status == "open"
    assert ms.minutes_to_close == 15


def test_premarket():
    ms = market_status(_et(2026, 6, 5, 7, 0))
    assert ms.status == "premarket"


def test_afterhours():
    ms = market_status(_et(2026, 6, 5, 17, 0))
    assert ms.status == "afterhours"


def test_half_day_early_close_minutes_to_close():
    # 2025-12-24 is an early close (13:00 ET). At 12:55 the market closes in 5 min
    # (not ~65) so the MOC cutoff fires; previously it used 16:00 and would submit
    # an order into a closing/closed auction.
    ms = market_status(_et(2025, 12, 24, 12, 55))
    assert ms.status == "open"
    assert ms.minutes_to_close == 5


def test_half_day_afterhours_after_1pm():
    # After the 13:00 half-day close it must be afterhours, not "open with hours left".
    ms = market_status(_et(2025, 12, 24, 13, 30))
    assert ms.status == "afterhours"


def test_full_day_still_uses_4pm_close():
    # A normal day's close is unchanged at 16:00.
    ms = market_status(_et(2026, 6, 5, 15, 55))
    assert ms.status == "open"
    assert ms.minutes_to_close == 5


def test_closed_overnight():
    ms = market_status(_et(2026, 6, 5, 2, 0))
    assert ms.status == "closed"
    assert ms.next_open is not None


def test_closed_on_weekend():
    ms = market_status(_et(2026, 6, 6, 11, 0))  # Saturday RTH-ish
    assert ms.status == "closed"


def test_closed_on_holiday():
    ms = market_status(_et(2026, 12, 25, 11, 0))
    assert ms.status == "closed"


def test_next_open_skips_weekend():
    ms = market_status(_et(2026, 6, 6, 11, 0))  # Sat
    assert ms.next_open is not None
    assert ms.next_open.weekday() == 0  # Monday
    assert ms.next_open.tzinfo == ZoneInfo("America/New_York")


# Official NYSE 2028 calendar — New Year's Day is Saturday (no observed closure);
# Christmas Eve is Sunday (no trading session / early close).
HOLIDAYS_2028 = (
    date(2028, 1, 17),  # MLK Day
    date(2028, 2, 21),  # Presidents' Day
    date(2028, 4, 14),  # Good Friday
    date(2028, 5, 29),  # Memorial Day
    date(2028, 6, 19),  # Juneteenth
    date(2028, 7, 4),  # Independence Day
    date(2028, 9, 4),  # Labor Day
    date(2028, 11, 23),  # Thanksgiving
    date(2028, 12, 25),  # Christmas
)


@pytest.mark.parametrize("d", HOLIDAYS_2028)
def test_2028_holidays(d):
    assert is_holiday(d)


def test_2028_new_years_day_saturday_not_observed():
    # Saturday Jan 1 — NYSE has no Friday-observed closure for this year.
    assert not is_holiday(date(2028, 1, 1))


@pytest.mark.parametrize(
    "d",
    (
        date(2028, 7, 3),  # pre-July-4 early close
        date(2028, 11, 24),  # day after Thanksgiving early close
    ),
)
def test_2028_early_closes(d):
    assert close_time_for(d).hour == 13
    ms = market_status(_et(d.year, d.month, d.day, 12, 55))
    assert ms.status == "open"
    assert ms.minutes_to_close == 5


def test_2028_christmas_eve_sunday_not_early_close():
    assert close_time_for(date(2028, 12, 24)).hour == 16
