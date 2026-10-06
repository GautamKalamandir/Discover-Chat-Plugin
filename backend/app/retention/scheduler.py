"""Built-in periodic cleanup (CLEANUP_SCHEDULER_ENABLED / CLEANUP_INTERVAL_MINUTES).

Safe with several backend instances: `run_cleanup` takes a Postgres advisory lock, so only one
instance deletes per round. The same job is available as `python -m app.jobs.cleanup` for an
external scheduler (OS cron, container/cloud jobs).
"""

import asyncio
import logging
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.retention.cleanup import CleanupResult, run_cleanup

logger = logging.getLogger(__name__)


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
