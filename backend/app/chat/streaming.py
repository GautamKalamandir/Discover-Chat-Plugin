"""AgentEvent -> Server-Sent Event (wire format in docs/design/phase-9-chat-api.md §3)."""

import dataclasses
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sse_starlette import ServerSentEvent

from app.agent.events import AgentEvent


def _json_default(value: Any) -> Any:
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return sorted(value)
    return str(value)


def encode(event_type: str, data: dict[str, Any], seq: int) -> ServerSentEvent:
    return ServerSentEvent(
        data=json.dumps(data, default=_json_default, ensure_ascii=False),
        event=event_type,
        id=str(seq),
    )


def agent_event(event: AgentEvent, seq: int) -> ServerSentEvent:
    data = {k: v for k, v in dataclasses.asdict(event).items() if k != "type"}
    return encode(event.type, data, seq)
