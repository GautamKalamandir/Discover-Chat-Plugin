import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db.models import AccessStatus, AuditOutcome, MessageRole, QueryStatus, User
from app.db.repositories.audit import AuditRepository
from app.db.repositories.chat import ChatRepository
from app.db.repositories.queries import QueryExecutionRepository
from app.db.repositories.registry import RegistryRepository
from app.db.repositories.users import UserRepository
from app.retention import scheduler as scheduler_module
from app.retention.cleanup import CLEANUP_LOCK_KEY, CleanupResult, run_cleanup
from app.retention.scheduler import CleanupScheduler
from tests.db import BACKEND_DIR
from tests.test_repositories import identity

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


def settings(conversation_hours: int = 12, audit_hours: int = 2160) -> Settings:
    return Settings(
        _env_file=None,
        conversation_retention_hours=conversation_hours,
        audit_retention_hours=audit_hours,
    )


async def count(db: AsyncSession, table: str) -> int:
    return int(await db.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608


async def session_idle_for(db: AsyncSession, owner: User, hours: float) -> None:
    repo = ChatRepository(db, retention_hours=10_000)
    chat = await repo.create_session(owner)
    message = await repo.add_message(chat, MessageRole.USER, "q")
    await QueryExecutionRepository(db).record(
        chat,
        gateway="rest",
        dax="EVALUATE {1}",
        status=QueryStatus.SUCCEEDED,
        message_id=message.id,
    )
    await db.execute(
        text("UPDATE chat_sessions SET last_activity_at = :t WHERE id = :id"),
        {"t": NOW - timedelta(hours=hours), "id": chat.id},
    )


async def test_idle_conversations_are_deleted_with_their_messages_and_queries(
    db_session: AsyncSession,
) -> None:
    owner = await UserRepository(db_session).upsert_from_identity(identity())
    await session_idle_for(db_session, owner, hours=13)  # expired
    await session_idle_for(db_session, owner, hours=2)  # active

    result = await run_cleanup(db_session, settings(12), now=NOW)

    assert result.chat_sessions == 1
    assert await count(db_session, "chat_sessions") == 1
    assert await count(db_session, "chat_messages") == 1
    assert await count(db_session, "query_executions") == 1


@pytest.mark.parametrize(("hours", "deleted"), [(12, 1), (24, 0)])
async def test_retention_window_follows_configuration(
    db_session: AsyncSession, hours: int, deleted: int
) -> None:
    owner = await UserRepository(db_session).upsert_from_identity(identity())
    await session_idle_for(db_session, owner, hours=18)

    result = await run_cleanup(db_session, settings(conversation_hours=hours), now=NOW)

    assert result.chat_sessions == deleted


async def test_audit_events_use_their_own_window(db_session: AsyncSession) -> None:
    repo = AuditRepository(db_session)
    for age_hours in (1, 30, 3000):
        event = await repo.record("auth.failure", AuditOutcome.FAILURE)
        event.occurred_at = NOW - timedelta(hours=age_hours)
    await db_session.flush()

    result = await run_cleanup(db_session, settings(conversation_hours=12, audit_hours=24), now=NOW)

    assert result.audit_events == 2
    assert await count(db_session, "audit_events") == 1


async def test_expired_access_cache_rows_are_deleted(db_session: AsyncSession) -> None:
    registry = RegistryRepository(db_session)
    user = await UserRepository(db_session).upsert_from_identity(identity())
    ws = await registry.upsert_workspace(pbi_workspace_id="w", entra_tenant_id="t", name="W")
    for dataset, expires in (
        ("old", NOW - timedelta(minutes=1)),
        ("new", NOW + timedelta(hours=1)),
    ):
        model = await registry.upsert_semantic_model(
            pbi_dataset_id=dataset, workspace=ws, name=dataset
        )
        await registry.put_access(
            user_id=user.id,
            semantic_model_id=model.id,
            status=AccessStatus.ALLOWED,
            source="rest",
            verified_at=NOW,
            expires_at=expires,
        )

    result = await run_cleanup(db_session, settings(), now=NOW)

    assert result.access_cache == 1


async def test_only_long_inactive_users_without_conversations_are_deleted(
    db_session: AsyncSession,
) -> None:
    users = UserRepository(db_session)
    stale = await users.upsert_from_identity(identity("stale"))
    with_chat = await users.upsert_from_identity(identity("with-chat"))
    await users.upsert_from_identity(identity("recent"))
    await ChatRepository(db_session, retention_hours=12).create_session(with_chat)
    await db_session.execute(
        text("UPDATE users SET last_seen_at = :t WHERE id IN (:a, :b)"),
        {"t": NOW - timedelta(hours=5000), "a": stale.id, "b": with_chat.id},
    )

    result = await run_cleanup(db_session, settings(), now=NOW)

    assert result.users == 1
    remaining = await db_session.scalars(text("SELECT entra_object_id FROM users ORDER BY 1"))
    assert list(remaining) == ["recent", "with-chat"]


async def test_cleanup_is_skipped_while_another_instance_holds_the_lock(
    db_session: AsyncSession, test_database_url: str
) -> None:
    owner = await UserRepository(db_session).upsert_from_identity(identity())
    await session_idle_for(db_session, owner, hours=100)
    other_instance = create_async_engine(test_database_url, poolclass=NullPool)
    try:
        async with other_instance.connect() as conn, conn.begin():
            await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CLEANUP_LOCK_KEY})

            result = await run_cleanup(db_session, settings(), now=NOW)
    finally:
        await other_instance.dispose()

    assert result == CleanupResult(ran=False)
    assert await count(db_session, "chat_sessions") == 1


def test_standalone_cleanup_command_succeeds(test_database_url: str) -> None:
    env = {**os.environ, "DATABASE_URL": test_database_url, "LOG_LEVEL": "INFO"}

    completed = subprocess.run(
        [sys.executable, "-m", "app.jobs.cleanup"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Retention cleanup" in completed.stdout


async def test_scheduler_keeps_running_after_a_failed_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def flaky_cleanup(*_: object) -> CleanupResult:
        calls.append("run")
        if len(calls) == 1:
            raise ConnectionError("database briefly unavailable")
        return CleanupResult(ran=True)

    monkeypatch.setattr(scheduler_module, "run_cleanup_once", flaky_cleanup)
    scheduler = CleanupScheduler(None, settings(), interval_seconds=0.01)  # type: ignore[arg-type]

    scheduler.start()
    for _ in range(100):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    await scheduler.stop()

    assert len(calls) >= 3
