"""The investigation loop for one table of one model (ADR 0016 §10-§12, design §5).

seed step (server) -> loop { agent: next step | done } -> check -> DAX -> run as the user -> facts.

The agent decides what to look at next and when it has seen enough; the server only enforces safety:
- a query budget per table (`SALES_REPORT_QUERIES_PER_TABLE`, the seed included);
- every step is checked against the user's visible schema of a data source this table may use;
- an invalid step gets one re-ask with the problems, a second one uses a query from the budget;
- the same query is never run twice;
- server-built DAX is never sent to the LLM repair loop; a failing query is recorded as failed;
- lost access to a model stops at once (`MODEL_ACCESS_DENIED` is raised).
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, get_args

from pydantic import BaseModel, ConfigDict, Field

from app.agent.executor import Executor
from app.agent.models import PlanStep
from app.agent.planner import untrusted
from app.authz.models import AuthorizedContext
from app.core.errors import AppError, ErrorCode
from app.llm.base import ChatMessage, LLMProvider
from app.llm.budget import call_limits
from app.powerbi.base import QueryResult
from app.reports.models import SuggestionText
from app.reports.sales.dax import compile_step
from app.reports.sales.discovery import ModelRoles, model_block
from app.reports.sales.lenses import KEYS, TITLES, Lens
from app.reports.sales.periods import PeriodKey, SalesPeriods
from app.reports.sales.results import (
    EARLIER_ROWS_SHOWN,
    ROWS_SHOWN,
    Fact,
    StepResult,
    compact,
    parse_result,
)
from app.reports.sales.steps import MAX_FIGURES, LensStep, check_lens_step
from app.semantic.normalizer import NormalizedSchema

logger = logging.getLogger(__name__)

# Answer room per turn: a query, or the finish with findings and action points.
LOOP_OUTPUT_TOKENS = 1500


class LoopTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["query", "done"]
    step: LensStep | None = Field(default=None, description="The next query, when action is query")
    note: str = Field(default="", max_length=300, description="When done: what was found")
    # When done: the table's findings and action points, written from STATE (saves a call).
    findings: str = Field(default="", max_length=1500)
    suggestions: list[SuggestionText] = Field(default_factory=list, max_length=12)


class Runner(Protocol):
    async def __call__(
        self, model_id: str, label: str, dax: str, *, max_rows: int
    ) -> QueryResult: ...


@dataclass
class LoopResult:
    lens: Lens
    model_id: str
    steps: list[StepResult] = field(default_factory=list)
    queries_used: int = 0
    cut_short: bool = False  # the budget ran out (or the agent stopped answering) before "done"
    note: str = ""
    # What the agent wrote when it finished (checked by the writer like any other text).
    findings: str = ""
    suggestions: list[SuggestionText] = field(default_factory=list)
    written: bool = False

    def facts(self) -> list[Fact]:
        return [f for s in self.steps for f in s.facts]


class InvestigationLoop:
    def __init__(
        self,
        llm: LLMProvider,
        run: Runner,
        *,
        system_prompt: str,
        goals: Mapping[Lens, str],
        budget: int,
        max_rows: int,
        max_suggestions: int = 6,
    ) -> None:
        self._llm = llm
        self._run = run
        self._goals = goals
        self._budget = budget
        self._max_rows = max_rows
        # The answer format is described in the prompt itself: a JSON schema would cost more
        # tokens than the whole description (ADR 0016 amendment 1). Pydantic still checks it.
        self._system = system_prompt.replace("{max_figures}", str(MAX_FIGURES)).replace(
            "{max_suggestions}", str(max_suggestions)
        )

    async def investigate(
        self,
        lens: Lens,
        primary: ModelRoles,
        periods: SalesPeriods,
        *,
        seed: LensStep | None,
        extra: Sequence[ModelRoles] = (),
    ) -> LoopResult:
        """`extra`: other data sources this table may query (e.g. a stock model for the stock check
        of a degrowing LOB). Every step's figures stay with the data source they came from."""
        sources = {r.model.dataset_id: r for r in (primary, *extra)}
        result = LoopResult(lens=lens, model_id=primary.model.dataset_id)
        seen: set[str] = set()
        if seed is not None:
            await self._step(result, seed, sources, periods, seen, seed=True)
            if not result.steps or result.steps[0].error:
                return result  # no base for the table: it fails (design §12), nothing to explore
        while result.queries_used < self._budget:
            turn = await self._turn(result, primary, sources, periods, seen)
            if turn is None:  # the agent is unavailable: keep what was found
                result.cut_short = True
                return result
            if turn.action == "done":
                result.note = turn.note.strip()
                if turn.findings or turn.suggestions:
                    result.findings, result.suggestions = turn.findings, turn.suggestions
                    result.written = True
                return result
            if turn.step is None:
                result.queries_used += 1  # counted, so an agent that never asks can't loop
                continue
            await self._step(result, turn.step, sources, periods, seen)
        result.cut_short = True
        logger.info("Report table %s for %s: budget used up", lens, primary.model.dataset_id)
        return result

    # --- the agent's turn ---------------------------------------------------------------------

    async def _turn(
        self,
        result: LoopResult,
        primary: ModelRoles,
        sources: dict[str, ModelRoles],
        periods: SalesPeriods,
        seen: set[str],
    ) -> LoopTurn | None:
        messages = self._messages(result, primary, sources, periods)
        try:
            with call_limits(LOOP_OUTPUT_TOKENS, "low"):
                turn = await self._llm.complete_json(messages, LoopTurn)
            if turn.action == "done" or turn.step is None:
                return turn
            problems = self._problems(turn.step, sources, seen)
            if not problems:
                return turn
            # One re-ask with the problems; a second invalid step uses a query from the budget.
            with call_limits(LOOP_OUTPUT_TOKENS, "low"):
                again = await self._llm.complete_json(
                    [
                        *messages,
                        ChatMessage("assistant", turn.model_dump_json(exclude_defaults=True)),
                        ChatMessage("user", self._feedback(problems)),
                    ],
                    LoopTurn,
                )
            if again.action == "done" or again.step is None:
                return again
            if self._problems(again.step, sources, seen):
                logger.info("Report table %s: an invalid step used a query", result.lens)
                return LoopTurn(action="query", step=None)
            return again
        except AppError as exc:
            # Codes only: LLM messages can quote report content (Q18).
            logger.warning("Report table %s: the agent stopped (%s)", result.lens, exc.code)
            return None

    def _messages(
        self,
        result: LoopResult,
        primary: ModelRoles,
        sources: dict[str, ModelRoles],
        periods: SalesPeriods,
    ) -> list[ChatMessage]:
        # What stays the same for the whole table comes first (system, then the data sources),
        # so the provider can reuse it from cache; only STATE changes from turn to turn.
        context = {
            "table": TITLES[result.lens],
            "data_source": primary.model.dataset_id,
            "latest_day_with_data": f"{periods.day:%d %b %Y}",
            "periods": {key: periods.label(key) for key in get_args(PeriodKey)},
            "fields_found": {
                roles.model.dataset_id: {
                    "date_column": roles.date_column,
                    **roles.columns,
                    **roles.measures,
                    "lower_is_better": sorted(roles.lower_is_better),
                }
                for roles in sources.values()
            },
        }
        blocks = "\n\n".join(model_block(r.model, r.schema, r.tables) for r in sources.values())
        state = {
            "queries_left": self._budget - result.queries_used,
            # The latest query in full, the earlier ones by their largest moves only.
            "queries_so_far": [
                compact(s, ROWS_SHOWN if n == len(result.steps) else EARLIER_ROWS_SHOWN)
                for n, s in enumerate(result.steps, start=1)
            ],
        }
        system = self._system.replace("{lens_goal}", self._goals[result.lens])
        return [
            ChatMessage("system", system),
            ChatMessage(
                "user",
                "DATA SOURCES\n"
                + untrusted(
                    json.dumps(context, default=str, separators=(",", ":")) + "\n" + blocks
                ),
            ),
            ChatMessage(
                "user", "STATE\n" + untrusted(json.dumps(state, default=str, separators=(",", ":")))
            ),
        ]

    @staticmethod
    def _feedback(problems: list[str]) -> str:
        return (
            "That query cannot be run: "
            + "; ".join(problems)[:1500]
            + ". Fix it, ask a different query, or finish."
        )

    @staticmethod
    def _problems(step: LensStep, sources: dict[str, ModelRoles], seen: set[str]) -> list[str]:
        roles = sources.get(step.model_id)
        if roles is None:
            return [f"{step.model_id} is not a data source listed for this table"]
        checked, problems = check_lens_step(step, roles.schema, roles.date_column)
        if not problems and checked.key() in seen:
            problems.append("this exact query was already run")
        return problems

    # --- running a step -----------------------------------------------------------------------

    async def _step(
        self,
        result: LoopResult,
        step: LensStep,
        sources: dict[str, ModelRoles],
        periods: SalesPeriods,
        seen: set[str],
        *,
        seed: bool = False,
    ) -> None:
        number = len(result.steps) + 1
        roles = sources.get(step.model_id)
        problems = self._problems(step, sources, seen) if roles is not None else ["unknown"]
        if roles is None or roles.date_column is None or problems:
            # Only reached for a seed (agent steps are checked in `_turn`): a server bug, logged.
            logger.warning("Report table %s: step %d not run: %s", result.lens, number, problems)
            result.queries_used += 1
            return
        checked, _ = check_lens_step(step, roles.schema, roles.date_column)
        seen.add(checked.key())
        result.queries_used += 1
        dax, figures = compile_step(checked, roles.date_column, periods)
        label = (checked.why or f"{TITLES[result.lens]}: {'seed' if seed else 'query'}")[:120]
        record = StepResult(number=number, model_id=checked.model_id, why=checked.why, step=checked)
        try:
            outcome = await self._run(checked.model_id, label, dax, max_rows=self._max_rows)
        except AppError as exc:
            if exc.code is ErrorCode.MODEL_ACCESS_DENIED:
                raise
            logger.warning("Report table %s: step %d failed (%s)", result.lens, number, exc.code)
            result.steps.append(record.model_copy(update={"error": str(exc.code)}))
            return
        better = {
            f"m{index}": roles.higher_is_better(ref)
            for index, ref in enumerate(checked.measures, start=1)
        }
        facts = parse_result(
            KEYS[result.lens],
            number,
            checked,
            figures,
            outcome.columns,
            outcome.rows,
            higher_is_better=better,
        )
        unmatched = _unmatched(checked, outcome.rows) if checked.model_id != result.model_id else []
        result.steps.append(
            record.model_copy(
                update={
                    "facts": facts,
                    "rows": len(outcome.rows),
                    "truncated": outcome.truncated,
                    "unmatched": unmatched,
                }
            )
        )


def _unmatched(step: LensStep, rows: list[dict[str, Any]]) -> list[str]:
    """Names taken from one data source that another one doesn't have (e.g. an LOB or store of the
    sales data with no row in the stock data). Never matched loosely or guessed (design §5.3)."""
    missing: list[str] = []
    for flt in step.filters:
        if flt.op not in ("=", "in"):
            continue
        if flt.column in step.group_by:
            found = {str(r.get(flt.column)) for r in rows}
            missing += [str(v) for v in flt.values if str(v) not in found]
        elif not rows:
            missing += [str(v) for v in flt.values]
    return list(dict.fromkeys(missing))


def executor_runner(
    executor: Executor,
    authz: AuthorizedContext,
    report_id: uuid.UUID,
    schemas: dict[str, NormalizedSchema],
    limit: asyncio.Semaphore,
) -> Runner:
    """Runs the loop's queries as the user through the Executor (G3, read-only DAX validation,
    `query_executions` recorded against the report), at most `limit` at once across the build."""

    async def run(model_id: str, label: str, dax: str, *, max_rows: int) -> QueryResult:
        step = PlanStep(model_id=model_id, label=label[:120], custom_dax=dax)
        async with limit:
            outcome = await executor.run_step(
                authz,
                None,
                step,
                schemas[model_id],
                "",
                max_rows=max_rows,
                report_id=report_id,
                repair=False,
            )
        return outcome.result

    return run
