"""/summary reports over HTTP (ADR 0015, design §4): background builds, reuse, progress, owner-only
access, retention, PDF and one-time links. Real app (dev sign-in), test database, dev_synthetic
data, a prompt-routing LLM, report files in a temporary folder.

user-a may use Sales and HR, user-b only Sales, user-c nothing.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError, ErrorCode
from app.llm.base import ChatMessage, LLMResponse
from app.main import create_app
from app.reports.builder import NOTHING_LOADED
from app.reports.manager import TIMEOUT_MESSAGE
from app.semantic.schema_source import DevFixtureSchemaSource
from tests.authz_helpers import seed_registry
from tests.semantic_helpers import FIXTURES, HashEmbedder, fixture
from tests.test_chat_api import headers, parse_sse, settings
from tests.test_report_builder import ReportLLM, llm

pytestmark = pytest.mark.integration


class SlowLLM(ReportLLM):
    """Every answer takes `delay` seconds (time limit and shutdown tests)."""

    def __init__(self, delay: float) -> None:
        super().__init__(**llm().handlers)
        self.delay = delay

    async def complete(
        self, messages: Sequence[ChatMessage], *, json_mode: bool = False
    ) -> LLMResponse:
        await asyncio.sleep(self.delay)
        return await super().complete(messages, json_mode=json_mode)


@dataclass
class Api:
    app: Any
    client: AsyncClient
    llm: ReportLLM
    storage: Path

    @property
    def sm(self) -> async_sessionmaker[AsyncSession]:
        sessionmaker: async_sessionmaker[AsyncSession] = self.app.state.db_sessionmaker
        return sessionmaker

    async def summary(self, oid: str, *, regenerate: bool = False) -> Any:
        response = await self.client.post(
            "/api/v1/reports/summary", headers=headers(oid), json={"regenerate": regenerate}
        )
        return response

    async def events(self, oid: str, report_id: str) -> list[dict[str, Any]]:
        response = await self.client.get(
            f"/api/v1/reports/{report_id}/events", headers=headers(oid)
        )
        assert response.status_code == 200, response.text
        return parse_sse(response.text)

    async def built(self, oid: str, *, regenerate: bool = False) -> str:
        """Starts (or reuses) a report and waits until its build has finished."""
        started = await self.summary(oid, regenerate=regenerate)
        assert started.status_code == 200, started.text
        report_id = str(started.json()["report_id"])
        await self.events(oid, report_id)
        return report_id

    async def get(self, oid: str | None, path: str) -> Any:
        return await self.client.get(path, headers=headers(oid) if oid else {})

    async def scalar(self, sql: str, **params: Any) -> Any:
        async with self.sm() as session:
            return await session.scalar(text(sql), params)


@pytest.fixture
async def make_api(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    test_database_url: str,
    tmp_path: Path,
) -> AsyncIterator[Any]:
    await seed_registry(committed_sessionmaker)
    built: list[Api] = []

    async def build(model: ReportLLM | None = None, **overrides: Any) -> Api:
        model = model or llm()
        app = create_app(
            settings(test_database_url, report_storage_dir=str(tmp_path), **overrides),
            llm=model,
            embedder=HashEmbedder(),
            schema_source=DevFixtureSchemaSource(str(FIXTURES)),
        )
        for dataset in ("sales-ds", "hr-ds", "finance-ds"):
            await app.state.metadata_sync.sync_payload(dataset, fixture(dataset))
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        api = Api(app, client, model, tmp_path)
        built.append(api)
        return api

    yield build
    for api in built:
        await api.app.state.report_manager.aclose()
        await api.client.aclose()
        await api.app.state.db_engine.dispose()


# --- building and reuse -------------------------------------------------------------------------


async def test_summary_builds_in_the_background_and_streams_progress(make_api: Any) -> None:
    api = await make_api()

    started = await api.summary("user-a")
    assert started.status_code == 200
    body = started.json()
    assert (body["status"], body["reused"]) == ("building", False)

    events = await api.events("user-a", body["report_id"])

    names = [e["event"] for e in events]
    assert names[0] == "progress" and names[-1] == "done"
    assert events[-1]["data"] == {"status": "ready", "report_id": body["report_id"]}
    done = [e["data"] for e in events if e["event"] == "model_done"]
    assert {(d["model_name"], d["status"]) for d in done} == {("Sales", "ok"), ("HR", "no_data")}
    assert sorted(d["done"] for d in done) == [1, 2] and {d["total"] for d in done} == {2}
    stages = [e["data"]["stage"] for e in events if e["event"] == "progress"]
    assert stages[0] == "starting" and "rendering_pdf" in stages
    assert [e["id"] for e in events] == list(range(1, len(events) + 1))


async def test_a_ready_report_has_its_content_files_and_history_row(make_api: Any) -> None:
    api = await make_api()
    report_id = await api.built("user-a")

    response = await api.get("user-a", f"/api/v1/reports/{report_id}")

    assert response.status_code == 200
    detail = response.json()
    assert detail["status"] == "ready"
    assert [(s["name"], s["status"]) for s in detail["content"]["sections"]] == [
        ("HR", "no_data"),
        ("Sales", "ok"),
    ]
    assert detail["content"]["summary"] == "All good today."
    assert detail["headline"] == "All good today."
    assert detail["suggestion_counts"] == {"high": 1, "medium": 0, "low": 1}
    assert [m["name"] for m in detail["models"]] == ["HR", "Sales"]
    files = sorted(p.name for p in api.storage.rglob("*") if p.is_file())
    assert files == ["report.json", "report.pdf"]


async def test_the_same_day_reuses_the_report_and_regenerate_builds_a_new_one(
    make_api: Any,
) -> None:
    api = await make_api()
    first = await api.built("user-a")
    planned = api.llm.count("kpis")

    again = (await api.summary("user-a")).json()
    assert again == {"report_id": first, "status": "ready", "reused": True}
    assert api.llm.count("kpis") == planned  # nothing rebuilt

    second = await api.built("user-a", regenerate=True)

    assert second != first
    listed = (await api.get("user-a", "/api/v1/reports")).json()["reports"]
    assert [r["id"] for r in listed] == [second, first]  # newest first
    assert (await api.summary("user-a")).json()["report_id"] == second


async def test_concurrent_summaries_share_one_build(make_api: Any) -> None:
    api = await make_api()

    first, second = await asyncio.gather(api.summary("user-a"), api.summary("user-a"))

    assert first.json()["report_id"] == second.json()["report_id"]
    await api.events("user-a", first.json()["report_id"])
    assert await api.scalar("SELECT count(*) FROM summary_reports") == 1


async def test_a_finished_build_replays_its_stored_status(make_api: Any) -> None:
    api = await make_api()
    report_id = await api.built("user-a")
    await asyncio.sleep(0)  # let the finished task be forgotten

    events = await api.events("user-a", report_id)

    assert [(e["event"], e["data"]["status"]) for e in events] == [("done", "ready")]


# --- failures -----------------------------------------------------------------------------------


async def test_a_failed_build_says_why_and_the_next_summary_tries_again(make_api: Any) -> None:
    def broken(_: str, __: Sequence[ChatMessage]) -> Any:
        raise AppError(ErrorCode.LLM_UNAVAILABLE, "down", 503)

    api = await make_api(llm(kpis=broken))
    started = (await api.summary("user-b")).json()

    events = await api.events("user-b", started["report_id"])

    assert events[-2] == {
        "event": "error",
        "data": {"code": "all_models_failed", "message": NOTHING_LOADED},
        "id": len(events) - 1,
    }
    assert events[-1]["data"]["status"] == "failed"
    detail = (await api.get("user-b", f"/api/v1/reports/{started['report_id']}")).json()
    assert (detail["status"], detail["error_message"], detail["content"]) == (
        "failed",
        NOTHING_LOADED,
        None,
    )
    assert not any(p.is_file() for p in api.storage.rglob("*"))  # nothing left behind
    retry = (await api.summary("user-b")).json()
    assert retry["report_id"] != started["report_id"] and retry["status"] == "building"


async def test_a_build_over_the_time_limit_is_stopped(make_api: Any) -> None:
    api = await make_api(SlowLLM(delay=5), report_build_timeout_seconds=1)
    started = (await api.summary("user-b")).json()

    events = await api.events("user-b", started["report_id"])

    assert ("error", "build_timeout", TIMEOUT_MESSAGE) in [
        (e["event"], e["data"].get("code"), e["data"].get("message")) for e in events
    ]
    status = await api.scalar(
        "SELECT status FROM summary_reports WHERE id = :i", i=uuid.UUID(started["report_id"])
    )
    assert status == "failed"


async def test_shutdown_cancels_running_builds_and_marks_them_failed(make_api: Any) -> None:
    api = await make_api(SlowLLM(delay=30))
    started = (await api.summary("user-b")).json()
    await asyncio.sleep(0.2)

    await api.app.state.report_manager.aclose()

    row = await api.scalar(
        "SELECT error_code FROM summary_reports WHERE id = :i", i=uuid.UUID(started["report_id"])
    )
    assert row == "cancelled"


async def test_a_user_without_data_sources_is_told_before_any_build(make_api: Any) -> None:
    api = await make_api()

    response = await api.summary("user-c")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "report_failed"
    assert "No data sources are available to you" in response.json()["error"]["message"]
    assert await api.scalar("SELECT count(*) FROM summary_reports") == 0


# --- access -------------------------------------------------------------------------------------


@pytest.mark.scenario(22)
async def test_reports_are_visible_to_their_owner_only(make_api: Any) -> None:
    api = await make_api()
    report_id = await api.built("user-a")

    for path in (
        f"/api/v1/reports/{report_id}",
        f"/api/v1/reports/{report_id}/events",
        f"/api/v1/reports/{report_id}/pdf",
        f"/api/v1/reports/{uuid.uuid4()}",
    ):
        response = await api.get("user-b", path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "report_not_found"
    link = await api.client.post(
        f"/api/v1/reports/{report_id}/download-link", headers=headers("user-b")
    )
    assert link.status_code == 404
    assert (await api.get("user-b", "/api/v1/reports")).json() == {"reports": []}


async def test_reports_past_retention_are_hidden(make_api: Any) -> None:
    api = await make_api()
    report_id = await api.built("user-a")
    async with api.sm() as session, session.begin():
        await session.execute(
            text("UPDATE summary_reports SET created_at = now() - interval '8 days'")
        )

    assert (await api.get("user-a", f"/api/v1/reports/{report_id}")).status_code == 404
    assert (await api.get("user-a", "/api/v1/reports")).json() == {"reports": []}
    assert (await api.summary("user-a")).json()["reused"] is False  # a new one is built


@pytest.mark.scenario(10)
async def test_signing_in_is_required(make_api: Any) -> None:
    api = await make_api()

    assert (await api.get(None, "/api/v1/reports")).status_code == 401
    assert (await api.client.post("/api/v1/reports/summary", json={})).status_code == 401


async def test_the_reports_api_is_off_when_disabled(make_api: Any) -> None:
    api = await make_api(reports_enabled=False)

    assert (await api.summary("user-a")).status_code == 404


# --- PDF and one-time links ---------------------------------------------------------------------


async def test_the_pdf_downloads_as_an_attachment_and_is_audited(make_api: Any) -> None:
    api = await make_api()
    report_id = await api.built("user-a")

    response = await api.get("user-a", f"/api/v1/reports/{report_id}/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith('attachment; filename="summary-')
    assert response.content.startswith(b"%PDF-")
    audited = await api.scalar(
        "SELECT count(*) FROM audit_events WHERE event_type = 'report.downloaded' "
        "AND details->>'report_id' = :r AND details->>'tool' = 'pdf'",
        r=report_id,
    )
    assert audited == 1


async def test_a_one_time_link_downloads_once_without_signing_in(make_api: Any) -> None:
    api = await make_api()
    report_id = await api.built("user-a")

    link = await api.client.post(
        f"/api/v1/reports/{report_id}/download-link", headers=headers("user-a")
    )
    assert link.status_code == 200
    url_path, expires_in = link.json()["url_path"], link.json()["expires_in"]
    assert url_path.startswith("/api/v1/reports/download/") and expires_in == 60

    first = await api.get(None, url_path)
    second = await api.get(None, url_path)

    assert first.status_code == 200 and first.content.startswith(b"%PDF-")
    assert first.headers["cache-control"] == "no-store"
    assert second.status_code == 404
    token = url_path.rsplit("/", 1)[1]
    stored = await api.scalar(
        "SELECT count(*) FROM report_download_tokens WHERE token_hash = :t", t=token
    )
    assert stored == 0  # only the hash is stored


@pytest.mark.parametrize("token", ["not-a-token", "x" * 500])
async def test_invalid_links_are_not_found(make_api: Any, token: str) -> None:
    api = await make_api()

    response = await api.get(None, f"/api/v1/reports/download/{token}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "report_not_found"


async def test_builds_are_audited_with_ids_and_counts_only(make_api: Any) -> None:
    api = await make_api()
    report_id = await api.built("user-a")

    async with api.sm() as session:
        row = (
            await session.execute(
                text("SELECT outcome, details FROM audit_events WHERE event_type = 'report.built'")
            )
        ).one()
    assert row.outcome == "success"
    assert row.details == {"report_id": report_id, "model_ids": ["sales-ds"], "count": 2}
