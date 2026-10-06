import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthenticatedUser
from app.db.models import AuditEvent, AuditOutcome

# Keys allowed in `details`. Audit rows must never carry question text, answers or data values
# (ADR 0004), so free-form details are restricted to this allow-list.
ALLOWED_DETAIL_KEYS = frozenset(
    {"tool", "gateway", "status_code", "error_code", "model_ids", "denied_model_ids", "count"}
)


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        event_type: str,
        outcome: AuditOutcome,
        *,
        user: AuthenticatedUser | None = None,
        pbi_dataset_id: str | None = None,
        session_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        if details and (unexpected := set(details) - ALLOWED_DETAIL_KEYS):
            raise ValueError(f"Audit details keys not allowed: {sorted(unexpected)}")
        event = AuditEvent(
            event_type=event_type,
            outcome=outcome,
            entra_tenant_id=user.tenant_id if user else None,
            entra_object_id=user.object_id if user else None,
            pbi_dataset_id=pbi_dataset_id,
            session_id=session_id,
            correlation_id=correlation_id,
            reason=reason[:255] if reason else None,
            details=details,
        )
        self._session.add(event)
        await self._session.flush()
        return event
