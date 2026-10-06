"""Stages 7-10: build/validate DAX, run it as the user, repair on DAX errors, record executions."""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.dax_builder import build_dax
from app.agent.dax_validator import DaxValidationError, validate_dax
from app.agent.models import PlanStep
from app.agent.planner import Planner
from app.authz.models import AuthorizedContext
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.db.models import ChatSession, QueryStatus
from app.db.repositories.queries import QueryExecutionRepository
from app.powerbi.base import QueryResult
from app.powerbi.errors import DaxQueryError
from app.powerbi.service import PowerBIService
from app.semantic.normalizer import NormalizedSchema

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StepOutcome:
    step: PlanStep
    dax: str
    result: QueryResult
    query_id: uuid.UUID


class Executor:
    def __init__(
        self,
        powerbi: PowerBIService,
        planner: Planner,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._powerbi = powerbi
        self._planner = planner
        self._sessionmaker = sessionmaker
        self._max_repairs = settings.agent_max_repairs
        self._fiscal_start = settings.fiscal_year_start_month
        self._today = today

    async def run_step(
        self,
        authz: AuthorizedContext,
        chat: ChatSession,
        step: PlanStep,
        schema: NormalizedSchema,
        context: str,
        *,
        max_rows: int,
    ) -> StepOutcome:
        model_uuid = authz.allowed[step.model_id].model_uuid
        dax = step.custom_dax or build_dax(
            step, today=self._today(), fiscal_start_month=self._fiscal_start
        )
        for attempt in range(self._max_repairs + 1):
            try:
                validate_dax(dax, schema)
            except DaxValidationError as exc:
                await self._record(chat, model_uuid, dax, QueryStatus.REJECTED, error="rejected")
                if attempt == self._max_repairs:
                    raise
                dax = await self._planner.repair_dax(dax, exc.reason, context)
                continue
            try:
                result = await self._powerbi.execute_query(
                    authz, model_id=step.model_id, dax=dax, max_rows=max_rows
                )
            except DaxQueryError as exc:
                await self._record(chat, model_uuid, dax, QueryStatus.FAILED, error="dax_error")
                if attempt == self._max_repairs:
                    raise
                logger.info("Repairing DAX for %s (attempt %d)", step.model_id, attempt + 1)
                dax = await self._planner.repair_dax(dax, exc.dax_error, context)
                continue
            except AppError as exc:
                status = (
                    QueryStatus.DENIED
                    if exc.code in (ErrorCode.MODEL_ACCESS_DENIED, ErrorCode.NEEDS_BUILD_PERMISSION)
                    else QueryStatus.FAILED
                )
                await self._record(chat, model_uuid, dax, status, error=str(exc.code))
                raise
            query_id = await self._record(
                chat,
                model_uuid,
                dax,
                QueryStatus.SUCCEEDED,
                gateway=result.gateway,
                result=result,
            )
            return StepOutcome(step, dax, result, query_id)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _record(
        self,
        chat: ChatSession,
        model_uuid: uuid.UUID,
        dax: str,
        status: QueryStatus,
        *,
        gateway: str = "-",
        error: str | None = None,
        result: QueryResult | None = None,
    ) -> uuid.UUID:
        async with self._sessionmaker() as session, session.begin():
            execution = await QueryExecutionRepository(session).record(
                chat,
                gateway=gateway,
                dax=dax,
                status=status,
                semantic_model_id=model_uuid,
                row_count=result.row_count if result else None,
                column_names=result.columns if result else None,
                truncated=result.truncated if result else False,
                duration_ms=result.duration_ms if result else None,
                error_code=error,
            )
            return execution.id
