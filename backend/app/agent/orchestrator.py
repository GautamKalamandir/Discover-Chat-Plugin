"""The agent: one question in, a stream of AgentEvents out (ADR 0008, fixed pipeline, Q13).

understanding -> finding_data -> planning -> querying -> analyzing -> answering -> done

Every outcome is persisted to the conversation. User-facing text is always either a grounded
answer, one clarification question, or a user-safe error/denial message.
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.analyzer import analyze
from app.agent.answer import AnswerWriter
from app.agent.context_builder import ContextBuilder
from app.agent.events import (
    AgentEvent,
    ClarificationEvent,
    DoneEvent,
    ErrorEvent,
    StatusEvent,
    TableEvent,
    TokenEvent,
)
from app.agent.executor import Executor, StepOutcome
from app.agent.models import QueryPlan
from app.agent.plan_validator import PlanInvalidError, validate_plan
from app.agent.planner import Planner
from app.agent.values import resolve_values
from app.authz.messages import GENERIC_DENIAL
from app.authz.models import AuthorizedContext
from app.authz.service import AuthorizationService
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.db.models import ChatSession, MessageRole
from app.db.repositories.chat import ChatRepository
from app.llm.base import ChatMessage, LLMProvider
from app.powerbi.service import PowerBIService
from app.semantic.normalizer import NormalizedSchema
from app.semantic.retriever import SemanticRetriever
from app.semantic.user_schema import UserSchemaService

logger = logging.getLogger(__name__)

# Shown as a normal answer, not an error banner (Q16: one generic message).
_GENERIC_ANSWER_CODES = {ErrorCode.MODEL_ACCESS_DENIED, ErrorCode.CANNOT_ANSWER}
_CHUNK = 40


class Agent:
    def __init__(
        self,
        llm: LLMProvider,
        retriever: SemanticRetriever,
        user_schemas: UserSchemaService,
        powerbi: PowerBIService,
        authz_service: AuthorizationService,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._authz_service = authz_service
        self._powerbi = powerbi
        self._today = today
        self._context = ContextBuilder(retriever, user_schemas, settings)
        self._planner = Planner(
            llm,
            fiscal_start_month=settings.fiscal_year_start_month,
            max_steps=settings.agent_max_query_steps,
        )
        self._executor = Executor(powerbi, self._planner, sessionmaker, settings, today=today)
        self._writer = AnswerWriter(llm, rows_to_llm=settings.agent_result_rows_to_llm)

    async def run(
        self,
        authz: AuthorizedContext,
        chat: ChatSession,
        question: str,
        *,
        primary_model_id: str | None = None,
        report_filters: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        started = time.perf_counter()
        query_ids: list[uuid.UUID] = []
        try:
            question = self._check_question(question)
            yield StatusEvent("understanding")
            previous = await self._previous_turn(chat)
            await self._persist(chat, MessageRole.USER, question)

            yield StatusEvent("finding_data")
            candidates = await self._context.candidates(authz, question, primary_model_id)
            context = await self._context.build(authz, question, candidates)
            if not context.schemas:
                raise _cannot_answer("no model context available for this user")

            yield StatusEvent("planning")
            today = self._today()
            messages = self._planner.messages(
                question,
                context.text,
                today=today,
                previous=previous,
                report_filters=report_filters,
            )
            plan = await self._planner.plan(messages)
            plan, authz = await self._checked_plan(plan, messages, authz, context.schemas, chat)

            if plan.status == "clarify" and plan.clarification:
                message_id = await self._persist(
                    chat,
                    MessageRole.ASSISTANT,
                    plan.clarification,
                    {"status": "clarify", "question": question},
                )
                yield ClarificationEvent(plan.clarification)
                yield DoneEvent(message_id)
                return
            if plan.status != "ready" or plan.unresolved_terms:
                # Scenario 6/8: never a partial answer when part of the question isn't covered.
                raise _cannot_answer(f"unresolved terms: {len(plan.unresolved_terms)}")

            plan = await resolve_values(plan, authz, self._powerbi)

            yield StatusEvent("querying")
            outcomes: list[StepOutcome] = []
            for step in plan.steps:
                outcome = await self._executor.run_step(
                    authz,
                    chat,
                    step,
                    context.schemas[step.model_id],
                    context.text,
                    max_rows=self._settings.powerbi_max_rows_limit,
                )
                outcomes.append(outcome)
                query_ids.append(outcome.query_id)
                rows = outcome.result.rows[: self._settings.agent_table_rows]
                yield TableEvent(
                    model_id=step.model_id,
                    title=step.label,
                    columns=outcome.result.columns,
                    rows=rows,
                    truncated=outcome.result.truncated or len(rows) < outcome.result.row_count,
                )

            yield StatusEvent("analyzing")
            facts = analyze(
                plan,
                outcomes,
                {d: m.name for d, m in authz.allowed.items()},
                today=today,
                fiscal_start_month=self._settings.fiscal_year_start_month,
            )

            yield StatusEvent("answering")
            answer = await self._writer.write(question, facts, outcomes)
            for chunk in _chunks(answer):
                yield TokenEvent(chunk)
            message_id = await self._persist(
                chat,
                MessageRole.ASSISTANT,
                answer,
                {
                    "question": question,
                    "plan": plan.model_dump(mode="json"),
                    "query_ids": [str(q) for q in query_ids],
                },
            )
            yield DoneEvent(message_id, query_ids)
        except AppError as exc:
            logger.info("Agent turn ended with %s: %s", exc.code, exc.log_detail or exc.message)
            if exc.code in _GENERIC_ANSWER_CODES:
                for chunk in _chunks(GENERIC_DENIAL):
                    yield TokenEvent(chunk)
                message_id = await self._persist(chat, MessageRole.ASSISTANT, GENERIC_DENIAL)
                yield DoneEvent(message_id, query_ids)
            else:
                yield ErrorEvent(str(exc.code), exc.message)
                message_id = await self._persist(chat, MessageRole.ASSISTANT, exc.message)
                yield DoneEvent(message_id, query_ids)
        except Exception:
            logger.exception("Agent turn failed unexpectedly")
            yield ErrorEvent(
                str(ErrorCode.INTERNAL_ERROR), "Something went wrong. Please try again."
            )
            yield DoneEvent(None, query_ids)
        finally:
            logger.info("Agent turn finished in %.0f ms", (time.perf_counter() - started) * 1000)

    # --- helpers --------------------------------------------------------------------------------

    def _check_question(self, question: str) -> str:
        question = question.strip()
        if not question:
            raise AppError(ErrorCode.QUESTION_INVALID, "Please type a question.", 422)
        limit = self._settings.agent_max_question_chars
        if len(question) > limit:
            raise AppError(
                ErrorCode.QUESTION_INVALID, f"Please keep questions under {limit} characters.", 422
            )
        return question

    async def _checked_plan(
        self,
        plan: QueryPlan,
        messages: list[ChatMessage],
        authz: AuthorizedContext,
        schemas: dict[str, NormalizedSchema],
        chat: ChatSession,
    ) -> tuple[QueryPlan, AuthorizedContext]:
        """Validates a ready plan; one re-plan with feedback if it uses unknown objects/models.

        A plan naming a model the user may not access raises the generic denial immediately
        (assert_allowed) and is never re-planned.
        """
        for attempt in range(2):
            if plan.status != "ready" or plan.unresolved_terms:
                return plan, authz
            try:
                validated = await validate_plan(
                    plan,
                    authz,
                    self._authz_service,
                    schemas,
                    max_steps=self._settings.agent_max_query_steps,
                    session_id=chat.id,
                )
            except PlanInvalidError as exc:
                if attempt == 1:
                    raise _cannot_answer(
                        f"plan still invalid: {len(exc.problems)} problems"
                    ) from exc
                logger.info("Re-planning after invalid plan (%d problems)", len(exc.problems))
                plan = await self._planner.replan(messages, plan, exc.problems)
                continue
            return validated.plan, validated.authz
        raise AssertionError("unreachable")  # pragma: no cover

    async def _previous_turn(self, chat: ChatSession) -> dict[str, Any] | None:
        """The most recent answered plan in this conversation (for follow-up questions)."""
        async with self._sessionmaker() as session:
            repo = ChatRepository(
                session, retention_hours=self._settings.conversation_retention_hours
            )
            messages = await repo.list_messages(chat, limit=self._settings.agent_history_turns * 2)
        for message in reversed(messages):
            context = message.resolved_context or {}
            if message.role is MessageRole.ASSISTANT and "plan" in context:
                return {"question": context.get("question"), "plan": context["plan"]}
        return None

    async def _persist(
        self,
        chat: ChatSession,
        role: MessageRole,
        content: str,
        resolved_context: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        async with self._sessionmaker() as session, session.begin():
            repo = ChatRepository(
                session, retention_hours=self._settings.conversation_retention_hours
            )
            message = await repo.add_message(chat, role, content, resolved_context=resolved_context)
            return message.id


def _cannot_answer(detail: str) -> AppError:
    return AppError(ErrorCode.CANNOT_ANSWER, GENERIC_DENIAL, 200, log_detail=detail)


def _chunks(text: str) -> list[str]:
    return [text[i : i + _CHUNK] for i in range(0, len(text), _CHUNK)] or [""]
