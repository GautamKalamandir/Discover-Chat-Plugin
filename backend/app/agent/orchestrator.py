"""The agent: one question in, a stream of AgentEvents out (ADR 0008, fixed pipeline, Q13).

understanding -> finding_data -> planning -> querying -> analyzing -> answering -> done

Every outcome is persisted to the conversation. User-facing text is always either a grounded
answer, one clarification question (optionally with options to click), or a user-safe
error/denial message.

ADR 0014: the agent decides when to ask and what to offer; the server checks every value against the
user's own data and every option like any plan, and a click runs exactly the option it validated.
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
from app.agent.change import (
    analyze_change,
    basis_options,
    build_steps,
    resolve_periods,
    with_change_columns,
)
from app.agent.context_builder import ContextBuilder, PlannerContext, focused
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
from app.agent.models import ClarificationChoice, PlanStep, QueryPlan
from app.agent.plan_validator import PlanInvalidError, validate_plan
from app.agent.planner import Planner
from app.agent.resolution import (
    Evidence,
    ValueEvidence,
    apply_stored_values,
    step_models,
    unknown_values,
)
from app.agent.table_selector import TableSelector, needs_selection
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
STALE_CHOICE = "That option is no longer available. Please ask your question again."
_NO_MORE_QUESTIONS = (
    "you may not ask the user another question for this request: plan the most likely reading "
    '(list it in "assumptions") or return "cannot_answer"'
)


@dataclass(frozen=True)
class _Chain:
    """The question a run of clarifications is about, and how many were asked so far."""

    question: str
    asked: int  # counted against AGENT_MAX_CLARIFICATIONS (running-period questions aren't)
    continues: bool = False  # this turn answers our clarification


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
            max_options=settings.agent_max_options,
        )
        self._executor = Executor(powerbi, self._planner, sessionmaker, settings, today=today)
        self._writer = AnswerWriter(llm, rows_to_llm=settings.agent_result_rows_to_llm)
        self._selector = TableSelector(llm, fiscal_start_month=settings.fiscal_year_start_month)
        self._values = ValueEvidence(powerbi, settings)

    async def run(
        self,
        authz: AuthorizedContext,
        chat: ChatSession,
        question: str,
        *,
        primary_model_id: str | None = None,
        report_filters: list[dict[str, Any]] | None = None,
        choice: ClarificationChoice | None = None,
    ) -> AsyncIterator[AgentEvent]:
        started = time.perf_counter()
        query_ids: list[uuid.UUID] = []
        try:
            question = self._check_question(question)
            yield StatusEvent("understanding")
            previous = await self._previous_turn(chat)
            await self._persist(chat, MessageRole.USER, question)

            if choice is not None:
                async for event in self._resume(authz, chat, question, choice, previous, query_ids):
                    yield event
                return

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
                    question, authz, context.schemas, today=today, previous=_for_planner(previous)
                )
                if selection is not None:
                    context = focused(authz, context.schemas, selection)

            yield StatusEvent("planning")
            messages = self._planner.messages(
                question,
                context.text,
                today=today,
                previous=_for_planner(previous),
                report_filters=report_filters,
            )
            plan = await self._planner.plan(messages)
            if plan.status == "help":
                topic = plan.help_topic or "capabilities"
                async for event in self._help(chat, authz, topic, primary_model_id):
                    yield event
                return
            chain = _chain(previous, plan, question)
            may_ask = chain.asked < self._settings.agent_max_clarifications

            plan, authz = await self._checked_plan(plan, messages, authz, context.schemas, chat)
            plan, authz, evidence = await self._resolve_values(
                plan, messages, authz, context.schemas, chat, may_ask=may_ask
            )

            if plan.status == "clarify" and plan.clarification and not may_ask:
                logger.info("Clarification limit reached; re-planning without asking")
                plan = await self._planner.replan(messages, plan, [_NO_MORE_QUESTIONS])
                plan, authz = await self._checked_plan(plan, messages, authz, context.schemas, chat)
                if plan.status == "clarify":
                    raise _cannot_answer("clarification limit reached")
                evidence = await self._values.gather(plan, authz, known=evidence)
                plan = apply_stored_values(plan, evidence)

            if plan.status == "clarify" and plan.clarification:
                options, problems, authz, evidence = await self._options(
                    plan, authz, context.schemas, chat, evidence
                )
                if plan.options and not options:
                    logger.info("No usable option (%d problems); re-planning once", len(problems))
                    plan = await self._planner.replan(messages, plan, problems[:10])
                    plan, authz = await self._checked_plan(
                        plan, messages, authz, context.schemas, chat
                    )
                    evidence = await self._values.gather(plan, authz, known=evidence)
                    if plan.status == "clarify" and plan.clarification:
                        options, _, authz, evidence = await self._options(
                            plan, authz, context.schemas, chat, evidence
                        )
                    else:
                        plan = apply_stored_values(plan, evidence)
                if plan.status == "clarify" and plan.clarification:
                    async for event in self._ask(
                        chat, plan.clarification, options, chain.question, chain.asked + 1
                    ):
                        yield event
                    return

            if plan.status != "ready" or plan.unresolved_terms:
                # Scenario 6/8: never a partial answer when part of the question isn't covered.
                detail = f"unresolved terms: {len(plan.unresolved_terms)}"
                if message := not_found_message(plan.unresolved_terms, question):
                    raise AppError(ErrorCode.CANNOT_ANSWER, message, 200, log_detail=detail)
                raise _cannot_answer(detail)

            asked = f"{chain.question} ({question})" if chain.continues else question
            async for event in self._execute_and_answer(
                authz, chat, plan, messages, context, today, asked, chain, query_ids
            ):
                yield event
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

    # --- stages ---------------------------------------------------------------------------------

    async def _execute_and_answer(
        self,
        authz: AuthorizedContext,
        chat: ChatSession,
        plan: QueryPlan,
        messages: list[ChatMessage],
        context: PlannerContext,
        today: date,
        asked: str,
        chain: _Chain,
        query_ids: list[uuid.UUID],
    ) -> AsyncIterator[AgentEvent]:
        """A validated, ready plan: periods, queries, analysis, the grounded answer."""
        periods = None
        if plan.change is not None:
            fiscal = self._settings.fiscal_year_start_month
            resolved = resolve_periods(plan.change, today, fiscal)
            if isinstance(resolved, str):  # a period is still running: ask how to compare
                options = [
                    (label, plan.model_copy(update={"change": change}))
                    for label, change in basis_options(plan.change, today, fiscal)
                ]
                pending = {"pending_plan": plan.model_dump(mode="json")}
                async for event in self._ask(
                    chat, resolved, options, chain.question, chain.asked, pending
                ):
                    yield event
                return
            periods = resolved
            steps = build_steps(
                plan.change, periods, max_steps=self._settings.agent_max_query_steps
            )
        else:
            steps = plan.steps

        yield StatusEvent("querying")
        outcomes = await self._run_steps(authz, chat, steps, context)
        flat = flat_breakdowns(outcomes) if periods is None else []
        if flat:
            # The measure ignores the breakdown (same value on every row, e.g. a measure built
            # with ALL()). Never show that as a breakdown: re-plan once with the evidence.
            logger.info("Flat breakdown in %d step(s); re-planning once", len(flat))
            retry = await self._replan_flat(plan, messages, authz, context, chat, flat)
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
        facts.assumptions = list(plan.assumptions)

        yield StatusEvent("answering")
        answer = await self._writer.write(asked, facts, outcomes)
        for chunk in _chunks(answer):
            yield TokenEvent(chunk)
        message_id = await self._persist(
            chat,
            MessageRole.ASSISTANT,
            answer,
            {
                "question": asked,
                "plan": plan.model_dump(mode="json"),
                "query_ids": [str(q) for q in query_ids],
            },
        )
        yield DoneEvent(message_id, query_ids)

    async def _resume(
        self,
        authz: AuthorizedContext,
        chat: ChatSession,
        question: str,
        choice: ClarificationChoice,
        previous: dict[str, Any] | None,
        query_ids: list[uuid.UUID],
    ) -> AsyncIterator[AgentEvent]:
        """A click on an option: runs exactly the plan validated when it was offered, after
        checking access and the user's schema again. No planning call."""
        option = _offered(previous, choice)
        if previous is None or option is None:
            logger.info("Choice for a clarification that is not open; refused")
            for chunk in _chunks(STALE_CHOICE):
                yield TokenEvent(chunk)
            message_id = await self._persist(chat, MessageRole.ASSISTANT, STALE_CHOICE)
            yield DoneEvent(message_id)
            return

        yield StatusEvent("finding_data")
        chain = _Chain(
            str(previous.get("question") or question), int(previous["clarifications"]), True
        )
        plan = QueryPlan.model_validate(option["plan"])
        context = await self._context.build(authz, chain.question, step_models(plan))
        try:
            validated = await validate_plan(
                plan,
                authz,
                self._authz_service,
                context.schemas,
                max_steps=self._settings.agent_max_query_steps,
                session_id=chat.id,
            )
        except PlanInvalidError as exc:
            raise _cannot_answer(f"chosen option no longer valid: {len(exc.problems)}") from exc
        today = self._today()
        async for event in self._execute_and_answer(
            validated.authz,
            chat,
            validated.plan,
            self._planner.messages(chain.question, context.text, today=today),
            context,
            today,
            f"{chain.question} ({question})",
            chain,
            query_ids,
        ):
            yield event

    async def _ask(
        self,
        chat: ChatSession,
        question: str,
        options: list[tuple[str, QueryPlan]],
        original: str,
        asked: int,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Persists the question with its validated options (server ids) and asks it."""
        clarification_id = uuid.uuid4()
        stored: list[dict[str, Any]] = [
            {"id": str(uuid.uuid4()), "label": label, "plan": plan.model_dump(mode="json")}
            for label, plan in options
        ]
        message_id = await self._persist(
            chat,
            MessageRole.ASSISTANT,
            question,
            {
                "status": "clarify",
                "question": original,
                "clarification_id": str(clarification_id),
                "options": stored,
                "clarifications": asked,
                **(extra or {}),
            },
        )
        logger.info("Asked a clarification with %d options", len(stored))
        yield ClarificationEvent(
            question,
            id=clarification_id,
            options=[{"id": o["id"], "label": o["label"]} for o in stored],
        )
        yield DoneEvent(message_id)

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

    async def _resolve_values(
        self,
        plan: QueryPlan,
        messages: list[ChatMessage],
        authz: AuthorizedContext,
        schemas: dict[str, NormalizedSchema],
        chat: ChatSession,
        *,
        may_ask: bool,
    ) -> tuple[QueryPlan, AuthorizedContext, Evidence]:
        """Looks the plan's value words up in the user's data. Stored values are used as stored;
        otherwise the agent sees the evidence once and decides (ADR 0014)."""
        if plan.status not in ("ready", "cannot_answer"):
            return plan, authz, Evidence()
        unresolved_models = list(schemas) if plan.unresolved_terms else []
        if plan.status == "cannot_answer" and not unresolved_models:
            return plan, authz, Evidence()
        evidence = await self._values.gather(plan, authz, unresolved_models=unresolved_models)
        if evidence.needs_decision():
            logger.info("Values not stored as written; the agent decides with the evidence")
            resolved = await self._planner.resolve(
                messages, plan, evidence.lines(), may_ask=may_ask
            )
            plan, authz = await self._checked_plan(resolved, messages, authz, schemas, chat)
            evidence = await self._values.gather(plan, authz, known=evidence)
        if plan.status == "ready":
            plan = apply_stored_values(plan, evidence)
        return plan, authz, evidence

    async def _options(
        self,
        plan: QueryPlan,
        authz: AuthorizedContext,
        schemas: dict[str, NormalizedSchema],
        chat: ChatSession,
        evidence: Evidence,
    ) -> tuple[list[tuple[str, QueryPlan]], list[str], AuthorizedContext, Evidence]:
        """The options worth offering: each validated like any plan (a forbidden model still
        denies the whole request) and using only values stored in the user's data."""
        valid: list[tuple[str, QueryPlan]] = []
        problems: list[str] = []
        for index, option in enumerate(plan.options[: self._settings.agent_max_options], 1):
            try:
                checked = await validate_plan(
                    QueryPlan.from_option(option),
                    authz,
                    self._authz_service,
                    schemas,
                    max_steps=self._settings.agent_max_query_steps,
                    session_id=chat.id,
                )
            except PlanInvalidError as exc:
                problems += [f"option {index}: {p}" for p in exc.problems]
                continue
            authz = checked.authz
            evidence = await self._values.gather(checked.plan, authz, known=evidence)
            if missing := unknown_values(checked.plan, evidence):
                problems += [f"option {index}: {p}" for p in missing]
                continue
            valid.append((option.label.strip(), apply_stored_values(checked.plan, evidence)))
        if len(valid) < len(plan.options):
            logger.info(
                "Dropped %d of %d options", len(plan.options) - len(valid), len(plan.options)
            )
        return valid, problems, authz, evidence

    async def _run_steps(
        self,
        authz: AuthorizedContext,
        chat: ChatSession,
        steps: list[PlanStep],
        context: PlannerContext,
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
        context: PlannerContext,
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
            new_plan, authz = await self._checked_plan(
                new_plan, messages, authz, context.schemas, chat
            )
        except AppError as exc:
            logger.info("Re-plan after flat breakdown failed: %s", exc.code)
            return None
        if new_plan.status != "ready" or new_plan.unresolved_terms or new_plan.change is not None:
            return None
        evidence = await self._values.gather(new_plan, authz)
        return apply_stored_values(new_plan, evidence), authz

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

    async def _previous_turn(self, chat: ChatSession) -> dict[str, Any] | None:
        """For follow-ups: the most recent answered plan, or the clarification we just asked
        (`open` only when it is the latest assistant message: the one its options belong to)."""
        async with self._sessionmaker() as session:
            repo = ChatRepository(
                session, retention_hours=self._settings.conversation_retention_hours
            )
            messages = await repo.list_messages(chat, limit=self._settings.agent_history_turns * 2)
        latest = True
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
                    "clarification_id": context.get("clarification_id"),
                    "options": context.get("options") or [],
                    "clarifications": int(context.get("clarifications", 1)),
                    "open": latest,
                }
                if context.get("pending_plan"):
                    previous["pending_plan"] = context["pending_plan"]
                return previous
            latest = False
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


def _for_planner(previous: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the planner sees of the previous turn: option labels, not the stored plans or ids."""
    if previous is None or "clarification_asked" not in previous:
        return previous
    shown: dict[str, Any] = {
        k: previous[k] for k in ("question", "clarification_asked") if k in previous
    }
    if labels := [o.get("label") for o in previous.get("options", [])]:
        shown["options_offered"] = labels
    if "pending_plan" in previous:
        shown["pending_plan"] = previous["pending_plan"]
    return shown


def _chain(previous: dict[str, Any] | None, plan: QueryPlan, question: str) -> _Chain:
    """A reply to our clarification continues its question chain; anything else starts anew."""
    if previous and "clarification_asked" in previous and plan.answers_previous_clarification:
        question = str(previous.get("question") or question)
        return _Chain(question, int(previous["clarifications"]), True)
    return _Chain(question, 0)


def _offered(previous: dict[str, Any] | None, choice: ClarificationChoice) -> dict[str, Any] | None:
    """The option the user clicked, only if it belongs to the open clarification of this chat."""
    if not previous or not previous.get("open"):
        return None
    if previous.get("clarification_id") != str(choice.clarification_id):
        return None
    return next(
        (o for o in previous.get("options", []) if o.get("id") == str(choice.option_id)), None
    )


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
