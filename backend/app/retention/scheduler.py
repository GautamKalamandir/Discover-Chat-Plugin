"""Built-in schedulers for the deletion jobs.

- Retention cleanup every CLEANUP_INTERVAL_MINUTES (ADR 0004); also `python -m app.jobs.cleanup`.
- /summary report deletion once a day at REPORT_CLEANUP_TIME in REPORT_TIMEZONE, and once at
  startup so a run missed while the backend was down catches up (ADR 0015); also
  `python -m app.jobs.report_cleanup`.

Safe with several backend instances: each job takes a Postgres advisory lock, so only one instance
deletes per round. The commands serve external schedulers (OS cron, container/cloud jobs).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.reports.cleanup import ReportCleanupResult, run_report_cleanup
from app.reports.store import ReportStore
from app.retention.cleanup import CleanupResult, run_cleanup

logger = logging.getLogger(__name__)

# After a failed report cleanup (e.g. database briefly down), try again this soon.
REPORT_CLEANUP_RETRY_SECONDS = 3600.0
# A daily run never follows the previous one sooner than this.
MIN_GAP_SECONDS = 60.0


async def run_cleanup_once(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> CleanupResult:
    async with sessionmaker() as session, session.begin():
        return await run_cleanup(session, settings)


class CleanupScheduler:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        interval_seconds: float | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._interval_seconds = interval_seconds or settings.cleanup_interval_minutes * 60
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="retention-cleanup")
            logger.info(
                "Retention cleanup scheduled every %d min", self._settings.cleanup_interval_minutes
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await run_cleanup_once(self._sessionmaker, self._settings)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed round (e.g. database briefly down) must not stop future rounds.
                logger.exception("Retention cleanup failed; will retry next interval")
            await asyncio.sleep(self._interval_seconds)


async def run_report_cleanup_once(
    sessionmaker: async_sessionmaker[AsyncSession], store: ReportStore, settings: Settings
) -> ReportCleanupResult:
    async with sessionmaker() as session, session.begin():
        return await run_report_cleanup(session, store, settings)


def seconds_until(at: time, zone: ZoneInfo, now: datetime) -> float:
    """Seconds from `now` (aware) to the next `at` wall-clock time in `zone`."""
    local = now.astimezone(zone)
    target = datetime.combine(local.date(), at, tzinfo=zone)
    if target <= local:
        target = datetime.combine(local.date() + timedelta(days=1), at, tzinfo=zone)
    # Subtract in UTC: aware datetimes sharing a tzinfo subtract as wall times.
    return max((target.astimezone(UTC) - now.astimezone(UTC)).total_seconds(), 0.0)


class ReportCleanupScheduler:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        store: ReportStore,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._store = store
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="report-cleanup")
            logger.info(
                "Report cleanup scheduled daily at %s %s",
                self._settings.report_cleanup_time,
                self._settings.report_timezone,
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:  # the first round runs at startup
            failed = False
            try:
                await run_report_cleanup_once(self._sessionmaker, self._store, self._settings)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed round must not stop future rounds; it is retried sooner than tomorrow.
                logger.exception("Report cleanup failed; will retry")
                failed = True
            delay = seconds_until(
                self._settings.report_cleanup_clock, self._settings.report_zone, self._clock()
            )
            if delay < MIN_GAP_SECONDS:  # woke a moment early: the next run is tomorrow's
                delay += 24 * 3600
            if failed:
                delay = min(delay, REPORT_CLEANUP_RETRY_SECONDS)
            await self._sleep(delay)
