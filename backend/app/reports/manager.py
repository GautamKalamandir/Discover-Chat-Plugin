"""Runs /summary builds in the background and relays their progress (ADR 0015 §14, design §1).

- One build at a time per user: the database's partial unique index decides, so it holds across
  backend instances. A second `/summary` while one runs attaches to the running build.
- The build keeps going when the visual closes, until REPORT_BUILD_TIMEOUT_SECONDS. It runs with
  the request's AuthorizedContext: every query is still made as the user (their cached Power BI
  token covers about an hour, well beyond the time limit).
- Progress is kept per build on the instance that runs it; a follower on another instance gets
  the stored status instead.
- On success: report.json and report.pdf are written, then the row is marked ready. On any failure
  the row is marked failed and partial files are removed. Audit events carry ids and counts only.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.help import NO_SOURCES
from app.authz.models import AuthorizedContext
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.db.models import AuditOutcome, User
from app.db.repositories.audit import AuditRepository
from app.db.repositories.reports import ReportRepository
from app.reports.builder import NOTHING_LOADED, ProgressCallback, ReportBuildError, ReportProgress
from app.reports.pdf import render_pdf
from app.reports.sales.builder import NO_SALES_MODELS
from app.reports.sales.content import ReportContent, SalesReportContent
from app.reports.sales.pdf import render_sales_pdf
from app.reports.store import ReportStore

logger = logging.getLogger(__name__)

TIMEOUT_MESSAGE = "Building the report took too long. Please try again."
FAILED_MESSAGE = "The report couldn't be built. Please try again."
# User-safe text for a failed report's stored error code.
FAILURE_MESSAGES = {
    "no_models": NO_SOURCES,
    "all_models_failed": NOTHING_LOADED,
    "no_sales_models": NO_SALES_MODELS,
    "build_timeout": TIMEOUT_MESSAGE,
}


def failure_message(error_code: str | None) -> str:
    return FAILURE_MESSAGES.get(error_code or "", FAILED_MESSAGE)


class ContentBuilder(Protocol):
    """Builds a report's content as the user: /sales-summary (ADR 0016) or the schema-2 report."""

    async def build(
        self,
        authz: AuthorizedContext,
        report_id: uuid.UUID,
        *,
        user_name: str | None,
        progress: ProgressCallback | None = None,
    ) -> ReportContent: ...


def render(content: ReportContent, *, company_name: str, logo_path: str | None) -> bytes:
    if isinstance(content, SalesReportContent):
        return render_sales_pdf(content, company_name=company_name, logo_path=logo_path)
    return render_pdf(content, company_name=company_name, logo_path=logo_path)


@dataclass(frozen=True)
class StartResult:
    report_id: uuid.UUID
    status: Literal["building", "ready"]
    reused: bool


@dataclass
class _Channel:
    """A build's events, replayed to every follower from the start."""

    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def publish(self, event: dict[str, Any], *, last: bool = False) -> None:
        async with self.changed:
            self.events.append(event)
            self.done = self.done or last
            self.changed.notify_all()

    async def follow(self) -> AsyncIterator[dict[str, Any]]:
        sent = 0

        def ready() -> bool:
            return sent < len(self.events) or self.done

        while True:
            async with self.changed:
                await self.changed.wait_for(ready)
                batch, finished = self.events[sent:], self.done
            sent += len(batch)
            for event in batch:
                yield event
            if finished and sent >= len(self.events):
                return


class ReportBuildManager:
    def __init__(
        self,
        builder: ContentBuilder,
        store: ReportStore,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._builder = builder
        self._store = store
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._today = today or (lambda: datetime.now(settings.report_zone).date())
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._channels: dict[uuid.UUID, _Channel] = {}

    def repository(self, session: AsyncSession) -> ReportRepository:
        return ReportRepository(
            session,
            retention_days=self._settings.report_retention_days,
            build_timeout_seconds=self._settings.report_build_timeout_seconds,
        )

    async def start(self, authz: AuthorizedContext, *, regenerate: bool) -> StartResult:
        """Today's ready report (unless `regenerate`), the running build, or a new build."""
        if not authz.allowed:
            raise AppError(ErrorCode.REPORT_FAILED, NO_SOURCES, 422, log_detail="no models")
        day = self._today()
        async with self._sessionmaker() as session, session.begin():
            user = await session.get(User, authz.user_id)
            if user is None:  # pragma: no cover - build_context always upserts the user
                raise AppError(ErrorCode.INTERNAL_ERROR, FAILED_MESSAGE, 500)
            repo = self.repository(session)
            if not regenerate and (ready := await repo.latest_ready_for_day(user, day)):
                return StartResult(ready.id, "ready", reused=True)
            report, created = await repo.start_build(user, day)
        if created:
            channel = _Channel()
            self._channels[report.id] = channel
            task = asyncio.create_task(
                self._build(authz, report.id, channel), name=f"report-{report.id}"
            )
            self._tasks[report.id] = task
            task.add_done_callback(lambda _: self._forget(report.id))
        return StartResult(report.id, "building", reused=False)

    def follow(self, report_id: uuid.UUID) -> AsyncIterator[dict[str, Any]] | None:
        """Progress of a build running on this instance; None when it isn't (finished, or on
        another instance): the caller then reports the stored status."""
        channel = self._channels.get(report_id)
        return channel.follow() if channel is not None else None

    async def aclose(self) -> None:
        """Shutdown: running builds are cancelled and marked failed."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _forget(self, report_id: uuid.UUID) -> None:
        self._tasks.pop(report_id, None)
        self._channels.pop(report_id, None)

    # --- the build ------------------------------------------------------------------------------

    async def _build(
        self, authz: AuthorizedContext, report_id: uuid.UUID, channel: _Channel
    ) -> None:
        user = authz.request.user

        async def progress(event: ReportProgress) -> None:
            data: dict[str, Any] = {"type": "progress", "stage": event.stage}
            if event.stage == "model_done":
                data = {
                    "type": "model_done",
                    "model_name": event.model_name,
                    "status": event.status,
                }
            elif event.model_name:
                data["model_name"] = event.model_name
            if event.total is not None:  # data sources finished so far, out of how many
                data["done"], data["total"] = event.done, event.total
            await channel.publish(data)

        try:
            async with asyncio.timeout(self._settings.report_build_timeout_seconds):
                await progress(ReportProgress("starting"))
                content = await self._builder.build(
                    authz,
                    report_id,
                    user_name=user.display_name or user.username,
                    progress=progress,
                )
                await progress(ReportProgress("rendering_pdf"))
                pdf = await asyncio.to_thread(
                    render,
                    content,
                    company_name=self._settings.report_company_name,
                    logo_path=self._settings.report_logo_path,
                )
                json_path = await self._store.write(
                    authz.user_id, report_id, "report.json", content.model_dump_json().encode()
                )
                pdf_path = await self._store.write(authz.user_id, report_id, "report.pdf", pdf)
            if not await self._ready(authz, report_id, content, json_path, pdf_path):
                # The row stopped being "building" meanwhile (failed as lost): don't claim success.
                await self._failed(authz, report_id, channel, "build_lost", FAILED_MESSAGE)
                return
        except TimeoutError:
            await self._failed(authz, report_id, channel, "build_timeout", TIMEOUT_MESSAGE)
            return
        except ReportBuildError as exc:
            await self._failed(authz, report_id, channel, exc.reason, exc.message)
            return
        except AppError as exc:
            await self._failed(authz, report_id, channel, str(exc.code), exc.message)
            return
        except asyncio.CancelledError:
            await asyncio.shield(
                self._failed(authz, report_id, channel, "cancelled", FAILED_MESSAGE)
            )
            raise
        except Exception:
            logger.exception("Report %s failed", report_id)
            await self._failed(authz, report_id, channel, "internal_error", FAILED_MESSAGE)
            return
        await channel.publish(
            {"type": "done", "status": "ready", "report_id": str(report_id)}, last=True
        )

    async def _ready(
        self,
        authz: AuthorizedContext,
        report_id: uuid.UUID,
        content: ReportContent,
        json_path: str,
        pdf_path: str,
    ) -> bool:
        counts = content.suggestion_counts
        models = content.model_status()
        async with self._sessionmaker() as session, session.begin():
            marked = await self.repository(session).mark_ready(
                report_id,
                models=models,
                headline=_headline(content),
                suggestion_counts=counts,
                json_path=json_path,
                pdf_path=pdf_path,
            )
            if not marked:
                return False
            await AuditRepository(session).record(
                "report.built",
                AuditOutcome.SUCCESS,
                user=authz.request.user,
                correlation_id=authz.request.correlation_id,
                details={
                    "report_id": str(report_id),
                    "model_ids": [m["dataset_id"] for m in models if m["status"] == "ok"],
                    "count": sum(counts.values()),
                },
            )
        logger.info("Report %s ready", report_id)
        return True

    async def _failed(
        self,
        authz: AuthorizedContext,
        report_id: uuid.UUID,
        channel: _Channel,
        error_code: str,
        message: str,
    ) -> None:
        try:
            await self._store.delete_report(authz.user_id, report_id)
        except OSError:
            logger.exception("Could not remove partial files of report %s", report_id)
        try:
            async with self._sessionmaker() as session, session.begin():
                await self.repository(session).mark_failed(report_id, error_code)
                await AuditRepository(session).record(
                    "report.built",
                    AuditOutcome.FAILURE,
                    user=authz.request.user,
                    correlation_id=authz.request.correlation_id,
                    details={"report_id": str(report_id), "error_code": error_code[:64]},
                )
        except Exception:  # the deletion job fails builds left behind
            logger.exception("Could not mark report %s failed", report_id)
        logger.info("Report %s failed: %s", report_id, error_code)
        await channel.publish({"type": "error", "code": error_code, "message": message})
        await channel.publish(
            {"type": "done", "status": "failed", "report_id": str(report_id)}, last=True
        )


def _headline(content: ReportContent) -> str:
    """The history list's one line: the summary's first sentence (its first bullet)."""
    lines = content.summary.strip().splitlines()
    first = lines[0].split(". ", 1)[0].strip() if lines else ""
    if not first:
        return ""
    return first if first.endswith(".") else f"{first}."
