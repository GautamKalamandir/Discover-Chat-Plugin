"""/summary reports API (ADR 0015, design §4).

- POST /reports/summary starts or reuses today's report (gate G1: built from the user's allowed
  models, as the user).
- Everything else reads the caller's own reports only. Missing, expired and other users' reports
  all return the same 404. Stored reports are not re-checked against current model access
  (ADR 0015 §11, accepted exception).
- GET /reports/download/{token} is the only route without a bearer token: a one-time link that
  opens in a browser tab when Power BI blocks downloads from visuals.
"""

import itertools
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sse_starlette import EventSourceResponse, ServerSentEvent

from app.auth.dependencies import CurrentContext
from app.auth.models import AuthenticatedUser
from app.authz.dependencies import AuthorizedDep
from app.chat.streaming import encode
from app.core.config import Settings
from app.db.models import AuditOutcome, ReportStatus, SummaryReport
from app.db.repositories.audit import AuditRepository
from app.db.repositories.reports import report_not_found
from app.db.repositories.users import UserRepository
from app.reports.manager import ReportBuildManager, failure_message
from app.reports.models import SummaryReportContent
from app.reports.store import ReportStore

router = APIRouter(prefix="/reports", tags=["reports"])

MAX_TOKEN_CHARS = 128


class SummaryRequest(BaseModel):
    regenerate: bool = False


class SummaryStarted(BaseModel):
    report_id: uuid.UUID
    status: Literal["building", "ready"]
    reused: bool


class ReportItem(BaseModel):
    id: uuid.UUID
    status: Literal["building", "ready", "failed"]
    report_date: date
    created_at: datetime
    completed_at: datetime | None
    headline: str | None
    models: list[dict[str, Any]]
    suggestion_counts: dict[str, int] | None
    error_message: str | None = None


class ReportList(BaseModel):
    reports: list[ReportItem]


class ReportDetail(ReportItem):
    content: SummaryReportContent | None = None


class DownloadLink(BaseModel):
    url_path: str
    expires_in: int


# --- helpers -------------------------------------------------------------------------------------


def _manager(request: Request) -> ReportBuildManager:
    manager: ReportBuildManager = request.app.state.report_manager
    return manager


def _store(request: Request) -> ReportStore:
    store: ReportStore = request.app.state.report_store
    return store


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def _owned(
    request: Request, identity: AuthenticatedUser, report_id: uuid.UUID
) -> SummaryReport:
    async with request.app.state.db_sessionmaker() as session:
        user = await UserRepository(session).get_by_identity(identity)
        if user is None:
            raise report_not_found()
        return await _manager(request).repository(session).get_owned(report_id, user)


def _item(report: SummaryReport) -> dict[str, Any]:
    return {
        "id": report.id,
        "status": report.status.value,
        "report_date": report.report_date,
        "created_at": report.created_at,
        "completed_at": report.completed_at,
        "headline": report.headline,
        "models": report.models,
        "suggestion_counts": report.suggestion_counts,
        "error_message": (
            failure_message(report.error_code) if report.status is ReportStatus.FAILED else None
        ),
    }


def _pdf_response(report: SummaryReport, pdf: bytes) -> Response:
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="summary-{report.report_date}.pdf"',
            "Cache-Control": "no-store",
        },
    )


async def _audit_download(
    request: Request, report: SummaryReport, tool: str, user: AuthenticatedUser | None
) -> None:
    async with request.app.state.db_sessionmaker() as session, session.begin():
        await AuditRepository(session).record(
            "report.downloaded",
            AuditOutcome.SUCCESS,
            user=user,
            details={"report_id": str(report.id), "tool": tool},
        )


# --- routes -------------------------------------------------------------------------------------


@router.post("/summary")
async def start_summary(
    body: SummaryRequest, request: Request, authz: AuthorizedDep
) -> SummaryStarted:
    """`/summary` in the chat: today's report, the running build, or a new build."""
    started = await _manager(request).start(authz, regenerate=body.regenerate)
    return SummaryStarted(report_id=started.report_id, status=started.status, reused=started.reused)


@router.get("")
async def list_reports(request: Request, ctx: CurrentContext) -> ReportList:
    """The caller's reports within the retention window, newest first (Previous reports)."""
    async with request.app.state.db_sessionmaker() as session:
        user = await UserRepository(session).get_by_identity(ctx.user)
        if user is None:
            return ReportList(reports=[])
        reports = await _manager(request).repository(session).list_recent(user)
    return ReportList(reports=[ReportItem(**_item(r)) for r in reports])


@router.get("/download/{token}")
async def download_with_link(token: str, request: Request) -> Response:
    """One-time link (no bearer token): valid once, for REPORT_DOWNLOAD_LINK_SECONDS."""
    if len(token) > MAX_TOKEN_CHARS:
        raise report_not_found()
    async with request.app.state.db_sessionmaker() as session, session.begin():
        report = await _manager(request).repository(session).consume_download_token(token)
    if report.pdf_path is None:  # pragma: no cover - ready reports always have a PDF
        raise report_not_found()
    pdf = await _store(request).read(report.pdf_path)
    await _audit_download(request, report, "link", None)
    return _pdf_response(report, pdf)


@router.get("/{report_id}")
async def get_report(report_id: uuid.UUID, request: Request, ctx: CurrentContext) -> ReportDetail:
    report = await _owned(request, ctx.user, report_id)
    content = None
    if report.status is ReportStatus.READY and report.json_path:
        raw = await _store(request).read(report.json_path)
        content = SummaryReportContent.model_validate_json(raw)
    return ReportDetail(**_item(report), content=content)


@router.get("/{report_id}/events")
async def report_events(
    report_id: uuid.UUID, request: Request, ctx: CurrentContext
) -> EventSourceResponse:
    """Build progress as Server-Sent Events: progress, model_done, error, done."""
    report = await _owned(request, ctx.user, report_id)
    events = _manager(request).follow(report.id)

    async def stream() -> AsyncIterator[ServerSentEvent]:
        seq = itertools.count(1)
        if events is not None:
            async for event in events:
                data = dict(event)
                yield encode(str(data.pop("type")), data, next(seq))
            return
        # Not running here: finished already, or building on another backend instance.
        if report.status is ReportStatus.FAILED:
            message = failure_message(report.error_code)
            yield encode("error", {"code": report.error_code, "message": message}, next(seq))
        yield encode("done", {"status": report.status.value, "report_id": report.id}, next(seq))

    return EventSourceResponse(
        stream(),
        ping=_settings(request).chat_heartbeat_seconds,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{report_id}/pdf")
async def get_pdf(report_id: uuid.UUID, request: Request, ctx: CurrentContext) -> Response:
    """For the visual's download API (the PDF is passed on as base64)."""
    report = await _owned(request, ctx.user, report_id)
    if report.status is not ReportStatus.READY or report.pdf_path is None:
        raise report_not_found()
    pdf = await _store(request).read(report.pdf_path)
    await _audit_download(request, report, "pdf", ctx.user)
    return _pdf_response(report, pdf)


@router.post("/{report_id}/download-link")
async def create_download_link(
    report_id: uuid.UUID, request: Request, ctx: CurrentContext
) -> DownloadLink:
    """A one-time link for when Power BI blocks downloads from visuals."""
    report = await _owned(request, ctx.user, report_id)
    if report.status is not ReportStatus.READY:
        raise report_not_found()
    seconds = _settings(request).report_download_link_seconds
    async with request.app.state.db_sessionmaker() as session, session.begin():
        token = (
            await _manager(request)
            .repository(session)
            .create_download_token(report, ttl_seconds=seconds)
        )
    return DownloadLink(url_path=f"/api/v1/reports/download/{token}", expires_in=seconds)
