"""/summary reports and their one-time download links (ADR 0015).

A report is visible to its owner only, and only within REPORT_RETENTION_DAYS of being built: the
window is enforced here, so a report the daily deletion job hasn't removed yet is already
unreachable. Reports are deliberately not re-checked against current model access (ADR 0015 §11).
"""

import hashlib
import secrets
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.db.models import ReportDownloadToken, ReportStatus, SummaryReport, User

# A build whose row is still "building" this long after its time limit was lost (crash, restart).
STALE_BUILD_GRACE = timedelta(seconds=60)


def report_not_found() -> AppError:
    # Same response whether the report never existed, expired, or belongs to someone else.
    return AppError(
        ErrorCode.REPORT_NOT_FOUND,
        "This report was not found or has expired. Type /summary for today's report.",
        404,
    )


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ReportRepository:
    def __init__(
        self, session: AsyncSession, *, retention_days: int, build_timeout_seconds: int
    ) -> None:
        self._session = session
        self._retention = timedelta(days=retention_days)
        self._stale_after = timedelta(seconds=build_timeout_seconds) + STALE_BUILD_GRACE

    # --- builds ---------------------------------------------------------------------------------

    async def start_build(self, owner: User, report_date: date) -> tuple[SummaryReport, bool]:
        """A new `building` row, or the build already running for `owner` (created=False).

        The partial unique index makes this safe across backend instances. A build left in
        `building` past its time limit (the instance died) is failed first, so it can't block
        the user."""
        now = await self._db_now()
        await self._session.execute(
            update(SummaryReport)
            .where(
                SummaryReport.user_id == owner.id,
                SummaryReport.status == ReportStatus.BUILDING,
                SummaryReport.created_at < now - self._stale_after,
            )
            .values(status=ReportStatus.FAILED, error_code="build_lost", completed_at=now)
        )
        report = SummaryReport(
            user_id=owner.id, status=ReportStatus.BUILDING, report_date=report_date
        )
        try:
            async with self._session.begin_nested():
                self._session.add(report)
                await self._session.flush()
        except IntegrityError:
            running = await self.running_build(owner)
            if running is None:  # pragma: no cover - finished between the insert and this read
                raise
            return running, False
        await self._session.refresh(report)
        return report, True

    async def running_build(self, owner: User) -> SummaryReport | None:
        stmt = select(SummaryReport).where(
            SummaryReport.user_id == owner.id, SummaryReport.status == ReportStatus.BUILDING
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def mark_ready(
        self,
        report_id: uuid.UUID,
        *,
        models: list[dict[str, Any]],
        headline: str | None,
        suggestion_counts: dict[str, int],
        json_path: str,
        pdf_path: str,
    ) -> bool:
        """False when the row is no longer building (e.g. the deletion job failed it as lost)."""
        result = await self._session.execute(
            update(SummaryReport)
            .where(SummaryReport.id == report_id, SummaryReport.status == ReportStatus.BUILDING)
            .values(
                status=ReportStatus.READY,
                models=models,
                headline=(headline or "")[:300] or None,
                suggestion_counts=suggestion_counts,
                json_path=json_path,
                pdf_path=pdf_path,
                completed_at=await self._db_now(),
            )
        )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def mark_failed(
        self, report_id: uuid.UUID, error_code: str, *, models: list[dict[str, Any]] | None = None
    ) -> None:
        values: dict[str, Any] = {
            "status": ReportStatus.FAILED,
            "error_code": error_code[:64],
            "completed_at": await self._db_now(),
        }
        if models is not None:
            values["models"] = models
        await self._session.execute(
            update(SummaryReport)
            .where(SummaryReport.id == report_id, SummaryReport.status == ReportStatus.BUILDING)
            .values(**values)
        )

    # --- reading --------------------------------------------------------------------------------

    async def latest_ready_for_day(self, owner: User, day: date) -> SummaryReport | None:
        """Today's report to reuse (ADR 0015 §2): the newest ready one built on `day`."""
        stmt = (
            select(SummaryReport)
            .where(
                SummaryReport.user_id == owner.id,
                SummaryReport.status == ReportStatus.READY,
                SummaryReport.report_date == day,
                SummaryReport.created_at >= await self._cutoff(),
            )
            .order_by(SummaryReport.created_at.desc())
            .limit(1)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def list_recent(self, owner: User) -> Sequence[SummaryReport]:
        """The owner's reports within the retention window, newest first."""
        stmt = (
            select(SummaryReport)
            .where(
                SummaryReport.user_id == owner.id,
                SummaryReport.created_at >= await self._cutoff(),
            )
            .order_by(SummaryReport.created_at.desc())
        )
        return (await self._session.scalars(stmt)).all()

    async def get_owned(self, report_id: uuid.UUID, owner: User) -> SummaryReport:
        stmt = select(SummaryReport).where(
            SummaryReport.id == report_id,
            SummaryReport.user_id == owner.id,
            SummaryReport.created_at >= await self._cutoff(),
        )
        report = (await self._session.scalars(stmt)).one_or_none()
        if report is None:
            raise report_not_found()
        return report

    # --- one-time download links ----------------------------------------------------------------

    async def create_download_token(self, report: SummaryReport, *, ttl_seconds: int) -> str:
        """A random token for one download within `ttl_seconds`. Only its hash is stored."""
        token = secrets.token_urlsafe(32)
        self._session.add(
            ReportDownloadToken(
                token_hash=hash_token(token),
                report_id=report.id,
                user_id=report.user_id,
                expires_at=await self._db_now() + timedelta(seconds=ttl_seconds),
            )
        )
        await self._session.flush()
        return token

    async def consume_download_token(self, token: str) -> SummaryReport:
        """The ready report the token opens, marking the token used. Unknown, used and expired
        tokens, and reports past retention, all look the same (404)."""
        now = await self._db_now()
        used = await self._session.execute(
            update(ReportDownloadToken)
            .where(
                ReportDownloadToken.token_hash == hash_token(token),
                ReportDownloadToken.used_at.is_(None),
                ReportDownloadToken.expires_at > now,
            )
            .values(used_at=now)
            .returning(ReportDownloadToken.report_id)
        )
        report_id = used.scalar_one_or_none()
        if report_id is None:
            raise report_not_found()
        stmt = select(SummaryReport).where(
            SummaryReport.id == report_id,
            SummaryReport.status == ReportStatus.READY,
            SummaryReport.created_at >= now - self._retention,
        )
        report = (await self._session.scalars(stmt)).one_or_none()
        if report is None:
            raise report_not_found()
        return report

    async def _cutoff(self) -> datetime:
        return await self._db_now() - self._retention

    async def _db_now(self) -> datetime:
        now: datetime = (await self._session.execute(select(func.now()))).scalar_one()
        return now
