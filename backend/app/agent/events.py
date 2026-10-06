"""Events produced by one agent turn. Phase 9 maps them 1:1 onto Server-Sent Events."""

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StatusEvent:
    stage: str  # understanding | finding_data | planning | querying | analyzing | answering
    type: str = "status"


@dataclass(frozen=True)
class TokenEvent:
    text: str
    type: str = "token"


@dataclass(frozen=True)
class TableEvent:
    model_id: str
    title: str
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool
    type: str = "table"


@dataclass(frozen=True)
class ClarificationEvent:
    question: str
    type: str = "clarification"


@dataclass(frozen=True)
class ErrorEvent:
    code: str
    message: str  # always user-safe (AppError.message)
    type: str = "error"


@dataclass(frozen=True)
class DoneEvent:
    message_id: uuid.UUID | None
    query_ids: list[uuid.UUID] = field(default_factory=list)
    type: str = "done"


AgentEvent = StatusEvent | TokenEvent | TableEvent | ClarificationEvent | ErrorEvent | DoneEvent
