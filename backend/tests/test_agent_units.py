"""Deterministic agent parts: fiscal periods, DAX builder/validator, grounding, analyzer."""

import uuid
from datetime import date
from typing import Any

import pytest

from app.agent.analyzer import analyze
from app.agent.answer import template_answer, ungrounded_numbers
from app.agent.dax_builder import build_dax, literal, quote_table
from app.agent.dax_validator import DaxValidationError, validate_dax
from app.agent.executor import StepOutcome
from app.agent.fiscal import resolve_period
from app.agent.models import Filter, PlanStep, QueryPlan, TimeRange
from app.powerbi.base import QueryResult
from app.semantic.normalizer import normalize
from tests.semantic_helpers import fixture

TODAY = date(2026, 10, 6)
SALES = normalize(fixture("sales-ds"))
SALES_USER_B = normalize(fixture("sales-ds.user-b"))


# --- fiscal (Q13c: April-March) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("period", "today", "start_month", "expected"),
    [
        ("current_fy", TODAY, 4, (date(2026, 4, 1), date(2027, 3, 31))),
        ("current_fy", date(2026, 2, 15), 4, (date(2025, 4, 1), date(2026, 3, 31))),
        ("current_fy", date(2026, 4, 1), 4, (date(2026, 4, 1), date(2027, 3, 31))),
        ("last_fy", TODAY, 4, (date(2025, 4, 1), date(2026, 3, 31))),
        ("fytd", TODAY, 4, (date(2026, 4, 1), TODAY)),
        ("current_fy", TODAY, 1, (date(2026, 1, 1), date(2026, 12, 31))),
        ("last_month", TODAY, 4, (date(2026, 9, 1), date(2026, 9, 30))),
        ("last_month", date(2026, 1, 10), 4, (date(2025, 12, 1), date(2025, 12, 31))),
        ("current_month", TODAY, 4, (date(2026, 10, 1), date(2026, 10, 31))),
        ("last_year", TODAY, 4, (date(2025, 1, 1), date(2025, 12, 31))),
    ],
)
def test_periods(period: str, today: date, start_month: int, expected: tuple[date, date]) -> None:
    time = TimeRange.model_validate({"column": "Sales[InvoiceDate]", "period": period})

    assert resolve_period(time, today, start_month) == expected


def test_last_n_months_are_complete_months() -> None:
    time = TimeRange(column="Sales[InvoiceDate]", period="last_n_months", n=3)

    assert resolve_period(time, TODAY, 4) == (date(2026, 7, 1), date(2026, 9, 30))


# --- DAX builder --------------------------------------------------------------------------------


def step(**overrides: object) -> PlanStep:
    base: dict[str, object] = {
        "model_id": "sales-ds",
        "label": "GOLD sales",
        "measures": ["Sales[Total Net Sales]"],
    }
    base.update(overrides)
    return PlanStep.model_validate(base)


def test_builder_golden_query() -> None:
    dax = build_dax(
        step(
            group_by=["Product[Category]"],
            filters=[Filter(column="Product[LOB]", values=["GOLD"])],
            time=TimeRange(column="Sales[InvoiceDate]", period="current_fy"),
            top_n=5,
        ),
        today=TODAY,
        fiscal_start_month=4,
    )

    assert dax == (
        "EVALUATE\n"
        "TOPN(5, SUMMARIZECOLUMNS(\n"
        "    'Product'[Category],\n"
        "    TREATAS({\"GOLD\"}, 'Product'[LOB]),\n"
        "    FILTER(ALL('Sales'[InvoiceDate]), 'Sales'[InvoiceDate] >= DATE(2026, 4, 1) "
        "&& 'Sales'[InvoiceDate] <= DATE(2027, 3, 31)),\n"
        '    "Total Net Sales", [Total Net Sales]\n'
        "), [Total Net Sales], DESC)\n"
        "ORDER BY [Total Net Sales] DESC"
    )
    validate_dax(dax, SALES)


def test_identifiers_and_literals_are_escaped() -> None:
    assert quote_table("O'Brien Sales") == "'O''Brien Sales'"
    assert literal('Say "hi"') == '"Say ""hi"""'
    assert literal(3) == "3" and literal(2.5) == "2.5" and literal(True) == "TRUE"


def test_aggregation_when_no_measure_fits() -> None:
    dax = build_dax(
        step(
            measures=[], aggregations=[{"column": "Product[Category]", "function": "distinctcount"}]
        ),
        today=TODAY,
        fiscal_start_month=4,
    )

    assert "\"Distinctcount of Category\", DISTINCTCOUNT('Product'[Category])" in dax


# --- DAX validator ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dax", "reason"),
    [
        ('EVALUATE ROW("x", [No Such Measure])', "No Such Measure"),
        ("EVALUATE VALUES('Product'[Colour])", "Product[Colour]"),
        ("EVALUATE INFO.TABLES()", "INFO"),
        ("EVALUATE VALUES('Product'[LOB]) EVALUATE VALUES('Product'[LOB])", "one EVALUATE"),
        ("DROP TABLE x", "DEFINE or EVALUATE"),
    ],
)
def test_validator_rejects(dax: str, reason: str) -> None:
    with pytest.raises(DaxValidationError) as excinfo:
        validate_dax(dax, SALES)

    assert reason in excinfo.value.reason
    assert excinfo.value.message == "I couldn't run the query for that question. Try rephrasing it."


def test_object_hidden_from_this_user_is_rejected() -> None:
    dax = "EVALUATE VALUES('Customer'[CreditLimit])"

    validate_dax(dax, SALES)  # user-a can see it
    with pytest.raises(DaxValidationError) as excinfo:
        validate_dax(dax, SALES_USER_B)

    assert "CreditLimit" in excinfo.value.reason


def test_references_inside_strings_and_defined_names_are_ignored() -> None:
    validate_dax(
        "DEFINE MEASURE Sales[Tmp] = [Total Net Sales] * 2\n"
        'EVALUATE ROW("Label [Secret]", [Tmp], "Alias", [Total Net Sales])',
        SALES,
    )


# --- grounding ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("Net sales were 1,234,567.89.", True),
        ("Net sales were about 1.2M.", True),
        ("Net sales were 12.35 lakh.", True),
        ("Net sales were 1.3M.", False),
        ("Net sales were 999,999.", False),
        ("Growth was 12.5% between 01 Apr 2026 and 31 Mar 2027.", True),
        ("Growth was 13.5%.", False),
        ("The top 5 LOBs in FY 2026-27:", True),
    ],
)
def test_grounding(text: str, ok: bool) -> None:
    assert (ungrounded_numbers(text, [1234567.89, 12.5]) == []) is ok


# --- analyzer + template answer -----------------------------------------------------------------


def outcome(step_: PlanStep, rows: list[dict[str, Any]], truncated: bool = False) -> StepOutcome:
    columns = list(rows[0]) if rows else []
    return StepOutcome(
        step_, "EVALUATE ...", QueryResult(columns, rows, truncated, "dev_synthetic"), uuid.uuid4()
    )


def test_compare_two_single_values() -> None:
    a = step(label="This FY")
    b = step(label="Last FY")
    plan = QueryPlan(status="ready", steps=[a, b], combine="compare")

    facts = analyze(
        plan,
        [outcome(a, [{"[Total Net Sales]": 1100.0}]), outcome(b, [{"[Total Net Sales]": 1000.0}])],
        {"sales-ds": "Sales"},
        today=TODAY,
        fiscal_start_month=4,
    )

    assert facts.comparisons == [
        {
            "measure": "Total Net Sales",
            "first": "This FY",
            "second": "Last FY",
            "difference": 100.0,
            "percent_change": 10.0,
        }
    ]
    assert "+10.00%" in template_answer(facts)


def test_grouped_results_are_ranked_and_truncation_reported() -> None:
    s = step(group_by=["Product[LOB]"], order="value_desc")
    rows = [
        {"Product[LOB]": "SILVER", "[Total Net Sales]": "800"},
        {"Product[LOB]": "GOLD", "[Total Net Sales]": "1,200.5"},
    ]

    facts = analyze(
        QueryPlan(status="ready", steps=[s]),
        [outcome(s, rows, truncated=True)],
        {},
        today=TODAY,
        fiscal_start_month=4,
    )

    assert [r["Product[LOB]"] for r in facts.steps[0].top_rows] == ["GOLD", "SILVER"]
    text = template_answer(facts)
    assert "GOLD: 1,200.50" in text and "part of the list" in text


def test_no_rows_is_stated() -> None:
    s = step()
    facts = analyze(
        QueryPlan(status="ready", steps=[s]),
        [outcome(s, [])],
        {},
        today=TODAY,
        fiscal_start_month=4,
    )

    assert template_answer(facts).startswith("No data was found")
