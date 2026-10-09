"""A measure that ignores the breakdown (same value on every row, e.g. built with ALL()) must
never be shown as a per-item split (live bug 2026-10-08: "Total Net Sales (Valid Stores)")."""

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.events import TableEvent
from app.agent.executor import StepOutcome
from app.agent.models import PlanStep
from app.agent.orchestrator import flat_breakdowns
from app.powerbi.base import QueryResult
from tests.test_agent_pipeline import (
    Env,
    ScriptedLLM,
    WrappedGateway,
    build_env,
    plan,
    text_of,
)

CATEGORY = "Product[Category]"


def outcome(rows: list[dict[str, Any]]) -> StepOutcome:
    step = PlanStep(model_id="sales-ds", label="by category", group_by=[CATEGORY])
    columns = list(rows[0]) if rows else []
    return StepOutcome(
        step, "EVALUATE ...", QueryResult(columns, rows, False, "rest"), uuid.uuid4()
    )


def by_category(*values: float, measure: str = "[Total Net Sales]") -> list[dict[str, Any]]:
    names = ["BANGLE", "CHAIN", "RING", "EARRING"]
    return [{CATEGORY: n, measure: v} for n, v in zip(names, values, strict=False)]


def test_same_value_on_every_row_is_a_flat_breakdown() -> None:
    flat = flat_breakdowns([outcome(by_category(9.0, 9.0, 9.0))])

    assert [(f.groups, f.values, f.rows) for f in flat] == [([CATEGORY], ["[Total Net Sales]"], 3)]


@pytest.mark.parametrize(
    "rows",
    [
        by_category(9.0, 8.0, 9.0),  # varies
        by_category(9.0, 9.0),  # too few rows to tell
        [{"[Total Net Sales]": 9.0}] * 3,  # no breakdown at all
    ],
)
def test_varying_small_or_ungrouped_results_are_fine(rows: list[dict[str, Any]]) -> None:
    assert flat_breakdowns([outcome(rows)]) == []


# --- whole turns (test database) ----------------------------------------------------------------


class ScriptedRows(WrappedGateway):
    """Returns the given row sets in order, one per query."""

    def __init__(self, *row_sets: list[dict[str, Any]]) -> None:
        super().__init__()
        self.row_sets = list(row_sets)

    async def execute_dax(
        self, user_token: str, dataset_id: str, dax: str, *, max_rows: int, user_key: str
    ) -> QueryResult:
        self.queries.append(dax)
        rows = self.row_sets.pop(0)
        return QueryResult(list(rows[0]), rows, False, self.name)


def step(measure: str) -> dict[str, Any]:
    return {
        "model_id": "sales-ds",
        "label": "Net sales by category",
        "measures": [measure],
        "group_by": [CATEGORY],
        "time": {"column": "Sales[InvoiceDate]", "period": "current_fy"},
    }


@pytest.fixture
async def env(committed_sessionmaker: async_sessionmaker[AsyncSession]) -> Env:
    return await build_env(committed_sessionmaker)


@pytest.mark.integration
async def test_flat_result_is_replanned_and_only_the_real_breakdown_is_shown(env: Env) -> None:
    gateway = ScriptedRows(by_category(900.0, 900.0, 900.0), by_category(500.0, 300.0, 100.0))
    llm = ScriptedLLM(
        [plan(step("Sales[Total Net Sales]")), plan(step("Sales[Total Net Sales]")), "Done."]
    )

    events = await env.run(env.agent(llm, gateway), "user-a", "net sales by category")

    feedback = llm.calls[1][-1].content
    assert "returned the same value for every Product[Category] (3 rows)" in feedback
    tables = [e for e in events if isinstance(e, TableEvent)]
    assert len(tables) == 1 and [r["[Total Net Sales]"] for r in tables[0].rows] == [
        500.0,
        300.0,
        100.0,
    ]
    assert len(gateway.queries) == 2


@pytest.mark.integration
async def test_still_flat_after_replan_is_stated_in_the_answer(env: Env) -> None:
    flat = by_category(900.0, 900.0, 900.0)
    gateway = ScriptedRows(flat, flat)
    llm = ScriptedLLM(
        [
            plan(step("Sales[Total Net Sales]")),
            plan(step("Sales[Total Net Sales]")),
            "Every category sold 1234567.89.",  # invented number -> template answer
        ]
    )

    events = await env.run(env.agent(llm, gateway), "user-a", "net sales by category")

    assert "doesn't break down that way" in llm.calls[2][-1].content  # FACTS notes for the LLM
    answer = text_of(events)
    assert "Note: Total Net Sales has the same value for every Product[Category]" in answer
