"""The four /sales-summary tables built from facts (design §6): layout, colours by the measure's
direction, the figures the server computes, and drill-downs. No database, no LLM."""

import dataclasses
import uuid
from datetime import date
from typing import Any

from app.authz.models import ModelSummary
from app.reports.sales.dax import compile_step
from app.reports.sales.discovery import ModelRoles
from app.reports.sales.lenses import KEYS, Lens
from app.reports.sales.loop import LoopResult
from app.reports.sales.periods import SalesPeriods
from app.reports.sales.results import StepResult, parse_result
from app.reports.sales.seeds import seed_step
from app.reports.sales.steps import LensStep
from app.reports.sales.tables import DRILL_ROWS, MAX_MAIN_ROWS, build_table
from app.reports.sales.writer import template_findings
from app.semantic.normalizer import normalize
from tests.semantic_helpers import fixture

PERIODS = SalesPeriods(date(2026, 10, 12), fiscal_start_month=4)  # FYTD: 195 days
DATE = "Sales[InvoiceDate]"
MEASURES = {
    "sales_value": "Sales[Total Net Sales]",
    "sales_weight": "Sales[Weight]",
    "stock_value": "Stock[Closing Stock]",
}
ROLES = ModelRoles(
    model=ModelSummary("sales-ds", "Sales", "Sales", uuid.uuid4()),
    schema=normalize(fixture("sales-ds")),
    lenses=("lob_growth",),
    date_column=DATE,
    columns={"lob": "Product[LOB]", "subcategory": "Product[Category]"},
    measures=MEASURES,
    lower_is_better=frozenset(),
    reason="",
)


def ran(lens: Lens, number: int, step: LensStep, rows: list[dict[str, Any]]) -> StepResult:
    _, figures = compile_step(step, DATE, PERIODS)
    columns = [*step.group_by, *(f"[{f.alias}]" for f in figures)]
    facts = parse_result(KEYS[lens], number, step, figures, columns, rows, higher_is_better={})
    return StepResult(
        number=number, model_id="sales-ds", why=step.why, step=step, facts=facts, rows=len(rows)
    )


def table(lens: Lens, *steps: StepResult) -> Any:
    return build_table(
        lens, LoopResult(lens=lens, model_id="sales-ds", steps=list(steps)), ROLES, PERIODS
    )


def texts(row: list[Any]) -> list[str]:
    return [c.text for c in row]


# --- LOB growth ---------------------------------------------------------------------------------


def test_lobs_are_listed_by_sales_with_growth_in_green_and_degrowth_in_red() -> None:
    seed = LensStep(
        model_id="sales-ds",
        measures=["Sales[Total Net Sales]", "Sales[Weight]"],
        group_by=["Product[LOB]"],
        periods=["day", "day_ly", "mtd", "mtd_ly", "fytd", "fytd_ly"],
    )
    rows = [
        {
            "Product[LOB]": "SILVER",
            "[m1|mtd]": 50.0,
            "[m1|mtd_ly]": 40.0,
            "[m2|mtd]": 9.0,
            "[m2|mtd_ly]": 10.0,
            "[m1|fytd]": 500.0,
            "[m1|fytd_ly]": 400.0,
        },
        {
            "Product[LOB]": "GOLD",
            "[m1|mtd]": 80.0,
            "[m1|mtd_ly]": 100.0,
            "[m1|day]": 5.0,
            "[m1|day_ly]": 4.0,
        },
    ]

    lob, derived = table("lob_growth", ran("lob_growth", 1, seed, rows))

    main = lob.main
    assert main.columns == [
        "LOB",
        "Day vs LY",
        "MTD",
        "MTD vs LY",
        "FYTD",
        "FYTD vs LY",
        "Weight MTD vs LY",
        "Status",
    ]
    gold, silver = main.rows  # by MTD sales, largest first
    assert texts(gold) == ["GOLD", "+25.00%", "80", "-20.00%", "", "", "", "Degrowth"]
    assert (gold[3].tone, gold[7].tone) == ("bad", "bad")
    assert gold[3].fact == "lob.1.r2.m1|mtd"
    assert texts(silver)[3:] == ["+25.00%", "500", "+25.00%", "-10.00%", "Growth"]
    assert silver[6].tone == "bad"  # value up, weight down: both shown
    assert derived == []


# --- sub-category loss --------------------------------------------------------------------------


def test_sub_categories_show_each_month_with_the_largest_loss_first() -> None:
    seed = LensStep(
        model_id="sales-ds",
        measures=["Sales[Total Net Sales]"],
        group_by=["Product[Category]"],
        periods=["mtd", "mtd_ly", "fytd", "fytd_ly", "months_fy", "months_fy_ly"],
    )
    rows = [
        {
            "Product[Category]": f"C{i}",
            "[m1|fytd]": 100.0 - i,
            "[m1|fytd_ly]": 100.0,
            "[m1|months_fy|2026-04]": 10.0,
            "[m1|months_fy_ly|2026-04]": 20.0,
        }
        for i in range(25)
    ]

    sub, _ = table("subcategory_loss", ran("subcategory_loss", 1, seed, rows))

    main = sub.main
    assert main.columns[:4] == ["Category", "MTD", "MTD vs LY", "FYTD vs LY"]
    assert main.columns[4:] == [
        f"{m} 2026 vs LY" for m in ("Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct")
    ]
    assert (
        len(main.rows) == MAX_MAIN_ROWS
        and main.note == "The 20 largest losses of 25 sub-categories."
    )
    assert texts(main.rows[0])[:5] == ["C24", "", "", "-24.00%", "-50.00%"]
    assert main.rows[0][4].fact == "sub.1.r25.m1|months_fy|2026-04"


# --- stock turn ---------------------------------------------------------------------------------


def test_stock_turn_is_computed_from_sales_and_month_end_stock() -> None:
    seed = LensStep(
        model_id="sales-ds",
        measures=["Sales[Total Net Sales]", "Stock[Closing Stock]"],
        group_by=["Product[LOB]"],
        periods=["fytd", "month_ends_fy"],
    )
    months = [f"2026-{m:02d}" for m in range(4, 11)]
    held = {f"[m2|month_ends_fy|{m}]": 365.0 for m in months}
    rows = [
        {"Product[LOB]": "GOLD", "[m1|fytd]": 195.0, **held},  # sells its stock once a year
        {"Product[LOB]": "PLATINUM", "[m1|fytd]": None, **held},  # holds stock, sells nothing
    ]

    stock, derived = table("stock_turn", ran("stock_turn", 1, seed, rows))

    main = stock.main
    assert main.columns == [
        "LOB",
        "Sales FYTD",
        "Average month-end stock",
        "Stock turn (annualised)",
        "Stock on 12 Oct",
        "",
    ]
    platinum, gold = main.rows  # slowest first
    assert texts(gold) == ["GOLD", "195", "365", "1", "365", ""]
    assert gold[3].fact == "stock.1.r1.turn"
    assert texts(platinum)[3:] == ["0", "365", "No sales this year"] and platinum[5].tone == "bad"
    assert {(f.id, f.value) for f in derived} >= {
        ("stock.1.r1.turn", 1.0),
        ("stock.1.r1.avg_stock", 365.0),
    }


def test_a_stock_turn_measure_of_the_model_is_used_as_it_is() -> None:
    roles = dataclasses.replace(ROLES, measures={"stock_turn": "Stock[Stock Turn]"})
    seed = LensStep(
        model_id="sales-ds",
        measures=["Stock[Stock Turn]"],
        group_by=["Product[LOB]"],
        periods=["fytd", "fytd_ly"],
    )
    result = LoopResult(
        lens="stock_turn",
        model_id="sales-ds",
        steps=[
            ran(
                "stock_turn",
                1,
                seed,
                [{"Product[LOB]": "GOLD", "[m1|fytd]": 1.5, "[m1|fytd_ly]": 2.0}],
            )
        ],
    )

    stock, derived = build_table("stock_turn", result, roles, PERIODS)

    assert stock.main.columns == ["LOB", "Stock turn FYTD", "vs LY"]
    assert texts(stock.main.rows[0]) == ["GOLD", "1.50", "-25.00%"] and derived == []


# --- price ranges -------------------------------------------------------------------------------


def test_price_bands_of_the_model_show_their_share_of_sales() -> None:
    seed = LensStep(
        model_id="sales-ds",
        measures=["Sales[Total Net Sales]"],
        group_by=["Product[Band]"],
        periods=["mtd", "mtd_ly", "fytd", "fytd_ly"],
    )
    rows = [
        {"Product[Band]": "Under 25k", "[m1|fytd]": 300.0, "[m1|fytd_ly]": 200.0},
        {"Product[Band]": "25k+", "[m1|fytd]": 100.0, "[m1|fytd_ly]": 200.0},
    ]

    price, derived = table("price_range", ran("price_range", 1, seed, rows))

    assert price.main.columns == ["Band", "MTD", "MTD vs LY", "FYTD", "FYTD vs LY", "Share of FYTD"]
    assert texts(price.main.rows[0]) == ["Under 25k", "", "", "300", "+50.00%", "75.00%"]
    assert [f.value for f in derived] == [75.0, 25.0]


def test_bands_the_agent_set_become_the_price_table() -> None:
    seed = LensStep(
        model_id="sales-ds",
        aggregations=[{"column": "Customer[CreditLimit]", "function": f} for f in ("min", "max")],
        periods=["fytd"],
    )
    banded = LensStep(
        model_id="sales-ds",
        measures=["Sales[Total Net Sales]"],
        band_column="Customer[CreditLimit]",
        bands=[{"label": "Under 50k", "max": 50000}, {"label": "50k+", "min": 50000}],
        periods=["fytd", "fytd_ly"],
        why="Sales by price range",
    )
    rows = [
        {
            "[m1|fytd|b1]": 60.0,
            "[m1|fytd_ly|b1]": 50.0,
            "[m1|fytd|b2]": 40.0,
            "[m1|fytd_ly|b2]": 50.0,
        }
    ]

    price, _ = table(
        "price_range",
        ran("price_range", 1, seed, [{"[m1|fytd]": 1000.0, "[m2|fytd]": 90000.0}]),
        ran("price_range", 2, banded, rows),
    )

    assert price.main.columns == [
        "Price range",
        "MTD",
        "MTD vs LY",
        "FYTD",
        "FYTD vs LY",
        "Share of FYTD",
    ]
    assert [texts(r) for r in price.main.rows] == [
        ["Under 50k", "", "", "60", "+20.00%", "60.00%"],
        ["50k+", "", "", "40", "-20.00%", "40.00%"],
    ]
    assert price.drilldowns == []  # the banded query is the main table, not shown twice


def test_without_price_bands_the_table_says_so() -> None:
    seed = LensStep(
        model_id="sales-ds",
        aggregations=[{"column": "Customer[CreditLimit]", "function": "max"}],
        periods=["fytd"],
    )

    price, _ = table("price_range", ran("price_range", 1, seed, [{"[m1|fytd]": 9.0}]))

    assert price.main is None
    assert price.notes == ["No price ranges were set for this data source."]


# --- drill-downs and notes ----------------------------------------------------------------------


def test_each_further_query_is_a_drill_down_with_the_largest_moves() -> None:
    seed = LensStep(
        model_id="sales-ds",
        measures=["Sales[Total Net Sales]"],
        group_by=["Product[LOB]"],
        periods=["mtd", "mtd_ly"],
    )
    stores = LensStep(
        model_id="sales-ds",
        measures=["Sales[Total Net Sales]"],
        group_by=["Store[Name]"],
        periods=["mtd", "mtd_ly", "months_fy"],
        why="Stores behind GOLD's fall",
    )
    rows = [
        {
            "Store[Name]": f"S{i}",
            "[m1|mtd]": 100.0 + i,
            "[m1|mtd_ly]": 100.0,
            "[m1|months_fy|2026-10]": 1.0,
        }
        for i in range(20)
    ]
    failed = StepResult(number=3, model_id="sales-ds", why="x", step=stores, error="query_failed")

    lob, _ = table(
        "lob_growth",
        ran("lob_growth", 1, seed, [{"Product[LOB]": "GOLD", "[m1|mtd]": 1.0, "[m1|mtd_ly]": 2.0}]),
        ran("lob_growth", 2, stores, rows),
        failed,
    )

    [drill] = lob.drilldowns  # the failed query has no table
    assert drill.title == "Stores behind GOLD's fall"
    # A month without figures (here every month but October) gets no column.
    assert drill.columns == ["Name", "MTD", "MTD vs LY", "Oct 2026"]
    assert (
        len(drill.rows) == 2 * DRILL_ROWS
        and drill.note == "The 8 largest falls and rises of 20 rows."
    )
    assert texts(drill.rows[0])[:3] == ["S0", "100", "0.00%"] and drill.rows[0][2].tone == "neutral"


def test_names_another_data_source_lacks_are_noted() -> None:
    seed = LensStep(
        model_id="sales-ds",
        measures=["Sales[Total Net Sales]"],
        group_by=["Product[LOB]"],
        periods=["mtd", "mtd_ly"],
    )
    stock = ran("lob_growth", 2, seed, []).model_copy(
        update={"model_id": "stock-ds", "unmatched": ["GOLD", "Store 9"]}
    )

    lob, _ = table("lob_growth", ran("lob_growth", 1, seed, []), stock)

    assert lob.notes == ["Not found in the data of stock-ds: GOLD, Store 9."]


def test_the_template_names_the_largest_falls_and_rises() -> None:
    seed = LensStep(
        model_id="sales-ds",
        measures=["Sales[Total Net Sales]"],
        group_by=["Product[LOB]"],
        periods=["day", "day_ly", "mtd", "mtd_ly"],
    )
    rows = [
        {"Product[LOB]": "GOLD", "[m1|day]": 8.0, "[m1|day_ly]": 10.0},
        {"Product[LOB]": "SILVER", "[m1|day]": 12.0, "[m1|day_ly]": 10.0},
    ]
    lob, _ = table("lob_growth", ran("lob_growth", 1, seed, rows))

    assert template_findings(lob.main) == (
        "- Largest falls against last year (Day vs LY): GOLD (-20.00%).\n"
        "- Largest rises against last year (Day vs LY): SILVER (+20.00%)."
    )
    assert template_findings(None) == ""


# --- the server's first query per table ---------------------------------------------------------


def roles_with(**fields: Any) -> ModelRoles:
    return dataclasses.replace(ROLES, **fields)


def test_first_queries_use_the_fields_discovery_found() -> None:
    lob = seed_step("lob_growth", ROLES)
    assert lob is not None and lob.measures == ["Sales[Total Net Sales]", "Sales[Weight]"]
    assert lob.periods == ["day", "day_ly", "mtd", "mtd_ly", "fytd", "fytd_ly"]

    sub = seed_step("subcategory_loss", ROLES)
    assert sub is not None and sub.group_by == ["Product[Category]"]
    assert "months_fy_ly" in sub.periods and len(sub.measures) == 1


def test_stock_turn_pairs_sales_and_stock_in_the_same_unit() -> None:
    weight = roles_with(
        measures={
            "sales_value": "Sales[Net]",
            "sales_weight": "Sales[Gm]",
            "stock_weight": "Stock[Gm]",
        }
    )
    step = seed_step("stock_turn", weight)
    assert step is not None and step.measures == ["Sales[Gm]", "Stock[Gm]"]
    assert step.periods == ["fytd", "month_ends_fy"]

    own = seed_step("stock_turn", roles_with(measures={"stock_turn": "Stock[Turn]"}))
    assert own is not None and own.measures == ["Stock[Turn]"]
    assert seed_step("stock_turn", roles_with(measures={"sales_value": "S[V]"})) is None


def test_price_ranges_start_from_the_band_column_or_the_price_spread() -> None:
    banded = seed_step("price_range", roles_with(columns={"price_band": "Product[Band]"}))
    assert banded is not None and banded.group_by == ["Product[Band]"]

    spread = seed_step("price_range", roles_with(columns={"price": "Product[MRP]"}))
    assert spread is not None
    assert [a.function for a in spread.aggregations] == ["min", "max", "average"]

    assert seed_step("price_range", ROLES) is None  # neither: no table
    assert seed_step("lob_growth", roles_with(date_column=None)) is None
