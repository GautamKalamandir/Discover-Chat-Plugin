import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthenticatedUser
from app.core.errors import AppError
from app.db.models import (
    AccessStatus,
    AuditEvent,
    AuditOutcome,
    MessageRole,
    QueryExecution,
    QueryStatus,
    User,
)
from app.db.repositories.audit import AuditRepository
from app.db.repositories.chat import ChatRepository
from app.db.repositories.queries import QueryExecutionRepository
from app.db.repositories.registry import RegistryRepository
from app.db.repositories.users import UserRepository

pytestmark = pytest.mark.integration


def identity(oid: str = "user-a", tid: str = "tenant-1", name: str = "User A") -> AuthenticatedUser:
    return AuthenticatedUser(oid, tid, f"{oid}@contoso.com", name, frozenset(), None)


async def make_user(db: AsyncSession, oid: str = "user-a") -> User:
    return await UserRepository(db).upsert_from_identity(identity(oid))


# --- users --------------------------------------------------------------------------------------


async def test_user_upsert_is_idempotent_and_refreshes_profile(db_session: AsyncSession) -> None:
    repo = UserRepository(db_session)

    first = await repo.upsert_from_identity(identity(name="Old Name"))
    second = await repo.upsert_from_identity(identity(name="New Name"))

    assert first.id == second.id
    assert second.display_name == "New Name"
    count = await db_session.scalar(text("SELECT count(*) FROM users"))
    assert count == 1


async def test_same_object_id_in_other_tenant_is_a_different_user(
    db_session: AsyncSession,
) -> None:
    repo = UserRepository(db_session)

    a = await repo.upsert_from_identity(identity(tid="tenant-1"))
    b = await repo.upsert_from_identity(identity(tid="tenant-2"))

    assert a.id != b.id


# --- conversations and scenario 22 (session hijack) ---------------------------------------------


async def test_owner_can_load_their_session(db_session: AsyncSession) -> None:
    repo = ChatRepository(db_session, retention_hours=12)
    owner = await make_user(db_session)
    chat = await repo.create_session(owner, report_hint="Sales report")

    loaded = await repo.get_owned_session(chat.id, owner)

    assert loaded.id == chat.id


async def test_other_user_cannot_load_someone_elses_session(db_session: AsyncSession) -> None:
    repo = ChatRepository(db_session, retention_hours=12)
    owner = await make_user(db_session, "user-a")
    intruder = await make_user(db_session, "user-b")
    chat = await repo.create_session(owner)

    with pytest.raises(AppError) as stolen:
        await repo.get_owned_session(chat.id, intruder)
    with pytest.raises(AppError) as missing:
        await repo.get_owned_session(uuid.uuid4(), intruder)

    # Indistinguishable from a session that doesn't exist: no existence leak.
    assert stolen.value.status_code == missing.value.status_code == 404
    assert stolen.value.code == missing.value.code == "session_not_found"
    assert stolen.value.message == missing.value.message


async def test_session_past_retention_is_unreachable_before_cleanup(
    db_session: AsyncSession,
) -> None:
    repo = ChatRepository(db_session, retention_hours=12)
    owner = await make_user(db_session)
    chat = await repo.create_session(owner)
    later = datetime.now(UTC) + timedelta(hours=13)

    with pytest.raises(AppError) as excinfo:
        await repo.get_owned_session(chat.id, owner, now=later)

    assert excinfo.value.code == "session_not_found"


async def test_messages_update_activity_and_list_in_order(db_session: AsyncSession) -> None:
    repo = ChatRepository(db_session, retention_hours=12)
    owner = await make_user(db_session)
    chat = await repo.create_session(owner)
    await db_session.execute(
        text("UPDATE chat_sessions SET last_activity_at = now() - interval '5 hours'")
    )

    await repo.add_message(chat, MessageRole.USER, "What are GOLD sales?")
    await repo.add_message(
        chat,
        MessageRole.ASSISTANT,
        "GOLD sales are 1.2M.",
        resolved_context={"metric": "[Net Sales]", "filters": {"LOB": "GOLD"}},
    )

    await db_session.refresh(chat)
    assert datetime.now(UTC) - chat.last_activity_at < timedelta(minutes=5)
    messages = await repo.list_messages(chat)
    assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert messages[1].resolved_context == {"metric": "[Net Sales]", "filters": {"LOB": "GOLD"}}


# --- query executions: no raw result rows (Q9c) -------------------------------------------------


async def test_query_execution_stores_metadata_but_never_rows(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    chat = await ChatRepository(db_session, retention_hours=12).create_session(owner)

    execution = await QueryExecutionRepository(db_session).record(
        chat,
        gateway="fabric_iq_mcp",
        dax='EVALUATE ROW("Sales", [Net Sales])',
        status=QueryStatus.SUCCEEDED,
        row_count=1,
        column_names=["[Sales]"],
        duration_ms=420,
    )

    assert execution.row_count == 1
    columns = {c.key for c in inspect(QueryExecution).columns}
    assert not columns & {"rows", "result", "results", "data", "values"}


# --- audit --------------------------------------------------------------------------------------


async def test_audit_event_is_recorded(db_session: AsyncSession) -> None:
    await AuditRepository(db_session).record(
        "authz.decision",
        AuditOutcome.DENY,
        user=identity(),
        pbi_dataset_id="finance-dataset",
        correlation_id="c-1",
        reason="model_not_allowed",
        details={"denied_model_ids": ["finance-dataset"]},
    )

    event = (await db_session.scalars(select(AuditEvent))).one()
    assert (event.entra_object_id, event.outcome) == ("user-a", AuditOutcome.DENY)


async def test_audit_rejects_free_form_details(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="question"):
        await AuditRepository(db_session).record(
            "authz.decision", AuditOutcome.DENY, details={"question": "What is CEO salary?"}
        )


# --- registry and authorization cache -----------------------------------------------------------


async def test_registry_upserts_and_lists_only_enabled_active_models(
    db_session: AsyncSession,
) -> None:
    repo = RegistryRepository(db_session)
    ws = await repo.upsert_workspace(pbi_workspace_id="ws-1", entra_tenant_id="t", name="X")
    await repo.upsert_semantic_model(
        pbi_dataset_id="sales", workspace=ws, name="Sales", domain="Sales", chatbot_enabled=True
    )
    await repo.upsert_semantic_model(pbi_dataset_id="hr", workspace=ws, name="HR")
    renamed = await repo.upsert_semantic_model(
        pbi_dataset_id="sales", workspace=ws, name="Sales v2", chatbot_enabled=True
    )

    models = await repo.list_chatbot_models()

    assert [m.pbi_dataset_id for m in models] == ["sales"]
    assert renamed.name == "Sales v2"


async def test_access_cache_upsert_and_lookup(db_session: AsyncSession) -> None:
    repo = RegistryRepository(db_session)
    user = await make_user(db_session)
    ws = await repo.upsert_workspace(pbi_workspace_id="ws-1", entra_tenant_id="t", name="X")
    model = await repo.upsert_semantic_model(pbi_dataset_id="sales", workspace=ws, name="Sales")
    now = datetime.now(UTC)

    for status in (AccessStatus.DENIED, AccessStatus.ALLOWED):
        await repo.put_access(
            user_id=user.id,
            semantic_model_id=model.id,
            status=status,
            source="fabric_iq_mcp",
            verified_at=now,
            expires_at=now + timedelta(minutes=10),
        )

    access = await repo.get_access(user.id, [model.id])
    await db_session.refresh(access[model.id])
    assert access[model.id].status is AccessStatus.ALLOWED


# --- database-level guarantees ------------------------------------------------------------------


async def test_database_rejects_unknown_enum_values(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_audit_events_audit_outcome"):
        await db_session.execute(
            text("INSERT INTO audit_events (event_type, outcome) VALUES ('x', 'maybe')")
        )
