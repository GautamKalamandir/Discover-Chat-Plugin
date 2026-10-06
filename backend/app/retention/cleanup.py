"""Deletes data older than the configured retention windows (ADR 0004).

- chat_sessions idle longer than CONVERSATION_RETENTION_HOURS (messages and query executions
  cascade with them)
- audit_events older than AUDIT_RETENTION_HOURS
- expired authorization-cache rows (they are re-verified against Power BI anyway)
- users with no remaining conversations who haven't been seen within either window
- semantic documents not seen by any metadata sync for METADATA_STALE_DAYS (ADR 0007)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Delete, delete, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import AuditEvent, ChatSession, SemanticDocument, User, UserModelAccess

logger = logging.getLogger(__name__)

# Arbitrary constant identifying the cleanup job's Postgres advisory lock.
CLEANUP_LOCK_KEY = 0x44434C45414E  # "DCLEAN"


@dataclass(frozen=True)
class CleanupResult:
    ran: bool  # False when another instance held the lock
    chat_sessions: int = 0
    audit_events: int = 0
    access_cache: int = 0
    users: int = 0
    semantic_documents: int = 0


async def run_cleanup(
    session: AsyncSession, settings: Settings, *, now: datetime | None = None
) -> CleanupResult:
    """Runs inside the caller's transaction; the caller commits. Returns what was deleted."""
    acquired = (
        await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": CLEANUP_LOCK_KEY}
        )
    ).scalar_one()
    if not acquired:
        logger.info("Retention cleanup skipped: another instance is running it")
        return CleanupResult(ran=False)

    if now is None:
        now = (await session.execute(select(func.now()))).scalar_one()
    conversation_cutoff = now - timedelta(hours=settings.conversation_retention_hours)
    audit_cutoff = now - timedelta(hours=settings.audit_retention_hours)
    user_cutoff = min(conversation_cutoff, audit_cutoff)

    sessions = await _delete(
        session, delete(ChatSession).where(ChatSession.last_activity_at < conversation_cutoff)
    )
    audits = await _delete(session, delete(AuditEvent).where(AuditEvent.occurred_at < audit_cutoff))
    access = await _delete(session, delete(UserModelAccess).where(UserModelAccess.expires_at < now))
    users = await _delete(
        session,
        delete(User).where(
            User.last_seen_at < user_cutoff,
            ~exists().where(ChatSession.user_id == User.id),
        ),
    )

    documents = await _delete(
        session,
        delete(SemanticDocument).where(
            SemanticDocument.last_seen_at < now - timedelta(days=settings.metadata_stale_days)
        ),
    )

    result = CleanupResult(
        ran=True,
        chat_sessions=sessions,
        audit_events=audits,
        access_cache=access,
        users=users,
        semantic_documents=documents,
    )
    logger.info(
        "Retention cleanup: sessions=%d audit_events=%d access_cache=%d users=%d "
        "semantic_documents=%d (conversation=%dh audit=%dh)",
        sessions,
        audits,
        access,
        users,
        documents,
        settings.conversation_retention_hours,
        settings.audit_retention_hours,
    )
    return result


async def _delete(session: AsyncSession, stmt: Delete) -> int:
    result = cast(CursorResult[Any], await session.execute(stmt))
    return int(result.rowcount or 0)
