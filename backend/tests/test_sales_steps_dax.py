"""/sales-summary queries without a database (ADR 0016, design §4): periods, step checks, the DAX
the server builds, and how results become facts."""

from datetime import date
from typing import Any

import pytest

from app.core.config import Settings
from app.powerbi.dev_synthetic import DevSyntheticGateway
from app.reports.sales.dax import compile_step
from app.reports.sales.periods import SalesPeriods, shift_year
from app.reports.sales.results import ROWS_SHOWN, StepResult, compact, parse_result
from app.reports.sales.steps import LensStep, check_lens_step, figure_count
from app.semantic.normalizer import normalize
from tests.semantic_helpers import fixture

SCHEMA = normalize(fixture("sales-ds"))
DATE = "Sales[InvoiceDate]"
PERIODS = SalesPeriods(date(2026, 10, 12), fiscal_start_month=4)


def step(**fields: Any) -> LensStep:
    base: dict[str, Any] = {
        "model_id": "sales-ds",
        "measures": ["Sales[Total Net Sales]"],
        "group_by": ["Product[LOB]"],
        "periods": ["mtd", "mtd_ly"],
    }
    return LensStep.model_validate({**base, **fields})


# --- periods ------------------------------------------------------------------------------------


def test_periods_run_from_the_financial_year_start_to_the_latest_day() -> None:
    assert PERIODS.fy_start == date(2026, 4, 1)
    assert PERIODS.ranges("day") == [(None, (date(2026, 10, 12), date(2026, 10, 12)))]
    assert PERIODS.ranges("mtd_ly") == [(None, (date(2025, 10, 1), date(2025, 10, 12)))]
    assert PERIODS.ranges("fytd_ly") == [(None, (date(2025, 4, 1), date(2025, 10, 12)))]
    months = PERIODS.ranges("months_fy")
    assert [m for m, _ in months] == [f"2026-{n:02d}" for n in range(4, 11)]
    assert months[0][1] == (date(2026, 4, 1), date(2026, 4, 30))
    assert months[-1][1] == (date(2026, 10, 1), date(2026, 10, 12))  # the month so far
    # Last year's months keep this year's keys so they line up, and stop at the same day.
    assert PERIODS.ranges("months_fy_ly")[-1] == (
        "2026-10",
        (date(2025, 10, 1), date(2025, 10, 12)),
    )


def test_a_day_before_the_fy_start_month_belongs_to_the_previous_year() -> None:
    periods = SalesPeriods(date(2027, 2, 3), fiscal_start_month=4)
    assert periods.fy_start == date(2026, 4, 1)
    assert len(periods.ranges("months_fy")) == 11  # Apr ... Feb


def test_last_year_of_29_february_is_28_february() -> None:
    assert shift_year(date(2028, 2, 29)) == date(2027, 2, 28)
    assert PERIODS.label("mtd") == "01 Oct 2026 to 12 Oct 2026"
    assert PERIODS.label("day") == "12 Oct 2026"


# --- checking a step ----------------------------------------------------------------------------


def test_a_valid_step_resolves_its_measures() -> None:
    checked, problems = check_lens_step(step(measures=["[Total Net Sales]"]), SCHEMA, DATE)
    assert problems == []
    assert checked.measures == ["Sales[Total Net Sales]"]


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"measures": ["Sales[Made Up]"]}, "unknown measure Sales[Made Up]"),
        ({"group_by": ["Store[Name]"]}, "unknown column Store[Name]"),
        ({"periods": ["mtd_ly"]}, "mtd_ly needs mtd as well"),
        ({"periods": ["mtd", "mtd"]}, "a period is listed twice"),
        ({"bands": [{"label": "all"}]}, "price bands need both band_column and bands"),
        (
            {"band_column": "Sales[InvoiceDate]", "bands": [{"label": "all"}]},
            "band_column Sales[InvoiceDate] is not a listed number column",
        ),
        (
            {
                "band_column": "Customer[CreditLimit]",
                "bands": [{"label": "a", "max": 50}, {"label": "b", "min": 40}],
            },
            "bands must be in ascending order and must not overlap",
        ),
        (
            {
                "band_column": "Customer[CreditLimit]",
                "bands": [{"label": "a", "min": 0}, {"label": "b", "min": 50, "max": 90}],
            },
            "only the last band may have no upper limit",
        ),
        (
            {
                "band_column": "Customer[CreditLimit]",
                "bands": [{"label": "a"}],
                "periods": ["months_fy"],
            },
            "price bands can't be combined with month-by-month periods",
        ),
        (
            {
                "periods": ["months_fy", "months_fy_ly", "mtd"],
                "aggregations": [{"column": "Customer[CreditLimit]", "function": "sum"}],
            },
            "50 figures in one query; ask for at most 48 (fewer measures, periods or bands)",
        ),
    ],
)
def test_invalid_steps_are_explained(fields: dict[str, Any], problem: str) -> None:
    _, problems = check_lens_step(step(**fields), SCHEMA, DATE)
    assert problem in problems


def test_a_source_without_a_date_column_cannot_compare_periods() -> None:
    _, problems = check_lens_step(step(), SCHEMA, None)
    assert "this data source has no date column, so no period can be compared" in problems


def test_hidden_columns_are_refused_for_the_user_who_cannot_see_them() -> None:
    user_b = normalize(fixture("sales-ds.user-b"))
    _, problems = check_lens_step(step(group_by=["Customer[CreditLimit]"]), user_b, DATE)
    assert problems == ["unknown column Customer[CreditLimit]"]


def test_figures_are_counted_per_measure_period_month_and_band() -> None:
    assert figure_count(step()) == 2
    assert figure_count(step(periods=["months_fy", "months_fy_ly"])) == 24
    banded = step(band_column="Customer[CreditLimit]", bands=[{"label": "a"}, {"label": "b"}])
    assert figure_count(banded) == 4


def test_the_purpose_does_not_make_a_query_different() -> None:
    assert step(why="first look").key() == step(why="again").key()
    assert step().key() != step(periods=["fytd", "fytd_ly"]).key()


# --- DAX ----------------------------------------------------------------------------------------


def test_each_measure_and_period_is_one_calculated_column() -> None:
    dax, figures = compile_step(
        step(
            filters=[{"column": "Product[LOB]", "op": "=", "values": ['GOLD "22K"']}],
            top_n=5,
            order="value_asc",
        ),
        DATE,
        PERIODS,
    )

    assert [f.alias for f in figures] == ["m1|mtd", "m1|mtd_ly"]
    assert dax.startswith("EVALUATE\nTOPN(5, SUMMARIZECOLUMNS(\n    'Product'[LOB],")
    assert 'TREATAS({"GOLD ""22K"""}, \'Product\'[LOB])' in dax  # values are escaped
    assert (
        "\"m1|mtd_ly\", CALCULATE([Total Net Sales], FILTER(ALL('Sales'[InvoiceDate]), "
        "'Sales'[InvoiceDate] >= DATE(2025, 10, 1) && 'Sales'[InvoiceDate] < DATE(2025, 10, 13)))"
    ) in dax  # whole days: a date-time column keeps the rows later in the last day
    assert dax.endswith("[m1|mtd], ASC)\nORDER BY [m1|mtd] ASC")


def test_months_and_bands_get_their_own_columns() -> None:
    _, months = compile_step(step(periods=["months_fy"]), DATE, PERIODS)
    assert months[0].alias == "m1|months_fy|2026-04" and months[0].month == "2026-04"

    banded = step(
        band_column="Customer[CreditLimit]",
        bands=[{"label": "low", "max": 1000}, {"label": "high", "min": 1000}],
        periods=["mtd"],
    )
    dax, figures = compile_step(banded, DATE, PERIODS)
    assert [(f.alias, f.band) for f in figures] == [("m1|mtd|b1", "low"), ("m1|mtd|b2", "high")]
    assert "FILTER(ALL('Customer'[CreditLimit]), 'Customer'[CreditLimit] < 1000.0)" in dax
    assert "FILTER(ALL('Customer'[CreditLimit]), 'Customer'[CreditLimit] >= 1000.0)" in dax


def test_aggregations_are_built_from_escaped_columns() -> None:
    dax, figures = compile_step(
        step(measures=[], aggregations=[{"column": "Customer[CreditLimit]", "function": "sum"}]),
        DATE,
        PERIODS,
    )
    assert figures[0].measure == "Sum of CreditLimit"
    assert "CALCULATE(SUM('Customer'[CreditLimit]), " in dax


# --- results as facts ---------------------------------------------------------------------------


def facts_of(rows: list[dict[str, Any]], *, better: bool = True, **fields: Any) -> list[Any]:
    lens_step = step(**fields)
    _, figures = compile_step(lens_step, DATE, PERIODS)
    columns = ["Product[LOB]", *(f"[{f.alias}]" for f in figures)]
    return parse_result(
        "lob", 2, lens_step, figures, columns, rows, higher_is_better={"m1": better}
    )


def test_each_period_is_paired_with_last_year() -> None:
    [gold] = facts_of([{"Product[LOB]": "GOLD", "[m1|mtd]": 80.0, "[m1|mtd_ly]": 100.0}])

    assert gold.id == "lob.2.r1.m1|mtd"
    assert gold.row == {"Product[LOB]": "GOLD"}
    assert (gold.value, gold.ly_value, gold.change, gold.percent_change) == (80, 100, -20, -20)
    assert gold.good is False


def test_good_news_follows_the_measures_direction() -> None:
    [returns] = facts_of(
        [{"Product[LOB]": "GOLD", "[m1|mtd]": 80.0, "[m1|mtd_ly]": 100.0}], better=False
    )
    assert returns.good is True  # fewer returns is good news


def test_nothing_sold_this_year_is_a_full_fall_not_a_missing_figure() -> None:
    [silent] = facts_of([{"Product[LOB]": "SILVER", "[m1|mtd]": None, "[m1|mtd_ly]": 50.0}])
    assert (silent.value, silent.change, silent.percent_change) == (0.0, -50.0, -100.0)

    [new] = facts_of([{"Product[LOB]": "NEW", "[m1|mtd]": 30.0, "[m1|mtd_ly]": None}])
    assert (new.change, new.percent_change) == (30.0, None)  # no % against nothing


def test_a_period_without_last_year_has_no_change() -> None:
    [only] = facts_of([{"Product[LOB]": "GOLD", "[m1|fytd]": 10.0}], periods=["fytd"])
    assert (only.value, only.ly_value, only.change, only.good) == (10.0, None, None, None)


def test_the_agent_sees_the_largest_movers_formatted() -> None:
    rows = [
        {"Product[LOB]": f"L{i}", "[m1|mtd]": 100.0 + i, "[m1|mtd_ly]": 100.0} for i in range(30)
    ]
    facts = facts_of(rows)
    shown = compact(
        StepResult(number=2, model_id="sales-ds", why="LOBs", step=step(), facts=facts, rows=30)
    )

    assert len(shown["results"]) == 2 * ROWS_SHOWN
    assert shown["note_rows"] == "10 largest falls and rises of 30 rows shown"
    first = shown["results"][0]["figures"][0]
    assert first == {
        "id": "lob.2.r1.m1|mtd",
        "measure": "Total Net Sales",
        "period": "mtd",
        "value": "100",
        "last_year": "100",
        "change": "no change",
        "good_news": None,
    }
    assert shown["results"][-1]["row"] == {"Product[LOB]": "L29"}


def test_a_failed_step_is_shown_without_figures() -> None:
    failed = StepResult(number=3, model_id="sales-ds", why="x", step=step(), error="query_failed")
    assert compact(failed)["error"] == "the query failed (query_failed); it has no figures"


async def test_the_dev_gateway_answers_report_queries_by_their_groups() -> None:
    dax, figures = compile_step(
        step(
            measures=[],
            aggregations=[{"column": "Customer[CreditLimit]", "function": "sum"}],
            filters=[{"column": "Product[LOB]", "op": "in", "values": ["GOLD", "SILVER"]}],
        ),
        DATE,
        PERIODS,
    )
    gateway = DevSyntheticGateway(Settings(_env_file=None, environment="test"))

    result = await gateway.execute_dax("", "sales-ds", dax, max_rows=100, user_key="u")

    # The aggregated column is an input, not a group-by column.
    assert result.columns == ["Product[LOB]", *(f"[{f.alias}]" for f in figures)]
    assert [r["Product[LOB]"] for r in result.rows] == ["GOLD", "SILVER"]
