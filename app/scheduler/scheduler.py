"""Market hours, due-time decisions and a single-run lock for collection jobs.

systemd timers start a short-lived job every few minutes; each job decides here
whether it is due, so intervals can be changed from the dashboard (app settings)
without touching the timer.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Delayed providers publish the closing price some minutes after the close
# (ASX closing auction ends around 16:12).
POST_CLOSE_DELAY = timedelta(minutes=30)


@dataclass(frozen=True)
class MarketSession:
    """Regular weekday trading hours in the exchange's own timezone.

    Public holidays are not modelled: on a holiday a few unnecessary requests are
    made and return the previous close.
    """

    name: str
    timezone: str
    opens: time
    closes: time

    def _local(self, now: datetime) -> datetime:
        return now.astimezone(ZoneInfo(self.timezone))

    def is_open(self, now: datetime) -> bool:
        local = self._local(now)
        return local.weekday() < 5 and self.opens <= local.time() < self.closes

    def last_close(self, now: datetime) -> datetime:
        """The most recent weekday close at or before ``now``, in UTC."""
        local = self._local(now)
        day = local.date()
        while True:
            close = datetime.combine(day, self.closes, ZoneInfo(self.timezone))
            if day.weekday() < 5 and close <= local:
                return close.astimezone(timezone.utc)
            day -= timedelta(days=1)


ASX = MarketSession("ASX", "Australia/Sydney", time(10, 0), time(16, 0))
US = MarketSession("US", "America/New_York", time(9, 30), time(16, 0))


def is_due(
    *,
    now: datetime,
    interval: timedelta,
    last_success: datetime | None,
    session: MarketSession | None = None,
) -> bool:
    """Whether a collection should run now.

    Without a market session (e.g. Bitcoin, 24/7) it runs every ``interval``.
    With one, it runs every ``interval`` while the market is open, plus once after
    each close to capture the closing price, and not at all otherwise.
    """
    if last_success is None:
        return True
    if session is None or session.is_open(now):
        return now - last_success >= interval
    after_close = session.last_close(now) + POST_CLOSE_DELAY
    return after_close <= now and last_success < after_close


class JobAlreadyRunning(RuntimeError):
    pass


@contextmanager
def single_run_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock so overlapping timer runs don't duplicate work."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise JobAlreadyRunning(f"Another collection job holds {lock_path}") from exc
        yield
    finally:
        os.close(handle)
