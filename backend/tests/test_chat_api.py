"""Chat API over HTTP (ADR 0009): SSE framing, sessions, scenario 22, limits, timeout,
disconnect."""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.events import AgentEvent, DoneEvent, StatusEvent
from app.auth.dev import DEV_AUDIENCE, DEV_ISSUER
from app.chat.turns import TurnGuard, with_time_limit
from app.core.config import Settings
from app.core.errors import AppError
from app.main import create_app
from app.semantic.schema_source import DevFixtureSchemaSource
from tests.authz_helpers import seed_registry
from tests.conftest import DEV_SECRET
from tests.semantic_helpers import FIXTURES, HashEmbedder, fixture
from tests.test_agent_pipeline import GOLD_STEP, ScriptedLLM, grounded_answer, plan

pytestmark = pytest.mark.integration

ACCESS = {"user-a": ["sales-ds", "hr-ds"], "user-b": ["sales-ds"]}


def headers(oid: str) -> dict[str, str]:
    now = int(time.time())
    claims = {
        "iss": DEV_ISSUER,
        "aud": DEV_AUDIENCE,
        "exp": now + 600,
        "oid": oid,
        "tid": "dev-tenant",
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, DEV_SECRET, algorithm='HS256')}"}


def parse_sse(body: str) -> list[dict[str, Any]]:
    events = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        event: dict[str, Any] = {}
        for line in block.split("\n"):
            if line.startswith("event:"):
                event["event"] = line[6:].strip()
            elif line.startswith("data:"):
                event["data"] = json.loads(line[5:].strip())
            elif line.startswith("id:"):
                event["id"] = int(line[3:].strip())
        if "event" in event:
            events.append(event)
    return events


class Harness:
    def __init__(self, app: Any, llm: ScriptedLLM, client: AsyncClient) -> None:
        self.app, self.llm, self.client = app, llm, client

    async def ask(self, oid: str, question: str, **body: Any) -> tuple[int, list[dict[str, Any]]]:
        response = await self.client.post(
            "/api/v1/chat/stream",
            headers=headers(oid),
            json={"question": question, "primary_model_id": "sales-ds", **body},
        )
        if response.status_code != 200:
            return response.status_code, [response.json()]
        return 200, parse_sse(response.text)


def settings(test_database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "auth_provider": "dev",
        "dev_auth_secret": DEV_SECRET,
        "database_url": test_database_url,
        "cleanup_scheduler_enabled": False,
        "dev_model_access": ACCESS,
        "powerbi_gateway": "dev_synthetic",
        "powerbi_fallback_gateway": None,
        "metadata_sync_on_use": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
async def make_harness(
    committed_sessionmaker: async_sessionmaker[AsyncSession], test_database_url: str
) -> AsyncIterator[Any]:
    await seed_registry(committed_sessionmaker)
    apps: list[Any] = []

    async def build(**overrides: Any) -> Harness:
        llm = ScriptedLLM([])
        app = create_app(
            settings(test_database_url, **overrides),
            llm=llm,
            embedder=HashEmbedder(),
            schema_source=DevFixtureSchemaSource(str(FIXTURES)),
        )
        for dataset in ("sales-ds", "hr-ds", "finance-ds"):
            await app.state.metadata_sync.sync_payload(dataset, fixture(dataset))
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        apps.append((app, client))
        return Harness(app, llm, client)

    yield build
    for app, client in apps:
        await client.aclose()
        await app.state.db_engine.dispose()


async def count(sm: async_sessionmaker[AsyncSession], sql: str) -> int:
    async with sm() as session:
        return int(await session.scalar(text(sql)) or 0)


# --- streaming -----------------------------------------------------------------------------------


async def test_stream_frames_a_whole_turn_as_sse(make_harness: Any) -> None:
    h = await make_harness()
    h.llm.replies += [plan(GOLD_STEP), grounded_answer]

    status, events = await h.ask("user-a", "What are GOLD sales this FY?")

    assert status == 200
    names = [e["event"] for e in events]
    assert names[0] == "session" and names[-1] == "done"
    assert names.index("table") < names.index("token")
    assert [e["id"] for e in events] == list(range(1, len(events) + 1))
    answer = "".join(e["data"]["text"] for e in events if e["event"] == "token")
    assert answer.startswith("GOLD net sales this FY were")
    session_id = events[0]["data"]["session_id"]
    assert events[-1]["data"]["message_id"]

    detail = await h.client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers("user-a"))
    assert detail.status_code == 200
    body = detail.json()
    assert body["primary_model_id"] == "sales-ds"
    assert [(m["role"], m["kind"]) for m in body["messages"]] == [
        ("user", "question"),
        ("assistant", "answer"),
    ]


async def test_follow_up_continues_the_same_conversation(make_harness: Any) -> None:
    h = await make_harness()
    h.llm.replies += [plan(GOLD_STEP), grounded_answer, plan(GOLD_STEP), grounded_answer]
    _, first = await h.ask("user-a", "GOLD sales this FY?")
    session_id = first[0]["data"]["session_id"]

    _, second = await h.ask("user-a", "and last year?", session_id=session_id)

    assert second[0]["data"]["session_id"] == session_id
    assert "PREVIOUS TURN" in h.llm.calls[2][-1].content


async def test_new_chat_and_delete(make_harness: Any) -> None:
    h = await make_harness()
    created = await h.client.post(
        "/api/v1/chat/sessions", headers=headers("user-a"), json={"primary_model_id": "sales-ds"}
    )
    assert created.status_code == 201
    session_id = created.json()["session_id"]

    deleted = await h.client.delete(
        f"/api/v1/chat/sessions/{session_id}", headers=headers("user-a")
    )
    again = await h.client.delete(f"/api/v1/chat/sessions/{session_id}", headers=headers("user-a"))

    assert (deleted.status_code, again.status_code) == (204, 404)


# --- rejections before the stream starts ---------------------------------------------------------


@pytest.mark.scenario(10)
async def test_sign_in_required(make_harness: Any) -> None:
    h = await make_harness()

    response = await h.client.post("/api/v1/chat/stream", json={"question": "hi"})

    assert response.status_code == 401


@pytest.mark.parametrize("model_id", ["finance-ds", "no-such-model"])
async def test_forbidden_and_unknown_primary_model_look_identical(
    make_harness: Any, model_id: str
) -> None:
    h = await make_harness()

    status, body = await h.ask("user-a", "sales?", primary_model_id=model_id)

    assert status == 404
    assert body[0]["error"]["code"] == "model_not_found"
    assert "finance" not in json.dumps(body).lower()


@pytest.mark.scenario(22)
async def test_scenario_22_other_users_conversation_is_not_found(make_harness: Any) -> None:
    h = await make_harness()
    h.llm.replies += [plan(GOLD_STEP), grounded_answer]
    _, events = await h.ask("user-a", "GOLD sales?")
    session_id = events[0]["data"]["session_id"]

    stolen, body = await h.ask("user-b", "show me", session_id=session_id)
    peek = await h.client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers("user-b"))
    random = await h.client.get(f"/api/v1/chat/sessions/{uuid.uuid4()}", headers=headers("user-b"))

    assert (stolen, peek.status_code, random.status_code) == (404, 404, 404)
    assert body[0]["error"]["code"] == peek.json()["error"]["code"] == "session_not_found"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"question": "x" * 2001}, 422),
        ({"question": "   "}, 422),
        ({"question": "q", "report_filters": [{"column": "T[c]", "values": []}] * 21}, 422),
    ],
)
async def test_invalid_requests(make_harness: Any, body: dict[str, Any], status: int) -> None:
    h = await make_harness()

    response = await h.client.post("/api/v1/chat/stream", headers=headers("user-a"), json=body)

    assert response.status_code == status


async def test_second_question_in_a_busy_conversation_is_refused(make_harness: Any) -> None:
    h = await make_harness()
    created = await h.client.post("/api/v1/chat/sessions", headers=headers("user-a"), json={})
    session_id = uuid.UUID(created.json()["session_id"])
    h.app.state.turn_guard.acquire("dev-tenant:user-a", session_id)  # a turn is running

    status, body = await h.ask("user-a", "GOLD?", session_id=str(session_id))

    assert status == 409 and body[0]["error"]["code"] == "turn_in_progress"


@pytest.mark.scenario(19)
async def test_questions_per_minute_limit(make_harness: Any) -> None:
    h = await make_harness(chat_questions_per_minute=2)
    h.llm.replies += [plan(status="clarify", clarification="Which?")] * 2

    first, _ = await h.ask("user-a", "q1")
    second, _ = await h.ask("user-a", "q2")
    third, body = await h.ask("user-a", "q3")

    assert (first, second, third) == (200, 200, 429)
    assert body[0]["error"]["code"] == "too_many_questions"


# --- timeout and disconnect ----------------------------------------------------------------------


class BlockingAgent:
    """Emits one status event, then waits forever (until cancelled)."""

    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    async def run(self, *args: Any, **kwargs: Any) -> AsyncIterator[AgentEvent]:
        yield StatusEvent("understanding")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        yield DoneEvent(None)  # pragma: no cover


async def test_turn_timeout_cancels_the_agent(make_harness: Any) -> None:
    h = await make_harness(chat_turn_timeout_seconds=1)
    agent = BlockingAgent()
    h.app.state.agent = agent

    status, events = await h.ask("user-a", "slow question")

    assert status == 200
    assert [e["event"] for e in events][-2:] == ["error", "done"]
    assert events[-2]["data"]["code"] == "turn_timeout"
    assert agent.cancelled.is_set()
    assert h.app.state.turn_guard.active_count() == 0


async def test_client_disconnect_cancels_the_agent_and_frees_the_slot(
    make_harness: Any, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    h = await make_harness()
    agent = BlockingAgent()
    h.app.state.agent = agent
    body = json.dumps({"question": "slow", "primary_model_id": "sales-ds"}).encode()
    disconnect = asyncio.Event()
    chunks: list[bytes] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if b"event: status" in b"".join(chunks):
                disconnect.set()  # the user closes the visual mid-answer

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/chat/stream",
        "raw_path": b"/api/v1/chat/stream",
        "query_string": b"",
        "server": ("test", 80),
        "client": ("test", 1234),
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        + [(k.lower().encode(), v.encode()) for k, v in headers("user-a").items()],
    }

    await asyncio.wait_for(h.app(scope, receive, send), timeout=10)

    assert agent.cancelled.is_set()
    assert h.app.state.turn_guard.active_count() == 0
    # The (fake) agent never reached an answer, so nothing was stored as one.
    assert (
        await count(
            committed_sessionmaker, "SELECT count(*) FROM chat_messages WHERE role = 'assistant'"
        )
        == 0
    )


# --- unit: guard and time limit ------------------------------------------------------------------


def test_guard_limits_parallel_turns_and_expires_stale_slots() -> None:
    now = [0.0]
    guard = TurnGuard(
        Settings(_env_file=None, chat_max_concurrent_turns=2, chat_turn_timeout_seconds=10),
        clock=lambda: now[0],
    )
    guard.acquire("u", uuid.uuid4())
    guard.acquire("u", uuid.uuid4())

    with pytest.raises(AppError) as excinfo:
        guard.acquire("u", uuid.uuid4())
    assert excinfo.value.code == "too_many_parallel_questions"

    now[0] = 41.0  # timeout (10) + grace (30) passed: leaked slots are reclaimed
    guard.acquire("u", uuid.uuid4())


async def test_time_limit_relays_events_unchanged_when_fast() -> None:
    async def quick() -> AsyncIterator[AgentEvent]:
        yield StatusEvent("planning")
        yield DoneEvent(None)

    events = [e async for e in with_time_limit(quick(), 5)]

    assert [e.type for e in events] == ["status", "done"]


async def test_cors_preflight_from_the_sandboxed_visual(make_harness: Any) -> None:
    h = await make_harness()

    response = await h.client.options(
        "/api/v1/chat/stream",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "null"


def test_sse_encoding_handles_dates_decimals_and_uuids() -> None:
    from datetime import date
    from decimal import Decimal

    from app.agent.events import TableEvent
    from app.chat.streaming import agent_event

    event = TableEvent(
        "sales-ds",
        "t",
        ["d", "v"],
        [{"d": date(2026, 4, 1), "v": Decimal("1200.50"), "u": uuid.UUID(int=1)}],
        False,
    )

    data = json.loads(str(agent_event(event, 7).data))

    assert data["rows"] == [
        {"d": "2026-04-01", "v": "1200.50", "u": "00000000-0000-0000-0000-000000000001"}
    ]
    assert "type" not in data
