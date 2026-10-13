"""Daily deletion of /summary reports (ADR 0015 §12).

- reports built more than REPORT_RETENTION_DAYS x 24 h ago: files first, then the row (its download
  links and query records cascade). A folder that can't be deleted keeps its row, so the next run
  retries it.
- builds still "building" well past REPORT_BUILD_TIMEOUT_SECONDS (lost by a crash) are failed and
  their partial files removed
- expired or used download links
- report folders with no row (left by a crash between writing files and saving the row)

Safe with several backend instances: a Postgres advisory lock lets one instance run per round.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ReportDownloadToken, ReportStatus, SummaryReport
from app.db.repositories.reports import STALE_BUILD_GRACE
from app.reports.store import ReportStore

logger = logging.getLogger(__name__)

# Arbitrary constant identifying the report cleanup's Postgres advisory lock.
REPORT_CLEANUP_LOCK_KEY = 0x445245504F52  # "DREPOR"


@dataclass(frozen=True)
class ReportCleanupResult:
    ran: bool  # False when another instance held the lock
    reports: int = 0
    kept_for_retry: int = 0  # files that couldn't be deleted; the rows stay for the next run
    lost_builds: int = 0
    download_links: int = 0
    orphan_folders: int = 0


async def run_report_cleanup(
    session: AsyncSession, store: ReportStore, settings: Settings, *, now: datetime | None = None
) -> ReportCleanupResult:
    """Runs inside the caller's transaction; the caller commits."""
    acquired = (
        await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": REPORT_CLEANUP_LOCK_KEY}
        )
    ).scalar_one()
    if not acquired:
        logger.info("Report cleanup skipped: another instance is running it")
        return ReportCleanupResult(ran=False)

    if now is None:
        now = (await session.execute(select(func.now()))).scalar_one()
    cutoff = now - timedelta(days=settings.report_retention_days)

    lost = await _fail_lost_builds(session, store, settings, now)

    expired = (
        await session.execute(
            select(SummaryReport.id, SummaryReport.user_id).where(SummaryReport.created_at < cutoff)
        )
    ).all()
    deletable: list[uuid.UUID] = []
    for report_id, user_id in expired:
        try:
            await store.delete_report(user_id, report_id)
        except OSError:
            logger.exception("Could not delete files of report %s; will retry next run", report_id)
            continue
        deletable.append(report_id)
    if deletable:
        await session.execute(delete(SummaryReport).where(SummaryReport.id.in_(deletable)))

    links = cast(
        CursorResult[Any],
        await session.execute(
            delete(ReportDownloadToken).where(
                or_(ReportDownloadToken.expires_at < now, ReportDownloadToken.used_at.is_not(None))
            )
        ),
    )
    orphans = await _delete_orphans(session, store, cutoff)

    result = ReportCleanupResult(
        ran=True,
        reports=len(deletable),
        kept_for_retry=len(expired) - len(deletable),
        lost_builds=lost,
        download_links=int(links.rowcount or 0),
        orphan_folders=orphans,
    )
    logger.info(
        "Report cleanup: reports=%d kept_for_retry=%d lost_builds=%d download_links=%d "
        "orphan_folders=%d (retention=%dd)",
        result.reports,
        result.kept_for_retry,
        result.lost_builds,
        result.download_links,
        result.orphan_folders,
        settings.report_retention_days,
    )
    return result


async def _fail_lost_builds(
    session: AsyncSession, store: ReportStore, settings: Settings, now: datetime
) -> int:
    time_limit = timedelta(seconds=settings.report_build_timeout_seconds)
    stale_before = now - time_limit - STALE_BUILD_GRACE
    lost = (
        await session.execute(
            update(SummaryReport)
            .where(
                SummaryReport.status == ReportStatus.BUILDING,
                SummaryReport.created_at < stale_before,
            )
            .values(status=ReportStatus.FAILED, error_code="build_lost", completed_at=now)
            .returning(SummaryReport.id, SummaryReport.user_id)
        )
    ).all()
    for report_id, user_id in lost:
        try:
            await store.delete_report(user_id, report_id)
        except OSError:
            logger.exception("Could not delete partial files of report %s", report_id)
    return len(lost)


async def _delete_orphans(session: AsyncSession, store: ReportStore, cutoff: datetime) -> int:
    folders = await store.folders_older_than(cutoff)
    if not folders:
        return 0
    known = set(
        (
            await session.scalars(
                select(SummaryReport.id).where(SummaryReport.id.in_([r for _, r in folders]))
            )
        ).all()
    )
    deleted = 0
    for user_id, report_id in folders:
        if report_id in known:
            continue  # its row is handled above (or kept for retry)
        try:
            await store.delete_report(user_id, report_id)
            deleted += 1
        except OSError:
            logger.exception("Could not delete orphan report folder %s", report_id)
    return deleted
