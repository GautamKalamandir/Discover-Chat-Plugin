import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatSession, QueryExecution, QueryStatus


class QueryExecutionRepository:
    """Records Power BI query executions.

    By design there is no parameter for result rows: only DAX and result metadata are stored
    (ADR 0004). Callers pass `row_count` / `column_names` derived from the result.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        chat: ChatSession,
        *,
        gateway: str,
        dax: str,
        status: QueryStatus,
        semantic_model_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        row_count: int | None = None,
        column_names: Sequence[str] | None = None,
        truncated: bool = False,
        duration_ms: int | None = None,
        error_code: str | None = None,
    ) -> QueryExecution:
        execution = QueryExecution(
            session_id=chat.id,
            message_id=message_id,
            semantic_model_id=semantic_model_id,
            gateway=gateway,
            dax=dax,
            status=status,
            row_count=row_count,
            column_names=list(column_names) if column_names is not None else None,
            truncated=truncated,
            duration_ms=duration_ms,
            error_code=error_code,
        )
        self._session.add(execution)
        await self._session.flush()
        return execution
