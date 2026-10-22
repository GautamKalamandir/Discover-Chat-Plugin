"""The /sales-summary investigation loop (ADR 0016 §10-§12, design §5): the agent picks each next
query and when to stop; the server checks every step, keeps to the budget, never repairs its own
DAX and records every query against the report.

Real database, authorization and Executor; dev_synthetic data; an LLM that plays scripted turns.
user-a may use Sales and HR (not Finance); user-b only Sales, with Customer[CreditLimit] hidden.
"""

import asyncio
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import date
from typing import Any

import pytest
from sqlalchemy import text

from app.agent.executor import Executor
from app.agent.planner import Planner
from app.auth.token_broker import UnavailableTokenBroker
from app.authz.models import AuthorizedContext
from app.core.errors import AppError, ErrorCode
from app.llm.base import ChatMessage, LLMProvider, LLMResponse
from app.llm.budget import current_limits
from app.powerbi.base import QueryResult
from app.powerbi.dev_synthetic import DevSyntheticGateway
from app.powerbi.errors import DaxQueryError
from app.powerbi.service import PowerBIService
from app.reports.sales.discovery import ModelRoles
from app.reports.sales.loop import (
    LOOP_OUTPUT_TOKENS,
    InvestigationLoop,
    LoopResult,
    Runner,
    executor_runner,
)
from app.reports.sales.periods import SalesPeriods
from app.reports.sales.prompts import lens_goals, load_sales_prompt
from app.reports.sales.steps import LensStep
from tests.authz_helpers import request_ctx
from tests.test_agent_pipeline import SETTINGS, Env, build_env
from tests.test_report_builder import TODAY, start_report

pytestmark = pytest.mark.integration

GOALS = lens_goals(SETTINGS)
PERIODS = SalesPeriods(date(2026, 10, 12), fiscal_start_month=4)
SEED = LensStep(
    model_id="sales-ds",
    measures=["Sales[Total Net Sales]"],
    group_by=["Product[LOB]"],
    periods=["mtd", "mtd_ly", "fytd", "fytd_ly"],
    why="Every LOB against last year",
)
DRILL = {
    "model_id": "sales-ds",
    "measures": ["Sales[Total Net Sales]"],
    "group_by": ["Product[Category]"],
    "filters": [{"column": "Product[LOB]", "op": "=", "values": ["DEV-A"]}],
    "periods": ["mtd", "mtd_ly"],
    "why": "Which categories DEV-A's fall came from",
}
Turn = dict[str, Any] | Callable[[Sequence[ChatMessage]], dict[str, Any]]


def query(**changes: Any) -> dict[str, Any]:
    return {"action": "query", "step": {**DRILL, **changes}}


DONE = {"action": "done", "note": "DEV-A fell most, mainly in one category."}


class LoopLLM(LLMProvider):
    """Plays scripted turns; any other prompt (e.g. a DAX repair) fails the test."""

    name = "scripted"
    model = "scripted"

    def __init__(self, *turns: Turn, fail: bool = False) -> None:
        self.turns = list(turns)
        self.fail = fail
        self.calls: list[list[ChatMessage]] = []

    async def complete(
        self, messages: Sequence[ChatMessage], *, json_mode: bool = False
    ) -> LLMResponse:
        assert "TASK: investigate one table" in messages[0].content, "unexpected LLM call"
        self.calls.append(list(messages))
        if self.fail:
            raise AppError(ErrorCode.LLM_UNAVAILABLE, "down", 503)
        turn = self.turns.pop(0)
        return LLMResponse(json.dumps(turn(messages) if callable(turn) else turn))

    def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:  # pragma: no cover
        raise NotImplementedError


def state(messages: Sequence[ChatMessage]) -> Any:
    message = next(m for m in messages if m.content.startswith("STATE"))
    block = message.content.split("<untrusted_data>\n", 1)[1].split("\n</untrusted_data>")[0]
    return json.loads(block)


class Gateway(DevSyntheticGateway):
    def __init__(self, *, fail_on: str | None = None, delay: float = 0.0) -> None:
        super().__init__(SETTINGS, today=lambda: TODAY)
        self.fail_on = fail_on
        self.delay = delay
        self.queries: list[str] = []
        self.datasets: list[str] = []  # the data source of each query
        self.running = self.most_at_once = 0

    async def execute_dax(
        self, user_token: str, dataset_id: str, dax: str, *, max_rows: int, user_key: str
    ) -> QueryResult:
        self.queries.append(dax)
        self.datasets.append(dataset_id)
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        try:
            await asyncio.sleep(self.delay)
            if self.fail_on and self.fail_on in dax:
                raise DaxQueryError("Column 'X' cannot be found")
            return await super().execute_dax(
                user_token, dataset_id, dax, max_rows=max_rows, user_key=user_key
            )
        finally:
            self.running -= 1


@pytest.fixture
async def env(committed_sessionmaker: Any) -> Env:
    return await build_env(committed_sessionmaker)


async def roles_for(env: Env, authz: AuthorizedContext, dataset_id: str = "sales-ds") -> ModelRoles:
    schema = await env.user_schemas.visible(authz, dataset_id)
    assert schema is not None
    sales = dataset_id == "sales-ds"
    return ModelRoles(
        model=authz.allowed[dataset_id],
        schema=schema,
        lenses=("lob_growth",) if sales else (),
        date_column="Sales[InvoiceDate]" if sales else None,
        columns={"lob": "Product[LOB]", "subcategory": "Product[Category]"} if sales else {},
        measures={"sales_value": "Sales[Total Net Sales]"} if sales else {},
        lower_is_better=frozenset(),
        reason="",
    )


async def investigate(
    env: Env,
    llm: LoopLLM,
    *,
    oid: str = "user-a",
    gateway: Gateway | None = None,
    budget: int = 4,
    extra: Sequence[str] = (),
    seed: LensStep | None = SEED,
) -> tuple[LoopResult, uuid.UUID, Gateway]:
    gateway = gateway or Gateway()
    authz = await env.authz_service.build_context(request_ctx(oid))
    report_id = await start_report(env.sm, authz)
    powerbi = PowerBIService(gateway, None, UnavailableTokenBroker(), env.authz_service, SETTINGS)
    planner = Planner(llm, fiscal_start_month=4, max_steps=4)  # would only be used for repairs
    executor = Executor(powerbi, planner, env.sm, SETTINGS, today=lambda: TODAY)
    primary = await roles_for(env, authz)
    others = [await roles_for(env, authz, d) for d in extra]
    schemas = {r.model.dataset_id: r.schema for r in (primary, *others)}
    run = executor_runner(executor, authz, report_id, schemas, asyncio.Semaphore(4))
    loop = InvestigationLoop(
        llm,
        run,
        system_prompt=load_sales_prompt("sales_loop"),
        goals=GOALS,
        budget=budget,
        max_rows=1000,
    )
    result = await loop.investigate("lob_growth", primary, PERIODS, seed=seed, extra=others)
    return result, report_id, gateway


# --- the agent investigates ---------------------------------------------------------------------


async def test_the_agent_drills_down_from_the_seed_and_says_when_it_is_done(env: Env) -> None:
    def drill_into_the_worst(messages: Sequence[ChatMessage]) -> dict[str, Any]:
        seen = state(messages)["queries_so_far"]
        assert [s["step"] for s in seen] == [1]
        assert seen[0]["results"][0]["figures"][0]["id"] == "lob.1.r1.m1|mtd"
        return query()

    llm = LoopLLM(drill_into_the_worst, DONE)

    result, report_id, gateway = await investigate(env, llm)

    assert [s.why for s in result.steps] == [SEED.why, DRILL["why"]]
    assert result.queries_used == 2 and not result.cut_short
    assert result.note == DONE["note"]
    drilled = result.steps[1].facts
    assert {f.row["Product[Category]"] for f in drilled} == {"DEV-A", "DEV-B", "DEV-C"}
    assert drilled[0].id == "lob.2.r1.m1|mtd" and drilled[0].ly_value is not None
    assert "TREATAS({\"DEV-A\"}, 'Product'[LOB])" in gateway.queries[1]
    async with env.sm() as session:
        recorded = await session.scalar(
            text(
                "SELECT count(*) FROM query_executions "
                "WHERE report_id = :r AND status = 'succeeded'"
            ),
            {"r": report_id},
        )
    assert recorded == 2  # every query is recorded against the report


async def test_the_budget_stops_an_agent_that_never_finishes(env: Env) -> None:
    llm = LoopLLM(query(), query(periods=["fytd", "fytd_ly"]), query(periods=["day", "day_ly"]))

    result, _, gateway = await investigate(env, llm, budget=3)

    assert result.queries_used == 3 and result.cut_short
    assert len(gateway.queries) == 3 and len(llm.calls) == 2  # the seed needs no turn


async def test_an_invalid_step_is_explained_once_then_costs_a_query(env: Env) -> None:
    wrong = query(measures=["Sales[Made Up]"])
    llm = LoopLLM(wrong, wrong, query(), DONE)

    result, _, gateway = await investigate(env, llm)

    feedback = llm.calls[1][-1].content
    assert feedback.startswith("That query cannot be run: unknown measure Sales[Made Up]")
    assert result.queries_used == 3  # seed + the invalid pair + the drill
    assert len(gateway.queries) == 2  # the invalid step never reached Power BI


async def test_the_same_query_is_never_run_twice(env: Env) -> None:
    again = {"action": "query", "step": SEED.model_dump() | {"why": "once more"}}
    llm = LoopLLM(again, DONE)

    _, _, gateway = await investigate(env, llm)

    assert "this exact query was already run" in llm.calls[1][-1].content
    assert len(gateway.queries) == 1


@pytest.mark.scenario(16)
async def test_columns_hidden_from_the_user_are_refused(env: Env) -> None:
    hidden = query(group_by=["Customer[CreditLimit]"])
    llm = LoopLLM(hidden, DONE)

    await investigate(env, llm, oid="user-b")

    assert "unknown column Customer[CreditLimit]" in llm.calls[1][-1].content
    assert "CreditLimit" not in llm.calls[0][1].content  # never shown to user-b either


@pytest.mark.scenario(9)
async def test_only_the_data_sources_of_this_table_can_be_queried(env: Env) -> None:
    finance = query(model_id="finance-ds")  # not allowed for user-a
    hr = query(model_id="hr-ds", measures=["Employee[Headcount]"], group_by=[], filters=[])
    llm = LoopLLM(finance, hr, DONE)

    result, _, gateway = await investigate(env, llm, extra=["hr-ds"])

    assert "finance-ds is not a data source listed for this table" in llm.calls[1][-1].content
    # HR is listed for this table but has no date column, so it can't compare periods.
    assert result.queries_used == 2 and len(gateway.queries) == 1
    assert '<model id="hr-ds"' in llm.calls[0][1].content


# --- failures -----------------------------------------------------------------------------------


async def test_a_failing_query_is_recorded_and_never_rewritten_by_the_llm(env: Env) -> None:
    llm = LoopLLM(query(), DONE)  # LoopLLM fails the test if asked to repair DAX

    result, report_id, _ = await investigate(
        env, llm, gateway=Gateway(fail_on="'Product'[Category]")
    )

    assert result.steps[1].error == "query_failed" and result.steps[1].facts == []
    assert "the query failed (query_failed)" in json.dumps(state(llm.calls[1]))
    async with env.sm() as session:
        statuses = (
            await session.scalars(
                text(
                    "SELECT status FROM query_executions WHERE report_id = :r ORDER BY created_at"
                ),
                {"r": report_id},
            )
        ).all()
    assert statuses == ["succeeded", "failed"]


async def test_the_agent_being_unavailable_keeps_what_was_found(env: Env) -> None:
    result, _, _ = await investigate(env, LoopLLM(fail=True))

    assert result.cut_short and len(result.steps) == 1 and result.steps[0].facts


async def test_lost_access_stops_at_once(env: Env) -> None:
    async def denied(model_id: str, label: str, dax: str, *, max_rows: int) -> QueryResult:
        raise AppError(ErrorCode.MODEL_ACCESS_DENIED, "no", 403)

    authz = await env.authz_service.build_context(request_ctx("user-a"))
    loop = InvestigationLoop(
        LoopLLM(),
        denied,
        system_prompt=load_sales_prompt("sales_loop"),
        goals=GOALS,
        budget=4,
        max_rows=10,
    )

    with pytest.raises(AppError) as raised:
        await loop.investigate("lob_growth", await roles_for(env, authz), PERIODS, seed=SEED)
    assert raised.value.code is ErrorCode.MODEL_ACCESS_DENIED


async def test_queries_across_a_build_share_one_limit(env: Env) -> None:
    gateway = Gateway(delay=0.05)
    authz = await env.authz_service.build_context(request_ctx("user-a"))
    report_id = await start_report(env.sm, authz)
    powerbi = PowerBIService(gateway, None, UnavailableTokenBroker(), env.authz_service, SETTINGS)
    executor = Executor(
        powerbi, Planner(LoopLLM(), fiscal_start_month=4, max_steps=4), env.sm, SETTINGS
    )
    roles = await roles_for(env, authz)
    run: Runner = executor_runner(
        executor, authz, report_id, {"sales-ds": roles.schema}, asyncio.Semaphore(2)
    )
    loops = [
        InvestigationLoop(
            LoopLLM(),
            run,
            system_prompt=load_sales_prompt("sales_loop"),
            goals=GOALS,
            budget=1,
            max_rows=10,
        )
        for _ in range(5)
    ]

    await asyncio.gather(
        *(
            loop.investigate(
                "lob_growth", roles, PERIODS, seed=SEED.model_copy(update={"why": str(i)})
            )
            for i, loop in enumerate(loops)
        )
    )

    assert len(gateway.queries) == 5 and gateway.most_at_once == 2


async def test_a_failed_first_query_ends_the_table_without_asking_the_agent(env: Env) -> None:
    llm = LoopLLM()  # no turns: asking would fail the test

    result, _, _ = await investigate(env, llm, gateway=Gateway(fail_on="'Product'[LOB]"))

    assert len(result.steps) == 1 and result.steps[0].error == "query_failed"
    assert llm.calls == []


class StockGateway(Gateway):
    """The stock data source has no GOLD rows (names differ between data sources)."""

    async def execute_dax(
        self, user_token: str, dataset_id: str, dax: str, *, max_rows: int, user_key: str
    ) -> QueryResult:
        result = await super().execute_dax(
            user_token, dataset_id, dax, max_rows=max_rows, user_key=user_key
        )
        if dataset_id != "hr-ds":
            return result
        rows = [r for r in result.rows if r.get("Product[LOB]") != "GOLD"]
        return QueryResult(result.columns, rows, result.truncated, result.gateway)


async def test_names_missing_from_the_stock_data_are_reported_not_guessed(env: Env) -> None:
    # hr-ds stands in for a stock data source (same fields as Sales in this test).
    stock = query(
        model_id="hr-ds",
        group_by=["Product[LOB]"],
        filters=[{"column": "Product[LOB]", "op": "in", "values": ["DEV-A", "GOLD"]}],
        periods=["day"],
        why="Stock of the falling LOBs on the latest day",
    )
    llm = LoopLLM(stock, DONE)
    authz = await env.authz_service.build_context(request_ctx("user-a"))
    report_id = await start_report(env.sm, authz)
    gateway = StockGateway()
    powerbi = PowerBIService(gateway, None, UnavailableTokenBroker(), env.authz_service, SETTINGS)
    executor = Executor(powerbi, Planner(llm, fiscal_start_month=4, max_steps=4), env.sm, SETTINGS)
    sales = await roles_for(env, authz)
    stand_in = dataclasses.replace(sales, model=authz.allowed["hr-ds"])
    schemas = {"sales-ds": sales.schema, "hr-ds": sales.schema}
    run = executor_runner(executor, authz, report_id, schemas, asyncio.Semaphore(2))
    loop = InvestigationLoop(
        llm,
        run,
        system_prompt=load_sales_prompt("sales_loop"),
        goals=GOALS,
        budget=4,
        max_rows=100,
    )

    result = await loop.investigate("lob_growth", sales, PERIODS, seed=SEED, extra=[stand_in])

    checked = result.steps[1]
    assert checked.model_id == "hr-ds" and checked.unmatched == ["GOLD"]
    assert {f.row["Product[LOB]"] for f in checked.facts} == {"DEV-A"}


# --- free-tier mode (ADR 0016 amendment 1) ------------------------------------------------------


async def test_the_agent_can_write_its_findings_when_it_finishes(env: Env) -> None:
    done = {
        **DONE,
        "findings": "- DEV-A fell most.",
        "suggestions": [{"priority": "high", "title": "Review DEV-A", "reason": "It fell.",
                         "evidence": ["lob.1.r1.m1|mtd"]}],
    }  # fmt: skip

    result, _, _ = await investigate(env, LoopLLM(done))

    assert result.written and result.findings == "- DEV-A fell most."
    assert [s.title for s in result.suggestions] == ["Review DEV-A"]


async def test_each_turn_sends_the_unchanging_part_first_and_asks_for_a_short_answer(
    env: Env,
) -> None:
    seen: list[Any] = []

    class Recording(LoopLLM):
        async def complete(
            self, messages: Sequence[ChatMessage], *, json_mode: bool = False
        ) -> LLMResponse:
            seen.append(current_limits())
            return await super().complete(messages, json_mode=json_mode)

    llm = Recording(query(), DONE)
    await investigate(env, llm)

    first, second = llm.calls
    assert [m.role for m in first] == ["system", "user", "user"]
    assert first[1].content.startswith("DATA SOURCES") and first[2].content.startswith("STATE")
    assert first[:2] == second[:2]  # identical prefix turn after turn (reused from cache)
    assert first[2] != second[2]
    assert '"$defs"' not in first[0].content  # no JSON schema in the prompt
    assert {(lim.max_output_tokens, lim.reasoning_effort) for lim in seen} == {
        (LOOP_OUTPUT_TOKENS, "low")
    }
