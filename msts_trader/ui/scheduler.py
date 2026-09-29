"""In-process scheduler for `msts-trader ui`.

Every enabled strategy runs once per rebalance period (daily / weekly /
monthly / ...) at its schedule time (US/Eastern) on trading days — pulled
10 minutes before an early close on half-days. LIVE only when the strategy has
`live_enabled`; otherwise a dry-run preview is logged. "Already ran this
period" is read back from the run log, so a restart never double-runs; the
rebalance engine's own idempotency guard is the second line of defence.

It only runs while the UI process is up. For unattended trading use cron:
    msts-trader strategy run <id> --yes
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

from ..market_hours import ET, close_time_for, is_holiday, is_weekend
from ..symphony import runner, store
from ..symphony.model import Symphony

TICK_SECONDS = 20
RETRY_AFTER = timedelta(minutes=10)  # after an error, don't hammer data/broker every tick


def period_key(d: date, cadence: str):
    if cadence == "daily":
        return d.isoformat()
    if cadence == "weekly":
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]}"
    if cadence == "monthly":
        return f"{d.year}-{d.month:02d}"
    if cadence == "quarterly":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    return str(d.year)


def run_time(s: Symphony, d: date) -> datetime:
    hh, mm = (int(x) for x in s.deploy.schedule_time.split(":"))
    want = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)
    close = datetime.combine(d, close_time_for(d), tzinfo=ET)
    return min(want, close - timedelta(minutes=10))


def last_scheduled_period(s: Symphony) -> str | None:
    """Rebalance period stamped on the latest non-error scheduler run."""
    for e in store.read_runs(s.id, limit=500):
        if e.get("source") == "scheduler" and e.get("status") != "error" and e.get("period"):
            return e["period"]
    return None


def due(s: Symphony, now: datetime) -> bool:
    """`now` may be in any timezone (or naive = this machine's local time):
    everything is decided on the New York clock, so a machine in Sydney or
    London runs at 15:50 ET, and each side's DST change is handled."""
    if not s.deploy.schedule_enabled:
        return False
    now = now.astimezone(ET)
    today = now.date()
    if is_weekend(today) or is_holiday(today):
        return False
    close = datetime.combine(today, close_time_for(today), tzinfo=ET)
    if not (run_time(s, today) <= now < close):
        return False
    return last_scheduled_period(s) != period_key(today, s.rebalance)


def tz_status() -> dict:
    """Clock facts for the UI: the machine's zone vs the market's."""
    local = datetime.now().astimezone()
    et = local.astimezone(ET)
    return {
        "market_tz": "America/New_York",
        "market_now": et.isoformat(timespec="seconds"),
        "local_tz": local.tzname(),
        "local_now": local.isoformat(timespec="seconds"),
    }


class Scheduler:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick: str | None = None
        self.last_error: str | None = None
        self.running_now: str | None = None
        self._retry_at: dict[str, datetime] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="msts-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self, now: datetime | None = None) -> list[dict]:
        now = (now or datetime.now(ET)).astimezone(ET)
        self.last_tick = now.isoformat(timespec="seconds")
        results = []
        for s in store.list_all():
            if not due(s, now) or now < self._retry_at.get(s.id, now):
                continue
            mode = runner.LIVE if s.deploy.live_enabled else runner.DRY
            self.running_now = s.id
            try:
                res = runner.run(s, mode=mode, source="scheduler", tags={"period": period_key(now.date(), s.rebalance)})
                results.append(res)
                if res.get("status") == "error":
                    self._retry_at[s.id] = now + RETRY_AFTER
                    self.last_error = f"{s.id}: {res.get('error')}"
            except Exception as e:  # never let one strategy kill the loop
                self.last_error = f"{s.id}: {e}"
                self._retry_at[s.id] = now + RETRY_AFTER
                results.append(
                    store.log_run(
                        {"strategy": s.id, "mode": mode, "source": "scheduler", "status": "error", "error": str(e)}
                    )
                )
            finally:
                self.running_now = None
        return results

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.last_error = str(e)
            self._stop.wait(TICK_SECONDS)

    def state(self) -> dict:
        now = datetime.now(ET)
        upcoming = []
        for s in store.list_all():
            if s.deploy.schedule_enabled:
                d = now.date()
                for _ in range(10):
                    if not (is_weekend(d) or is_holiday(d)) and run_time(s, d) > now:
                        break
                    d += timedelta(days=1)
                upcoming.append(
                    {
                        "strategy": s.id,
                        "mode": "live" if s.deploy.live_enabled else "dry",
                        "next_check": run_time(s, d).isoformat(timespec="minutes"),
                        "cadence": s.rebalance,
                    }
                )
        return {
            **tz_status(),
            "running": bool(self._thread and self._thread.is_alive()),
            "last_tick": self.last_tick,
            "last_error": self.last_error,
            "running_now": self.running_now,
            "upcoming": upcoming,
        }
