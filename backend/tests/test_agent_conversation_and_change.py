"""Conversational replies ("hi", "what can I ask?") and 'why did X change' analysis (ADR 0013)."""

import json
import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.analyzer import Facts, StepFacts, analyze
from app.agent.answer import template_answer, ungrounded_numbers
from app.agent.change import (
    ChangePeriods,
    analyze_change,
    build_steps,
    resolve_periods,
    with_change_columns,
)
from app.agent.events import ClarificationEvent, DoneEvent, TableEvent, TokenEvent
from app.agent.executor import StepOutcome
from app.agent.help import NO_SOURCES, example_questions, help_reply, quick_topic
from app.agent.models import ChangeAnalysis, PlanStep, QueryPlan
from app.authz.messages import GENERIC_DENIAL
from app.authz.models import ModelSummary
from app.powerbi.base import GatewayPayload, QueryResult
from app.semantic.normalizer import normalize
from tests.semantic_helpers import fixture
from tests.test_agent_pipeline import Env, ScriptedLLM, WrappedGateway, build_env, plan, text_of

TODAY = date(2026, 10, 6)
FY_2025_26 = {"column": "Sales[InvoiceDate]", "period": "explicit",
              "start": "2025-04-01", "end": "2026-03-31"}  # fmt: skip
FY_2026_27 = {"column": "Sales[InvoiceDate]", "period": "explicit",
              "start": "2026-04-01", "end": "2027-03-31"}  # fmt: skip
GOLD = [{"column": "Product[LOB]", "op": "=", "values": ["GOLD"]}]


def change(**overrides: object) -> ChangeAnalysis:
    data: dict[str, object] = {
        "model_id": "sales-ds",
        "label": "GOLD net sales",
        "measure": "Sales[Total Net Sales]",
        "filters": GOLD,
        "baseline": FY_2025_26,
        "current": FY_2026_27,
        "drivers": ["Product[Category]"],
    }
    data.update(overrides)
    return ChangeAnalysis.model_validate(data)


def model(dataset_id: str, name: str) -> ModelSummary:
    return ModelSummary(dataset_id, name, None, uuid.uuid4())


# --- conversation -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("hi", "greeting"),
        ("Hello!", "greeting"),
        ("hii there", "greeting"),
        ("Good morning", "greeting"),
        ("thanks", "thanks"),
        ("Thank you so much!", "thanks"),
        ("hi, what are gold sales?", None),
        ("history of gold sales", None),
    ],
)
def test_small_talk_is_recognised_without_the_llm(text: str, topic: str | None) -> None:
    assert quick_topic(text) == topic


def test_examples_come_from_the_users_visible_schema() -> None:
    examples = example_questions(normalize(fixture("sales-ds")))

    assert examples[0] == "What is Total Net Sales this financial year?"
    assert examples[1] == "Total Net Sales by LOB for last financial year"
    assert examples[2].startswith("Why did Total Net Sales change")


def test_help_lists_only_the_users_models_and_handles_none() -> None:
    reply = help_reply(
        "data_sources",
        user_name="Fena Patel",
        models=[model("sales-ds", "KMJL Sales New")],
        schema=None,
        fiscal_start_month=4,
    )
    greeting = help_reply("greeting", user_name="Fena Patel", models=[], schema=None,
                          fiscal_start_month=4)  # fmt: skip

    assert reply == "You can ask about: **KMJL Sales New**."
    assert greeting == f"Hi Fena! {NO_SOURCES}"


# --- periods ------------------------------------------------------------------------------------


def test_a_running_period_needs_the_users_choice() -> None:
    question = resolve_periods(change(), TODAY, 4)

    assert isinstance(question, str)
    assert "01 Apr 2025 to 06 Oct 2025 vs 01 Apr 2026 to 06 Oct 2026" in question
    assert "01 Apr 2025 to 31 Mar 2026 with 01 Apr 2026 to 06 Oct 2026 so far" in question


def test_like_for_like_compares_the_same_dates() -> None:
    periods = resolve_periods(change(basis="like_for_like"), TODAY, 4)

    assert periods == ChangePeriods(
        (date(2025, 4, 1), date(2025, 10, 6)), (date(2026, 4, 1), date(2026, 10, 6))
    )


def test_full_period_caps_the_running_one_at_today() -> None:
    periods = resolve_periods(change(basis="full_period"), TODAY, 4)

    assert periods == ChangePeriods(
        (date(2025, 4, 1), date(2026, 3, 31)), (date(2026, 4, 1), date(2026, 10, 6))
    )


def test_complete_periods_need_no_question() -> None:
    complete = change(
        baseline={**FY_2025_26, "start": "2024-04-01", "end": "2025-03-31"}, current=FY_2025_26
    )

    assert isinstance(resolve_periods(complete, TODAY, 4), ChangePeriods)


# --- queries and analysis -----------------------------------------------------------------------


PERIODS = ChangePeriods(
    (date(2025, 4, 1), date(2025, 10, 6)), (date(2026, 4, 1), date(2026, 10, 6))
)
A, B = "[01 Apr 2025 to 06 Oct 2025]", "[01 Apr 2026 to 06 Oct 2026]"


def test_one_query_for_totals_and_one_per_breakdown_both_periods_side_by_side() -> None:
    steps = build_steps(
        change(drivers=["Product[Category]", "Product[LOB]", "Sales[InvoiceDate]"]),
        PERIODS,
        max_steps=3,
    )

    assert [s.label for s in steps] == [
        "GOLD net sales: 01 Apr 2025 to 06 Oct 2025 vs 01 Apr 2026 to 06 Oct 2026",
        "GOLD net sales by Category",
        "GOLD net sales by LOB",
    ]  # third breakdown dropped: step limit
    dax = steps[1].custom_dax or ""
    assert "'Product'[Category]" in dax and "TREATAS({\"GOLD\"}, 'Product'[LOB])" in dax
    current = (
        '"01 Apr 2026 to 06 Oct 2026", CALCULATE([Total Net Sales], '
        "FILTER(ALL('Sales'[InvoiceDate]), 'Sales'[InvoiceDate] >= DATE(2026, 4, 1) "
        "&& 'Sales'[InvoiceDate] <= DATE(2026, 10, 6)))"
    )
    assert current in dax


def _outcome(
    label: str, columns: list[str], rows: list[dict[str, object]], group: str = ""
) -> StepOutcome:
    step = PlanStep(model_id="sales-ds", label=label, group_by=[group] if group else [])
    return StepOutcome(
        step, "EVALUATE ...", QueryResult(columns, rows, False, "rest"), uuid.uuid4()
    )


def test_change_is_explained_by_the_biggest_movers_and_the_template_is_grounded() -> None:
    outcomes = [
        _outcome("total", [A, B], [{A: 1000.0, B: 700.0}]),
        _outcome(
            "by category",
            ["Product[Category]", A, B],
            [
                {"Product[Category]": "BANGLE", A: 600.0, B: 350.0},
                {"Product[Category]": "CHAIN", A: 300.0, B: 250.0},
                {"Product[Category]": "RING", A: 100.0, B: 100.0},
            ],
            group="Product[Category]",
        ),
    ]

    facts = analyze_change(change(), PERIODS, outcomes, "Sales")
    summary = facts.change or {}

    assert (summary["change"], summary["percent_change"]) == (-300.0, -30.0)
    decreases = summary["breakdowns"][0]["biggest_decreases"]
    assert [(d["item"], d["change"], d["share_of_total_change"]) for d in decreases] == [
        ("BANGLE", -250.0, 83.33),
        ("CHAIN", -50.0, 16.67),
    ]
    answer = template_answer(facts)
    assert "BANGLE -250 (83.33% of the change)" in answer
    assert ungrounded_numbers(answer, facts.numbers()) == []


def test_displayed_tables_get_change_columns() -> None:
    columns, rows = with_change_columns([A, B], [{A: 200.0, B: 150.0}], PERIODS)

    assert columns == [A, B, "[Change]", "[Change %]"]
    assert rows[0]["[Change]"] == -50.0 and rows[0]["[Change %]"] == -25.0


def test_cross_model_grouped_compare_matches_rows_by_value() -> None:
    sales = _outcome("Sales by store", ["Sales[Store]", "[Net Sales]"],
                     [{"Sales[Store]": "Hyderabad", "[Net Sales]": 900.0},
                      {"Sales[Store]": "Pune", "[Net Sales]": 500.0}], "Sales[Store]")  # fmt: skip
    target = _outcome("Target by store", ["Target[STORE]", "[Target]"],
                      [{"Target[STORE]": "HYDERABAD ", "[Target]": 1000.0},
                       {"Target[STORE]": "Delhi", "[Target]": 300.0}], "Target[STORE]")  # fmt: skip
    compare = QueryPlan(status="ready", steps=[sales.step, target.step], combine="compare")

    facts = analyze(compare, [sales, target], {}, today=TODAY, fiscal_start_month=4)

    assert facts.comparisons == [
        {
            "group": "Hyderabad",
            "first": "Sales by store",
            "second": "Target by store",
            "first_value": 900.0,
            "second_value": 1000.0,
            "difference": -100.0,
            "percent_change": -10.0,
        }
    ]
    assert isinstance(facts, Facts) and all(isinstance(s, StepFacts) for s in facts.steps)


# --- whole turns (test database) ----------------------------------------------------------------


@pytest.fixture
async def env(committed_sessionmaker: async_sessionmaker[AsyncSession]) -> Env:
    return await build_env(committed_sessionmaker)


@pytest.mark.integration
async def test_hi_gets_a_greeting_without_the_llm_or_power_bi(env: Env) -> None:
    llm = ScriptedLLM([])  # any LLM call would fail: no replies scripted
    gateway = WrappedGateway()

    events = await env.run(env.agent(llm, gateway), "user-a", "hi")

    text = text_of(events)
    assert text.startswith("Hi user-a!") and "**HR**, **Sales**" in text
    assert "Finance" not in text and "What is Total Net Sales this financial year?" in text
    assert llm.calls == [] and gateway.queries == []
    assert isinstance(events[-1], DoneEvent) and events[-1].message_id is not None


@pytest.mark.integration
async def test_what_can_i_ask_is_answered_by_the_server(env: Env) -> None:
    llm = ScriptedLLM([plan(status="help", help_topic="capabilities")])

    events = await env.run(env.agent(llm), "user-a", "what can I ask you?")

    text = text_of(events)
    assert text.startswith("I answer questions about your Power BI data")
    assert "financial year starts in April" in text and "Finance" not in text


@pytest.mark.integration
async def test_user_without_any_model_is_told_so(env: Env) -> None:
    events = await env.run(env.agent(ScriptedLLM([])), "user-nobody", "what are gold sales?")

    assert text_of(events) == NO_SOURCES


@pytest.mark.integration
async def test_why_question_asks_about_the_running_year_then_explains_the_change(env: Env) -> None:
    # The date breakdown is dropped by the server (one row per day explains nothing).
    pending = change(drivers=["Product[Category]", "Sales[InvoiceDate]"]).model_dump(mode="json")
    agreed = {**pending, "basis": "like_for_like"}
    gateway = WrappedGateway()
    llm = ScriptedLLM(
        [
            plan(change=pending),
            plan(change=agreed),
            "Not grounded: sales fell by 12345678.9",  # invented number -> template answer
        ]
    )
    agent = env.agent(llm, gateway)
    chat = await env.chat("user-a")

    first = await env.run(agent, "user-a", "why is gold selling low in 2026-27 vs 2025-26?", chat)
    second = await env.run(agent, "user-a", "compare the same dates", chat)

    assert isinstance(first[-2], ClarificationEvent) and "same dates" in first[-2].question
    assert gateway.queries[:2] and len(gateway.queries) == 2  # totals + one breakdown
    previous = llm.calls[1][-1].content.split("PREVIOUS TURN", 1)[1]
    assert "clarification_asked" in previous and "why is gold selling low" in previous
    tables = [e for e in second if isinstance(e, TableEvent)]
    assert [t.columns[-2:] for t in tables] == [["[Change]", "[Change %]"]] * 2
    answer = text_of(second)
    assert "went from" in answer and "outside reasons" in answer and "12345678.9" not in answer
    assert all(isinstance(e, TokenEvent | TableEvent) or e.__class__.__name__ in
               ("StatusEvent", "DoneEvent") for e in second)  # fmt: skip


@pytest.mark.integration
async def test_why_question_on_a_forbidden_model_is_denied(env: Env) -> None:
    forbidden = {**change().model_dump(mode="json"), "model_id": "finance-ds"}
    gateway = WrappedGateway()

    events = await env.run(env.agent(ScriptedLLM([plan(change=forbidden)]), gateway), "user-a",
                           "why did finance drop?")  # fmt: skip

    assert text_of(events) == GENERIC_DENIAL and gateway.queries == []
    assert json.dumps(text_of(events)).count("Finance") == 0


class ValueSearchGateway(WrappedGateway):
    """dev_synthetic that finds "sivler" as the stored LOB value SILVER (like Fabric IQ)."""

    async def search_values(self, user_token: str, dataset_id: str, terms: list[str]) -> Any:
        self.queries.append(f"VALUESEARCH {terms}")
        results = {
            t: [{"Table": "Product", "Column": "LOB", "Value": "SILVER", "Score": 0.82}]
            for t in terms
            if t.lower() == "sivler"
        }
        return GatewayPayload(data={"Results": results}, gateway=self.name)


SILVER_STEP = {
    "model_id": "sales-ds",
    "label": "SILVER net sales this FY",
    "measures": ["Sales[Total Net Sales]"],
    "filters": [{"column": "Product[LOB]", "op": "=", "values": ["SILVER"]}],
    "time": {"column": "Sales[InvoiceDate]", "period": "current_fy"},
}


@pytest.mark.integration
async def test_misspelt_value_is_found_in_the_data_and_answered(env: Env) -> None:
    gateway = ValueSearchGateway()
    llm = ScriptedLLM(
        [
            plan(status="cannot_answer", unresolved_terms=["sivler"]),
            plan(SILVER_STEP),
            "SILVER sales are shown in the table.",
        ]
    )

    events = await env.run(env.agent(llm, gateway), "user-a", "net sales of sivler this year")

    feedback = llm.calls[1][-1].content
    assert "'Product'[LOB] = \"SILVER\" (match score 0.82)" in feedback
    assert gateway.queries[0] == "VALUESEARCH ['sivler']"
    assert any("TREATAS({\"SILVER\"}, 'Product'[LOB])" in q for q in gateway.queries)
    assert text_of(events).startswith("SILVER sales are shown")


@pytest.mark.integration
async def test_unknown_word_without_a_value_match_still_gets_the_refusal(env: Env) -> None:
    gateway = ValueSearchGateway()
    llm = ScriptedLLM([plan(status="cannot_answer", unresolved_terms=["salary"])])

    events = await env.run(env.agent(llm, gateway), "user-a", "show salaries")

    assert text_of(events) == GENERIC_DENIAL
    assert len(llm.calls) == 1  # no re-plan without a match
