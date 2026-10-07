"""Chat API (ADR 0009, Q17: one conversation per visual + "New chat").

Everything that can be rejected is rejected before the stream starts, as a normal JSON error
(401/404/409/422/429). Once streaming, problems arrive as `error` events.
"""

import itertools
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse, ServerSentEvent

from app.agent.orchestrator import Agent
from app.authz import messages
from app.authz.dependencies import AuthorizedDep, AuthzService
from app.authz.models import AuthorizedContext
from app.authz.service import AuthorizationService
from app.chat.streaming import agent_event, encode
from app.chat.turns import TurnGuard, with_time_limit
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.request_context import get_correlation_id
from app.db.models import ChatSession, MessageRole, User
from app.db.repositories.chat import ChatRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

Scalar = str | int | float | bool


class ReportFilter(BaseModel):
    column: str = Field(min_length=1, max_length=300)
    values: list[Scalar] = Field(max_length=50)


class StreamRequest(BaseModel):
    session_id: uuid.UUID | None = None
    question: str = Field(min_length=1, max_length=20_000)
    primary_model_id: str | None = Field(default=None, max_length=100)
    report_filters: list[ReportFilter] = Field(default_factory=list, max_length=20)


class NewSessionRequest(BaseModel):
    primary_model_id: str | None = Field(default=None, max_length=100)


class SessionOut(BaseModel):
    session_id: uuid.UUID
    primary_model_id: str | None
    created_at: datetime
    last_activity_at: datetime


class MessageOut(BaseModel):
    id: uuid.UUID
    role: Literal["user", "assistant"]
    kind: Literal["question", "answer", "clarification", "notice"]
    content: str
    created_at: datetime


class SessionDetail(SessionOut):
    messages: list[MessageOut]


# --- helpers -------------------------------------------------------------------------------------


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _repo(session: Any, settings: Settings) -> ChatRepository:
    return ChatRepository(session, retention_hours=settings.conversation_retention_hours)


async def _authorize_primary(
    authz: AuthorizedContext, service: AuthorizationService, model_id: str | None
) -> AuthorizedContext:
    """G2 for the Format-pane model; unknown/forbidden look identical (generic 404)."""
    if model_id is None:
        return authz
    try:
        return await service.assert_allowed(authz, [model_id])
    except AppError as exc:
        if exc.code is ErrorCode.MODEL_ACCESS_DENIED:
            raise messages.model_not_found(exc.log_detail or "denied") from exc
        raise


def _dataset_for(authz: AuthorizedContext, model_uuid: uuid.UUID | None) -> str | None:
    for model in authz.allowed.values():
        if model.model_uuid == model_uuid:
            return model.dataset_id
    return None  # primary model no longer allowed: simply not used


def _session_out(authz: AuthorizedContext, chat: ChatSession) -> SessionOut:
    return SessionOut(
        session_id=chat.id,
        primary_model_id=_dataset_for(authz, chat.primary_semantic_model_id),
        created_at=chat.created_at,
        last_activity_at=chat.last_activity_at,
    )


async def _owned_session(
    request: Request, authz: AuthorizedContext, session_id: uuid.UUID
) -> tuple[ChatSession, User]:
    settings = _settings(request)
    async with request.app.state.db_sessionmaker() as db:
        user = await db.get(User, authz.user_id)
        if user is None:  # pragma: no cover - build_context always upserts the user
            raise AppError(ErrorCode.SESSION_NOT_FOUND, "Conversation not found.", 404)
        return await _repo(db, settings).get_owned_session(session_id, user), user


async def _create_session(
    request: Request, authz: AuthorizedContext, primary_model_id: str | None
) -> ChatSession:
    model_uuid = authz.allowed[primary_model_id].model_uuid if primary_model_id else None
    async with request.app.state.db_sessionmaker() as db, db.begin():
        user = await db.get(User, authz.user_id)
        assert user is not None  # noqa: S101 - build_context always upserts the user
        return await _repo(db, _settings(request)).create_session(
            user, primary_semantic_model_id=model_uuid, report_hint="visual"
        )


# --- routes -------------------------------------------------------------------------------------


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def new_session(
    body: NewSessionRequest, request: Request, authz: AuthorizedDep, service: AuthzService
) -> SessionOut:
    """The visual's "New chat" button."""
    authz = await _authorize_primary(authz, service, body.primary_model_id)
    return _session_out(authz, await _create_session(request, authz, body.primary_model_id))


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: uuid.UUID, request: Request, authz: AuthorizedDep
) -> SessionDetail:
    """Restores a conversation after Power BI re-renders the visual. Tables are not stored."""
    chat, _ = await _owned_session(request, authz, session_id)
    settings = _settings(request)
    async with request.app.state.db_sessionmaker() as db:
        stored = await _repo(db, settings).list_messages(
            chat, limit=settings.agent_history_turns * 2
        )
    out = []
    for message in stored:
        context = message.resolved_context or {}
        if message.role is MessageRole.USER:
            kind = "question"
        elif "plan" in context:
            kind = "answer"
        elif context.get("status") == "clarify":
            kind = "clarification"
        else:
            kind = "notice"
        out.append(
            MessageOut(
                id=message.id,
                role=message.role.value,
                kind=kind,
                content=message.content,
                created_at=message.created_at,
            )
        )
    return SessionDetail(**_session_out(authz, chat).model_dump(), messages=out)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: uuid.UUID, request: Request, authz: AuthorizedDep) -> Response:
    chat, _ = await _owned_session(request, authz, session_id)
    async with request.app.state.db_sessionmaker() as db, db.begin():
        await _repo(db, _settings(request)).delete_session(chat)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/stream")
async def stream(
    body: StreamRequest, request: Request, authz: AuthorizedDep, service: AuthzService
) -> EventSourceResponse:
    settings = _settings(request)
    question = body.question.strip()
    if not question or len(question) > settings.agent_max_question_chars:
        raise AppError(
            ErrorCode.QUESTION_INVALID,
            f"Please ask a question of 1 to {settings.agent_max_question_chars} characters.",
            422,
        )
    authz = await _authorize_primary(authz, service, body.primary_model_id)

    if body.session_id is None:
        chat = await _create_session(request, authz, body.primary_model_id)
    else:
        chat, _ = await _owned_session(request, authz, body.session_id)
        if body.primary_model_id:
            async with request.app.state.db_sessionmaker() as db, db.begin():
                await _repo(db, settings).set_primary_model(
                    chat, authz.allowed[body.primary_model_id].model_uuid
                )
    primary = body.primary_model_id or _dataset_for(authz, chat.primary_semantic_model_id)

    guard: TurnGuard = request.app.state.turn_guard
    ticket = guard.acquire(authz.request.user.key, chat.id)  # 409 / 429 before streaming
    agent: Agent = request.app.state.agent
    correlation_id = get_correlation_id()

    async def events() -> AsyncIterator[ServerSentEvent]:
        seq = itertools.count(1)
        try:
            yield encode(
                "session",
                {"session_id": chat.id, "correlation_id": correlation_id},
                next(seq),
            )
            turn = agent.run(
                authz,
                chat,
                question,
                primary_model_id=primary,
                report_filters=[f.model_dump() for f in body.report_filters] or None,
            )
            async for event in with_time_limit(turn, settings.chat_turn_timeout_seconds):
                yield agent_event(event, next(seq))
        finally:
            guard.release(ticket)
            logger.info("Chat turn stream closed for session %s", chat.id)

    return EventSourceResponse(
        events(),
        ping=settings.chat_heartbeat_seconds,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
