"""Phase 2 spike diagnostics (docs/spikes-runbook.md).

Mounted only when DIAGNOSTICS_ENABLED=true outside production (app.main + production guard).
Requires a real sign-in. Runs as the caller against Power BI, which enforces the caller's own
permissions; results go to a local, gitignored capture bundle.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse, ServerSentEvent

from app.auth.dependencies import CurrentContext
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.diagnostics.capture import CaptureBundle
from app.diagnostics.runner import DiagnosticsRunner
from app.powerbi.fabric_iq import FabricIqMcpGateway

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])

STREAM_EVENTS = 10
STREAM_INTERVAL_SECONDS = 0.3


class RunRequest(BaseModel):
    model_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    value_term: str | None = Field(default=None, max_length=100)
    sample_dax: str | None = Field(default=None, max_length=4000)


class VisualReport(BaseModel):
    bundle_id: str | None = Field(default=None, max_length=40)
    report: dict[str, Any]


def _root(request: Request) -> Path:
    settings: Settings = request.app.state.settings
    return Path(settings.diagnostics_capture_dir)


@router.post("/run")
async def run(body: RunRequest, request: Request, ctx: CurrentContext) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    bundle = CaptureBundle.create(_root(request), ctx.user.key)
    fabric = FabricIqMcpGateway(settings)
    async with httpx.AsyncClient(timeout=settings.powerbi_query_timeout_seconds) as http:
        runner = DiagnosticsRunner(settings, request.app.state.token_broker, fabric, http)
        return await runner.run(
            ctx,
            bundle,
            model_id=body.model_id,
            origin=request.headers.get("origin"),
            value_term=body.value_term,
            sample_dax=body.sample_dax,
        )


@router.get("/stream")
async def stream(ctx: CurrentContext) -> EventSourceResponse:
    """S6: ten events 300 ms apart; the visual records when each one arrives."""

    async def events() -> AsyncIterator[ServerSentEvent]:
        started = time.perf_counter()
        for index in range(STREAM_EVENTS):
            sent_ms = int((time.perf_counter() - started) * 1000)
            yield ServerSentEvent(
                data=f'{{"index": {index}, "sent_ms": {sent_ms}}}', event="tick", id=str(index + 1)
            )
            await asyncio.sleep(STREAM_INTERVAL_SECONDS)
        yield ServerSentEvent(data="{}", event="done", id=str(STREAM_EVENTS + 1))

    return EventSourceResponse(
        events(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.post("/visual")
async def visual_report(
    body: VisualReport, request: Request, ctx: CurrentContext
) -> dict[str, str]:
    """S5/S6: what the visual itself observed (host, SSO status, dataView summary, timings)."""
    root = _root(request)
    if body.bundle_id:
        found = CaptureBundle.open(root, body.bundle_id, ctx.user.key)
        if found is None:  # unknown, malformed, or another user's bundle
            raise AppError(ErrorCode.NOT_FOUND, "Unknown diagnostics bundle.", 404)
        bundle = found
    else:
        bundle = CaptureBundle.create(root, ctx.user.key)
    bundle.write_json(
        "visual_report.json", {**body.report, "origin": request.headers.get("origin")}
    )
    return {"bundle_id": bundle.id}
