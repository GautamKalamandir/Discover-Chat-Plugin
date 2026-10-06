"""Relational schema (plan §5.2).

Retention (ADR 0004): chat_sessions are deleted after CONVERSATION_RETENTION_HOURS of inactivity;
chat_messages and query_executions cascade with their session. audit_events have their own
AUDIT_RETENTION_HOURS and deliberately hold no foreign keys so they outlive sessions/models.
Raw query result rows are never stored.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    # VARCHAR + CHECK constraint: adding a value later is a simple migration.
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda e: [m.value for m in e],
        length=32,
    )


class ModelStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    SYNC_ERROR = "sync_error"


class AccessStatus(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    UNKNOWN = "unknown"
    STALE = "stale"


class MetadataObjectType(StrEnum):
    TABLE = "table"
    COLUMN = "column"
    MEASURE = "measure"
    RELATIONSHIP = "relationship"
    HIERARCHY = "hierarchy"


class GlossaryEntryType(StrEnum):
    SYNONYM = "synonym"
    KPI_DEFINITION = "kpi_definition"
    BUSINESS_RULE = "business_rule"
    EXAMPLE_QUESTION = "example_question"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class QueryStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    REJECTED = "rejected"  # failed validation before reaching Power BI


class AuditOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    SUCCESS = "success"
    FAILURE = "failure"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --- Identity ---------------------------------------------------------------------------------


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    entra_tenant_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str | None] = mapped_column(String(255))
    is_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("entra_tenant_id", "entra_object_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    entra_tenant_id: Mapped[str] = mapped_column(String(64))
    entra_object_id: Mapped[str] = mapped_column(String(64))
    username: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(255))
    first_seen_at: Mapped[datetime] = _created_at()
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# --- Power BI registry ------------------------------------------------------------------------


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = _uuid_pk()
    pbi_workspace_id: Mapped[str] = mapped_column(String(64), unique=True)
    entra_tenant_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class SemanticModel(Base):
    __tablename__ = "semantic_models"

    id: Mapped[uuid.UUID] = _uuid_pk()
    pbi_dataset_id: Mapped[str] = mapped_column(String(64), unique=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    # Business domain (Sales, HR, Finance, ...) — used by the cross-model router (ADR 0003).
    domain: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ModelStatus] = mapped_column(
        _enum(ModelStatus, "model_status"), server_default=ModelStatus.ACTIVE.value
    )
    chatbot_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    schema_version: Mapped[str | None] = mapped_column(String(128))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    workspace: Mapped[Workspace] = relationship()


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    pbi_report_id: Mapped[str] = mapped_column(String(64), unique=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True
    )
    semantic_model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("semantic_models.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class UserModelAccess(Base):
    """Authorization *cache* — never the source of truth (Power BI is). Phase 5 owns the policy."""

    __tablename__ = "user_model_access"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    semantic_model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("semantic_models.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    status: Mapped[AccessStatus] = mapped_column(_enum(AccessStatus, "access_status"))
    capability: Mapped[str | None] = mapped_column(String(32))  # read | build | query
    source: Mapped[str] = mapped_column(String(32))  # e.g. fabric_iq_mcp | rest
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


# --- Semantic knowledge (non-data metadata; vectors arrive in Phase 7) ------------------------


class ModelMetadata(Base):
    __tablename__ = "model_metadata"
    __table_args__ = (
        UniqueConstraint(
            "semantic_model_id",
            "schema_version",
            "object_type",
            "table_name",
            "object_name",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    semantic_model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("semantic_models.id", ondelete="CASCADE"), index=True
    )
    schema_version: Mapped[str] = mapped_column(String(128))
    object_type: Mapped[MetadataObjectType] = mapped_column(
        _enum(MetadataObjectType, "metadata_object_type")
    )
    table_name: Mapped[str | None] = mapped_column(String(255))
    object_name: Mapped[str] = mapped_column(String(255))
    data_type: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    expression: Mapped[str | None] = mapped_column(Text)  # measure DAX definition
    is_hidden: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class BusinessGlossaryEntry(Base):
    __tablename__ = "business_glossary"

    id: Mapped[uuid.UUID] = _uuid_pk()
    semantic_model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("semantic_models.id", ondelete="CASCADE"), index=True
    )
    entry_type: Mapped[GlossaryEntryType] = mapped_column(
        _enum(GlossaryEntryType, "glossary_entry_type")
    )
    term: Mapped[str] = mapped_column(String(255))
    definition: Mapped[str] = mapped_column(Text)
    maps_to: Mapped[str | None] = mapped_column(String(512))  # e.g. "[Total Net Sales]"
    created_by: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# --- Conversations (CONVERSATION_RETENTION_HOURS) ---------------------------------------------


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    primary_semantic_model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("semantic_models.id", ondelete="SET NULL")
    )
    report_hint: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created_at()
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_session_seq", "session_id", "seq"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Insertion order. created_at alone can't order messages written in one transaction
    # (Postgres now() is fixed per transaction), e.g. a question and its answer.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE")
    )
    role: Mapped[MessageRole] = mapped_column(_enum(MessageRole, "message_role"))
    content: Mapped[str] = mapped_column(Text)
    # Metric/filters/dates/models resolved for this turn; lets follow-ups reuse context.
    resolved_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class QueryExecution(Base):
    """One Power BI query. Stores DAX and result *metadata* only — never result rows (ADR 0004)."""

    __tablename__ = "query_executions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL")
    )
    semantic_model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("semantic_models.id", ondelete="SET NULL")
    )
    gateway: Mapped[str] = mapped_column(String(32))
    dax: Mapped[str] = mapped_column(Text)
    status: Mapped[QueryStatus] = mapped_column(_enum(QueryStatus, "query_status"))
    row_count: Mapped[int | None] = mapped_column(Integer)
    column_names: Mapped[list[str] | None] = mapped_column(JSONB)
    truncated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()


# --- Security audit (AUDIT_RETENTION_HOURS) ---------------------------------------------------


class AuditEvent(Base):
    """Who / what / when / decision. No foreign keys and no question text or data values."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    outcome: Mapped[AuditOutcome] = mapped_column(_enum(AuditOutcome, "audit_outcome"))
    entra_tenant_id: Mapped[str | None] = mapped_column(String(64))
    entra_object_id: Mapped[str | None] = mapped_column(String(64), index=True)
    pbi_dataset_id: Mapped[str | None] = mapped_column(String(64))
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
