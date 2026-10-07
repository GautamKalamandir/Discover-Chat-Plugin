import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.db.models import ChatMessage, ChatSession, MessageRole, User


def session_not_found() -> AppError:
    # Same response whether the session never existed, expired, or belongs to someone else,
    # so callers can't probe for other users' session ids (security scenario 22).
    return AppError(
        ErrorCode.SESSION_NOT_FOUND,
        "This conversation was not found or has expired. Please start a new chat.",
        404,
    )


class ChatRepository:
    def __init__(self, session: AsyncSession, *, retention_hours: int) -> None:
        self._session = session
        self._retention = timedelta(hours=retention_hours)

    async def create_session(
        self,
        owner: User,
        *,
        primary_semantic_model_id: uuid.UUID | None = None,
        report_hint: str | None = None,
        title: str | None = None,
    ) -> ChatSession:
        chat = ChatSession(
            user_id=owner.id,
            primary_semantic_model_id=primary_semantic_model_id,
            report_hint=report_hint,
            title=title,
        )
        self._session.add(chat)
        await self._session.flush()
        await self._session.refresh(chat)
        return chat

    async def get_owned_session(
        self, session_id: uuid.UUID, owner: User, *, now: datetime | None = None
    ) -> ChatSession:
        """Returns the session only if `owner` owns it and it is within the retention window.

        The window is enforced here too, so data the cleanup job hasn't removed yet is
        already unreachable.
        """
        cutoff = (now or await self._db_now()) - self._retention
        stmt = select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == owner.id,
            ChatSession.last_activity_at >= cutoff,
        )
        chat = (await self._session.scalars(stmt)).one_or_none()
        if chat is None:
            raise session_not_found()
        return chat

    async def set_primary_model(self, chat: ChatSession, semantic_model_id: uuid.UUID) -> None:
        if chat.primary_semantic_model_id != semantic_model_id:
            chat.primary_semantic_model_id = semantic_model_id
            await self._session.execute(
                update(ChatSession)
                .where(ChatSession.id == chat.id)
                .values(primary_semantic_model_id=semantic_model_id)
            )

    async def delete_session(self, chat: ChatSession) -> None:
        """Messages and query records cascade (ON DELETE CASCADE)."""
        await self._session.execute(delete(ChatSession).where(ChatSession.id == chat.id))

    async def add_message(
        self,
        chat: ChatSession,
        role: MessageRole,
        content: str,
        *,
        resolved_context: dict[str, Any] | None = None,
    ) -> ChatMessage:
        message = ChatMessage(
            session_id=chat.id, role=role, content=content, resolved_context=resolved_context
        )
        self._session.add(message)
        await self._session.execute(
            update(ChatSession).where(ChatSession.id == chat.id).values(last_activity_at=func.now())
        )
        await self._session.flush()
        return message

    async def list_messages(self, chat: ChatSession, *, limit: int = 50) -> Sequence[ChatMessage]:
        """Most recent `limit` messages, oldest first."""
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == chat.id)
            .order_by(ChatMessage.seq.desc())
            .limit(limit)
        )
        messages = list((await self._session.scalars(stmt)).all())
        messages.reverse()
        return messages

    async def _db_now(self) -> datetime:
        now: datetime = (await self._session.execute(select(func.now()))).scalar_one()
        return now
