"""The agent: one question in, a stream of AgentEvents out (ADR 0008, fixed pipeline, Q13).

understanding -> finding_data -> planning -> querying -> analyzing -> answering -> done

Every outcome is persisted to the conversation. User-facing text is always either a grounded
answer, one clarification question, or a user-safe error/denial message.
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.analyzer import analyze
from app.agent.answer import AnswerWriter
from app.agent.change import analyze_change, build_steps, resolve_periods, with_change_columns
from app.agent.context_builder import ContextBuilder, focused
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
from app.agent.help import help_reply, quick_topic
from app.agent.models import PlanStep, QueryPlan
from app.agent.plan_validator import PlanInvalidError, validate_plan
from app.agent.planner import Planner
from app.agent.table_selector import TableSelector, needs_selection
from app.agent.values import resolve_values, value_hints
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
        self._user_schemas = user_schemas
        self._context = ContextBuilder(retriever, user_schemas, settings)
        self._planner = Planner(
            llm,
            fiscal_start_month=settings.fiscal_year_start_month,
            max_steps=settings.agent_max_query_steps,
        )
        self._executor = Executor(powerbi, self._planner, sessionmaker, settings, today=today)
        self._writer = AnswerWriter(llm, rows_to_llm=settings.agent_result_rows_to_llm)
        self._selector = TableSelector(llm, fiscal_start_month=settings.fiscal_year_start_month)

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

            # Small talk needs no data and no LLM ("hi", "thanks").
            if topic := quick_topic(question):
                async for event in self._help(chat, authz, topic, primary_model_id):
                    yield event
                return

            yield StatusEvent("finding_data")
            if not authz.allowed_models:
                async for event in self._help(chat, authz, "data_sources", primary_model_id):
                    yield event
                return
            candidates = await self._context.candidates(authz, question, primary_model_id)
            context = await self._context.build(authz, question, candidates)
            if not context.schemas:
                raise _cannot_answer("no model context available for this user")
            today = self._today()
            # Round trip 1 for large models: pick the tables, map the user's words to fields.
            if needs_selection(context.schemas, self._settings.agent_table_selection_min_columns):
                selection = await self._selector.select(
                    question, authz, context.schemas, today=today, previous=previous
                )
                if selection is not None:
                    context = focused(authz, context.schemas, selection)

            yield StatusEvent("planning")
            messages = self._planner.messages(
                question,
                context.text,
                today=today,
                previous=previous,
                report_filters=report_filters,
            )
            plan = await self._planner.plan(messages)
            if plan.status == "help":
                topic = plan.help_topic or "capabilities"
                async for event in self._help(chat, authz, topic, primary_model_id):
                    yield event
                return
            plan, authz = await self._checked_plan(plan, messages, authz, context.schemas, chat)
            if plan.status == "cannot_answer" and plan.unresolved_terms:
                # Maybe a misspelt value ("surar", "sivler"): look the words up in the data.
                hinted = await self._plan_with_value_hints(plan, messages, authz, context.schemas)
                if hinted is not None:
                    plan, authz = await self._checked_plan(
                        hinted, messages, authz, context.schemas, chat
                    )

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
                detail = f"unresolved terms: {len(plan.unresolved_terms)}"
                if message := not_found_message(plan.unresolved_terms, question):
                    raise AppError(ErrorCode.CANNOT_ANSWER, message, 200, log_detail=detail)
                raise _cannot_answer(detail)

            periods = None
            if plan.change is not None:
                resolved = resolve_periods(
                    plan.change, today, self._settings.fiscal_year_start_month
                )
                if isinstance(resolved, str):  # a period is still running: ask how to compare
                    message_id = await self._persist(
                        chat,
                        MessageRole.ASSISTANT,
                        resolved,
                        {
                            "status": "clarify",
                            "question": question,
                            "pending_plan": plan.model_dump(mode="json"),
                        },
                    )
                    yield ClarificationEvent(resolved)
                    yield DoneEvent(message_id)
                    return
                periods = resolved
                change = await self._resolved_change_filters(plan, authz)
                plan = plan.model_copy(update={"change": change})
                steps = build_steps(change, periods, max_steps=self._settings.agent_max_query_steps)
            else:
                plan = await resolve_values(plan, authz, self._powerbi)
                steps = plan.steps

            yield StatusEvent("querying")
            outcomes = await self._run_steps(authz, chat, steps, context)
            flat = flat_breakdowns(outcomes) if periods is None else []
            if flat:
                # The measure ignores the breakdown (same value on every row, e.g. a measure built
                # with ALL()). Never show that as a breakdown: re-plan once with the evidence.
                logger.info("Flat breakdown in %d step(s); re-planning once", len(flat))
                retry = await self._replan_flat(plan, messages, authz, context.schemas, chat, flat)
                if retry is not None:
                    new_plan, new_authz = retry
                    new_outcomes = await self._run_steps(new_authz, chat, new_plan.steps, context)
                    if len(flat_breakdowns(new_outcomes)) < len(flat):
                        plan, authz, outcomes = new_plan, new_authz, new_outcomes
                        flat = flat_breakdowns(outcomes)
            query_ids += [o.query_id for o in outcomes]
            for outcome in outcomes:
                step = outcome.step
                columns = outcome.result.columns
                rows = outcome.result.rows[: self._settings.agent_table_rows]
                if periods is not None:
                    columns, rows = with_change_columns(columns, rows, periods)
                yield TableEvent(
                    model_id=step.model_id,
                    title=step.label,
                    columns=columns,
                    rows=rows,
                    truncated=outcome.result.truncated or len(rows) < outcome.result.row_count,
                )

            yield StatusEvent("analyzing")
            model_names = {d: m.name for d, m in authz.allowed.items()}
            if plan.change is not None and periods is not None:
                facts = analyze_change(
                    plan.change,
                    periods,
                    outcomes,
                    model_names.get(plan.change.model_id, plan.change.model_id),
                )
            else:
                facts = analyze(
                    plan,
                    outcomes,
                    model_names,
                    today=today,
                    fiscal_start_month=self._settings.fiscal_year_start_month,
                )

            facts.notes += [_flat_note(f) for f in flat]

            yield StatusEvent("answering")
            asked = question
            if previous and previous.get("clarification_asked") and previous.get("question"):
                asked = f"{previous['question']} ({question})"  # reply to our clarification
            answer = await self._writer.write(asked, facts, outcomes)
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
                # A "not found" names only the user's own words; everything else is generic.
                text = exc.message if exc.code == ErrorCode.CANNOT_ANSWER else GENERIC_DENIAL
                for chunk in _chunks(text):
                    yield TokenEvent(chunk)
                message_id = await self._persist(chat, MessageRole.ASSISTANT, text)
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

    async def _run_steps(
        self,
        authz: AuthorizedContext,
        chat: ChatSession,
        steps: list[PlanStep],
        context: Any,
    ) -> list[StepOutcome]:
        return [
            await self._executor.run_step(
                authz,
                chat,
                step,
                context.schemas[step.model_id],
                context.text,
                max_rows=self._settings.powerbi_max_rows_limit,
            )
            for step in steps
        ]

    async def _replan_flat(
        self,
        plan: QueryPlan,
        messages: list[ChatMessage],
        authz: AuthorizedContext,
        schemas: dict[str, NormalizedSchema],
        chat: ChatSession,
        flat: "list[FlatBreakdown]",
    ) -> tuple[QueryPlan, AuthorizedContext] | None:
        problems = [
            f"measure {', '.join(f.values)} returned the same value for every {', '.join(f.groups)}"
            f" ({f.rows} rows), so it ignores that breakdown (it is probably fixed to all of "
            "them). Use a measure that changes per group, normally the plain base measure "
            "without qualifiers"
            for f in flat
        ]
        try:
            new_plan = await self._planner.replan(messages, plan, problems)
            new_plan, authz = await self._checked_plan(new_plan, messages, authz, schemas, chat)
        except AppError as exc:
            logger.info("Re-plan after flat breakdown failed: %s", exc.code)
            return None
        if new_plan.status != "ready" or new_plan.unresolved_terms or new_plan.change is not None:
            return None
        return await resolve_values(new_plan, authz, self._powerbi), authz

    async def _help(
        self,
        chat: ChatSession,
        authz: AuthorizedContext,
        topic: str,
        primary_model_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        """A conversational reply written by the server from the user's own models/schema."""
        models = authz.allowed_models
        schema = None
        if models and topic != "thanks":
            dataset_id = (
                primary_model_id
                if primary_model_id and authz.is_allowed(primary_model_id)
                else models[0].dataset_id
            )
            schema = await self._user_schemas.visible(authz, dataset_id)
        user = authz.request.user
        text = help_reply(
            topic,
            user_name=user.display_name or user.username,
            models=models,
            schema=schema,
            fiscal_start_month=self._settings.fiscal_year_start_month,
        )
        for chunk in _chunks(text):
            yield TokenEvent(chunk)
        message_id = await self._persist(
            chat, MessageRole.ASSISTANT, text, {"status": "help", "topic": topic}
        )
        yield DoneEvent(message_id)

    async def _plan_with_value_hints(
        self,
        plan: QueryPlan,
        messages: list[ChatMessage],
        authz: AuthorizedContext,
        schemas: dict[str, NormalizedSchema],
    ) -> QueryPlan | None:
        """Searches the unplaced words as values (Fabric IQ ValueSearch, as the user, only in the
        models already in the context) and re-plans once if any were found."""
        terms = [t.strip()[:100] for t in plan.unresolved_terms if t.strip()][:5]
        hints: list[str] = []
        for model_id in schemas:
            try:
                payload = await self._powerbi.search_values(authz, model_id=model_id, terms=terms)
            except AppError as exc:
                logger.info("Value search for unplaced words unavailable: %s", exc.code)
                continue
            hints += value_hints(payload.data)
        if not hints:
            return None
        logger.info("Re-planning with %d value matches for %d words", len(hints), len(terms))
        return await self._planner.replan_with_values(messages, plan, hints)

    async def _resolved_change_filters(self, plan: QueryPlan, authz: AuthorizedContext) -> Any:
        """The change analysis with filter values mapped to the stored spelling (ValueSearch)."""
        change = plan.change
        assert change is not None  # noqa: S101 - only called for change plans
        probe = QueryPlan(
            status="ready",
            steps=[PlanStep(model_id=change.model_id, label=change.label, filters=change.filters)],
        )
        resolved = await resolve_values(probe, authz, self._powerbi)
        return change.model_copy(update={"filters": resolved.steps[0].filters})

    async def _previous_turn(self, chat: ChatSession) -> dict[str, Any] | None:
        """For follow-ups: the most recent answered plan, or the clarification we just asked
        (then the new question is the user's reply to it)."""
        async with self._sessionmaker() as session:
            repo = ChatRepository(
                session, retention_hours=self._settings.conversation_retention_hours
            )
            messages = await repo.list_messages(chat, limit=self._settings.agent_history_turns * 2)
        for message in reversed(messages):
            context = message.resolved_context or {}
            if message.role is not MessageRole.ASSISTANT:
                continue
            if "plan" in context:
                return {"question": context.get("question"), "plan": context["plan"]}
            if context.get("status") == "clarify":
                previous: dict[str, Any] = {
                    "question": context.get("question"),
                    "clarification_asked": message.content,
                    "note": "The QUESTION is the user's reply to this clarification.",
                }
                if context.get("pending_plan"):
                    previous["pending_plan"] = context["pending_plan"]
                return previous
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


@dataclass(frozen=True)
class FlatBreakdown:
    step: str
    groups: list[str]
    values: list[str]
    rows: int


def flat_breakdowns(outcomes: list[StepOutcome]) -> list[FlatBreakdown]:
    """Grouped results (>= 3 rows) whose every value column holds one single value."""
    flat = []
    for outcome in outcomes:
        result = outcome.result
        values = [c for c in result.columns if c.startswith("[")]
        groups = [c for c in result.columns if not c.startswith("[")]
        if result.row_count < 3 or not values or not groups:
            continue
        if all(len({repr(row.get(c)) for row in result.rows}) == 1 for c in values):
            flat.append(FlatBreakdown(outcome.step.label, groups, values, result.row_count))
    return flat


def _flat_note(flat: FlatBreakdown) -> str:
    return (
        f"{', '.join(v.strip('[]') for v in flat.values)} has the same value for every "
        f"{', '.join(flat.groups)}: this measure doesn't break down that way, so the rows are "
        "not a per-item split."
    )


def not_found_message(terms: list[str], question: str) -> str | None:
    """Names the words that couldn't be found (decision 2026-10-08), but ONLY words that appear in
    the user's own question: the reply then depends on nothing but the user's words and their own
    data, so it can't reveal restricted models (Q16) or carry text invented by the LLM."""
    asked = question.lower()
    words: list[str] = []
    for term in terms:
        word = " ".join(term.split())[:60]
        if word and word.lower() in asked and word.lower() not in (w.lower() for w in words):
            words.append(word)
    if not words:
        return None
    quoted = [f'"{w}"' for w in words[:3]]
    listed = quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " and " + quoted[-1]
    return (
        f"I couldn't find {listed} in the data available to you. "
        "Check the spelling or try another word."
    )


def _cannot_answer(detail: str) -> AppError:
    return AppError(ErrorCode.CANNOT_ANSWER, GENERIC_DENIAL, 200, log_detail=detail)


def _chunks(text: str) -> list[str]:
    return [text[i : i + _CHUNK] for i in range(0, len(text), _CHUNK)] or [""]
