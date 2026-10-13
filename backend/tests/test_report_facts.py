"""/summary report building blocks that need no database (ADR 0015, design §2.1)."""

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from app.agent.answer import ungrounded_numbers
from app.agent.change import ChangePeriods, build_steps
from app.agent.dax_validator import validate_dax
from app.agent.models import Aggregation, ChangeAnalysis, TimeRange
from app.core.config import Settings
from app.powerbi.base import GatewayCapability
from app.powerbi.dev_synthetic import DevSyntheticGateway
from app.powerbi.errors import CapabilityNotSupportedError, GatewayUnavailableError
from app.powerbi.rest import PowerBiRestGateway
from app.reports import facts
from app.reports.builder import load_prompt
from app.reports.facts import ReportKpi
from app.reports.text import change_words, clean_text, compact_indian, format_indian
from app.semantic.normalizer import normalize
from tests.semantic_helpers import fixture
from tests.test_powerbi_service import Access, ScriptedGateway, authz, service

DATE = "Sales[InvoiceDate]"
SALES = ReportKpi("k1", "Net sales", "Sales[Total Net Sales]", None, None)
SCHEMA = normalize(fixture("sales-ds"))


# --- periods ------------------------------------------------------------------------------------


def test_periods_cover_the_day_the_previous_data_day_a_week_ago_and_month_to_date() -> None:
    periods = facts.report_periods(date(2026, 10, 12), date(2026, 10, 10))

    assert periods.day == date(2026, 10, 12)
    assert periods.prev_day == date(2026, 10, 10)  # the latest earlier day *with data*
    assert periods.week_ago == date(2026, 10, 5)
    assert periods.mtd == (date(2026, 10, 1), date(2026, 10, 12))
    assert periods.last_mtd == (date(2026, 9, 1), date(2026, 9, 12))


@pytest.mark.parametrize(
    ("latest", "last_mtd_end"),
    [
        (date(2026, 3, 31), date(2026, 2, 28)),  # capped at the shorter month's end
        (date(2028, 3, 30), date(2028, 2, 29)),  # leap year
        (date(2026, 1, 15), date(2025, 12, 15)),  # across the year
    ],
)
def test_last_month_to_date_is_capped_at_month_end(latest: date, last_mtd_end: date) -> None:
    periods = facts.report_periods(latest, None)

    assert periods.last_mtd[1] == last_mtd_end
    assert periods.last_mtd[0] == last_mtd_end.replace(day=1)
    assert "prev_day" not in periods.ranges()


@pytest.mark.parametrize(
    ("period", "label"),
    [
        ((date(2026, 10, 12), date(2026, 10, 12)), "12 Oct 2026"),
        ((date(2026, 10, 1), date(2026, 10, 12)), "01 to 12 Oct 2026"),
        ((date(2026, 9, 25), date(2026, 10, 2)), "25 Sep 2026 to 02 Oct 2026"),
    ],
)
def test_range_labels(period: tuple[date, date], label: str) -> None:
    assert facts.range_label(period) == label


# --- DAX ----------------------------------------------------------------------------------------


def test_latest_day_query_is_valid_read_only_dax_bounded_by_today() -> None:
    dax = facts.latest_dates_dax(DATE, SALES.expression, date(2026, 10, 13))

    validate_dax(dax, SCHEMA)  # same validator as every chat query
    assert "'Sales'[InvoiceDate] < DATE(2026, 10, 14)" in dax  # no future dates
    assert "NOT ISBLANK(CALCULATE([Total Net Sales]))" in dax


def test_kpi_query_uses_whole_days_and_one_value_per_kpi_and_period() -> None:
    kpis = [
        SALES,
        ReportKpi(
            "k2", "Lines", None, Aggregation(column="Sales[SalesKey]", function="count"), None
        ),
    ]
    periods = facts.report_periods(date(2026, 10, 12), date(2026, 10, 11))

    dax = facts.kpi_values_dax(kpis, DATE, periods)

    validate_dax(dax, SCHEMA)
    assert dax.count("CALCULATE(") == 2 * 5
    assert "\"k2|week_ago\", CALCULATE(COUNT('Sales'[SalesKey])" in dax
    # A date-time column keeps the rows later in the day: < next midnight, not <= midnight.
    day = "'Sales'[InvoiceDate] >= DATE(2026, 10, 12) && 'Sales'[InvoiceDate] < DATE(2026, 10, 13)"
    assert day in dax


def test_targets_are_queried_for_the_day_and_the_month() -> None:
    kpi = ReportKpi("k1", "Net sales", "Sales[Total Net Sales]", None, "Sales[Total Net Sales]")

    dax = facts.kpi_values_dax([kpi], DATE, facts.report_periods(date(2026, 10, 12), None))

    assert '"k1|target_day"' in dax and '"k1|target_mtd"' in dax
    assert '"k1|prev_day"' not in dax  # no earlier day with data


def test_change_steps_can_use_whole_days_while_chat_keeps_its_bounds() -> None:
    change = ChangeAnalysis(
        model_id="sales-ds",
        label="Net sales",
        measure="Sales[Total Net Sales]",
        baseline=TimeRange(
            column=DATE, period="explicit", start=date(2026, 10, 5), end=date(2026, 10, 5)
        ),
        current=TimeRange(
            column=DATE, period="explicit", start=date(2026, 10, 12), end=date(2026, 10, 12)
        ),
        drivers=["Product[LOB]"],
    )
    periods = ChangePeriods((date(2026, 10, 5),) * 2, (date(2026, 10, 12),) * 2)

    report = build_steps(change, periods, max_steps=2, whole_days=True)
    chat = build_steps(change, periods, max_steps=2)

    assert "'Sales'[InvoiceDate] < DATE(2026, 10, 13)" in (report[0].custom_dax or "")
    assert "'Sales'[InvoiceDate] <= DATE(2026, 10, 12)" in (chat[0].custom_dax or "")
    assert report[1].group_by == ["Product[LOB]"]


# --- figures ------------------------------------------------------------------------------------


def test_kpi_rows_compute_changes_and_leave_missing_values_out() -> None:
    target = ReportKpi("k2", "Orders", "Sales[Orders]", None, "Sales[Order Target]")
    periods = facts.report_periods(date(2026, 10, 12), date(2026, 10, 11))
    row = {
        "[k1|day]": 1200.0,
        "[k1|prev_day]": 1000.0,
        "[k1|week_ago]": 0,  # a zero baseline has no percent change
        "[k1|mtd]": "15,000.505",
        "[k1|last_mtd]": None,
        "[k2|day]": 50,
        "[k2|target_day]": 40,
    }

    rows = facts.kpi_rows([SALES, target], row, periods)

    sales = {c.baseline: c for c in rows[0].comparisons}
    assert rows[0].value == 1200.0
    assert (sales["prev_day"].change, sales["prev_day"].percent_change) == (200.0, 20.0)
    assert (sales["week_ago"].change, sales["week_ago"].percent_change) == (1200.0, None)
    assert sales["mtd"].current_value == 15000.5  # rounded to 2 decimals (15,000.505 -> .5)
    assert sales["mtd"].change is None  # last month missing
    assert sales["prev_day"].label == "vs 11 Oct 2026"
    assert sales["prev_day"].fact_id == "k1.prev_day"
    orders = {c.baseline: c for c in rows[1].comparisons}
    assert (orders["target_day"].change, orders["target_day"].percent_change) == (10.0, 25.0)
    assert "target_mtd" not in orders  # neither value known


def test_latest_dates_are_parsed_from_text_or_date_values() -> None:
    assert facts.parse_latest_dates({"[Latest date]": "2026-10-12", "[Previous date]": ""}) == (
        date(2026, 10, 12),
        None,
    )
    assert facts.parse_latest_dates(
        {"[Latest date]": datetime(2026, 10, 12, 18, 30), "[Previous date]": date(2026, 10, 9)}
    ) == (date(2026, 10, 12), date(2026, 10, 9))
    assert facts.parse_latest_dates({}) == (None, None)


def test_numbers_in_skips_flags_and_non_finite_values() -> None:
    assert facts.numbers_in({"a": [1, 2.5, True, float("nan")], "b": {"c": "3"}}) == [1.0, 2.5]


# --- refresh time -------------------------------------------------------------------------------


def rest(handler: Any) -> PowerBiRestGateway:
    return PowerBiRestGateway(
        Settings(_env_file=None), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_last_refresh_is_the_newest_completed_refresh() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "value": [
                    {"status": "Unknown", "endTime": None},  # still running
                    {"status": "Failed", "endTime": "2026-10-12T05:00:00Z"},
                    {"status": "Completed", "endTime": "2026-10-12T00:40:10.5Z"},
                ]
            },
        )

    refreshed = await rest(handler).last_refresh("tok", "ds-1")

    assert refreshed == datetime(2026, 10, 12, 0, 40, 10, 500000, tzinfo=UTC)
    assert seen[0].url.path == "/v1.0/myorg/datasets/ds-1/refreshes"
    assert seen[0].headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_a_refused_refresh_lookup_is_unsupported_not_access_denied(status: int) -> None:
    with pytest.raises(CapabilityNotSupportedError):
        await rest(lambda _: httpx.Response(status)).last_refresh("tok", "ds-1")


async def test_refresh_lookup_outage_is_unavailable() -> None:
    with pytest.raises(GatewayUnavailableError):
        await rest(lambda _: httpx.Response(500)).last_refresh("tok", "ds-1")


async def test_no_completed_refresh_means_unknown() -> None:
    handler = lambda _: httpx.Response(200, json={"value": []})  # noqa: E731

    assert await rest(handler).last_refresh("tok", "ds-1") is None


async def test_service_refresh_lookup_never_revokes_access() -> None:
    class RefusingRest(ScriptedGateway):
        async def last_refresh(self, user_token: str, dataset_id: str) -> datetime | None:
            raise CapabilityNotSupportedError("refreshes HTTP 403")

    access = Access(still_allowed=False)
    svc, _ = service(
        RefusingRest("rest", [], capabilities=frozenset({GatewayCapability.REFRESH_INFO})),
        access=access,
    )

    with pytest.raises(Exception) as caught:
        await svc.last_refresh(authz(), model_id="sales-ds")

    assert getattr(caught.value, "status_code", None) == 503
    assert access.revoked == []


async def test_dev_gateway_reports_yesterday_as_the_latest_day() -> None:
    gateway = DevSyntheticGateway(
        Settings(_env_file=None, environment="test"), today=lambda: date(2026, 10, 13)
    )
    dax = facts.latest_dates_dax(DATE, SALES.expression, date(2026, 10, 13))

    result = await gateway.execute_dax("", "sales-ds", dax, max_rows=1, user_key="u")

    assert facts.parse_latest_dates(result.rows[0]) == (date(2026, 10, 12), date(2026, 10, 11))


# --- report quality (ADR 0015 amendment 1) ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (147615304.7, "14,76,15,304.70"),
        (1234567, "12,34,567"),
        (100000, "1,00,000"),
        (999, "999"),
        (-194114359.36, "-19,41,14,359.36"),
        (12345.678, "12,345.68"),
        (None, "n/a"),
    ],
)
def test_amounts_use_the_indian_grouping(value: float | None, text: str) -> None:
    assert format_indian(value) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (147615304.7, "14.76 Cr"),
        (1476153, "14.76 lakh"),
        (53200.4, "53,200.40"),
        (-2.5e7, "-2.50 Cr"),
    ],
)
def test_compact_amounts_use_lakh_and_crore(value: float, text: str) -> None:
    assert compact_indian(value) == text


def test_changes_are_worded_without_double_negatives() -> None:
    assert change_words(-194114359.36, -56.8) == "down 19,41,14,359.36 (56.80%)"
    assert change_words(5, 2) == "up 5 (2.00%)"
    assert change_words(0, 0) == "no change"
    assert change_words(None, None) == "n/a"


def test_formatted_figures_pass_the_grounding_check() -> None:
    allowed = [147615304.7, 56.8]

    assert not ungrounded_numbers("Sales were 14,76,15,304.70, or 14.76 Cr, down 56.80%.", allowed)
    assert ungrounded_numbers("Sales were 15.20 Cr.", allowed) == ["15.20 Cr"]


def test_llm_text_is_cleaned_of_unsupported_characters_and_ids() -> None:
    raw = (
        "Target fell 2.5% day\u2011over\u2011day (k2.prev_day) and MTD rose (k2.mtd, k4.mtd) . "
        "See [f1] \u2014 \u201cquoted\u201d \u20b95\u2026"
    )

    cleaned = clean_text(raw)

    assert cleaned == 'Target fell 2.5% day-over-day and MTD rose. See - "quoted" Rs 5...'
    assert all(ord(ch) < 256 for ch in cleaned)  # all drawable in the PDF font


@pytest.mark.parametrize(
    ("day", "earlier"),
    [
        (date(2026, 10, 12), date(2026, 9, 12)),
        (date(2026, 3, 31), date(2026, 2, 28)),
        (date(2028, 3, 30), date(2028, 2, 29)),
        (date(2026, 1, 15), date(2025, 12, 15)),
    ],
)
def test_same_day_a_month_earlier_is_capped_at_month_end(day: date, earlier: date) -> None:
    assert facts.shift_month(day, -1) == earlier


def test_trend_query_is_valid_dax_with_two_values_per_day() -> None:
    dax = facts.trend_dax(SALES, DATE, date(2026, 10, 12), 30)

    validate_dax(dax, SCHEMA)
    assert dax.count("CALCULATE(") == 60
    assert '"t|2026-09-13"' in dax and '"t|2026-10-12"' in dax  # 30 days up to the latest
    assert '"t|2026-09-12"' not in dax
    # The value labelled 12 Oct one month earlier is 12 Sep.
    assert (
        "\"e|2026-10-12\", CALCULATE([Total Net Sales], FILTER(ALL('Sales'[InvoiceDate]), "
        "'Sales'[InvoiceDate] >= DATE(2026, 9, 12)" in dax
    )


def test_trend_points_are_read_per_day() -> None:
    row = {"[t|2026-10-12]": 10.0, "[e|2026-10-12]": "8", "[t|2026-10-11]": None}

    trend = facts.parse_trend(SALES, row, date(2026, 10, 12), 2)

    assert [(p.day.day, p.value, p.earlier_value) for p in trend.days] == [
        (11, None, None),
        (12, 10.0, 8.0),
    ]
    assert trend.kpi_label == "Net sales" and trend.higher_is_better


def test_inactive_query_compares_the_lookback_window_with_the_latest_day() -> None:
    dax = facts.inactive_dax([SALES], "Product[LOB]", DATE, date(2026, 10, 12), 7)

    validate_dax(dax, SCHEMA)
    assert "'Product'[LOB]" in dax
    before = (
        "'Sales'[InvoiceDate] >= DATE(2026, 10, 5) && 'Sales'[InvoiceDate] < DATE(2026, 10, 12)"
    )
    assert before in dax  # 5 to 11 Oct
    assert (
        "'Sales'[InvoiceDate] >= DATE(2026, 10, 12) && 'Sales'[InvoiceDate] < DATE(2026, 10, 13)"
        in dax
    )


def test_items_that_went_silent_are_found_largest_first() -> None:
    rows: list[dict[str, Any]] = [
        {"Product[LOB]": "SILVER", "[k1|before]": 500, "[k1|day]": None},
        {"Product[LOB]": "PLATINUM", "[k1|before]": 900, "[k1|day]": 0},
        {"Product[LOB]": "GOLD", "[k1|before]": 900, "[k1|day]": 100},  # still active
        {"Product[LOB]": "NEW", "[k1|before]": None, "[k1|day]": 50},  # new, not silent
        {"Product[LOB]": "RETURNED", "[k1|before]": -20, "[k1|day]": None},  # not a positive value
    ]

    found = facts.inactive_items(
        [SALES],
        rows,
        ["Product[LOB]", "[k1|before]", "[k1|day]"],
        by="LOB",
        lookback=7,
        truncated=False,
        top=1,
    )

    assert len(found) == 1
    assert [(i.item, i.baseline_value) for i in found[0].items] == [("PLATINUM", 900.0)]
    assert (found[0].total, found[0].by, found[0].kpi_label) == (2, "LOB", "Net sales")


def test_kpi_rows_carry_direction_and_target_achievement() -> None:
    kpi = ReportKpi(
        "k1", "Returns", "Sales[Total Net Sales]", None, "Sales[Total Net Sales]", False
    )
    periods = facts.report_periods(date(2026, 10, 12), None)
    row = {"[k1|day]": 50, "[k1|target_day]": 40, "[k1|mtd]": 300, "[k1|target_mtd]": 0}

    (kpi_row,) = facts.kpi_rows([kpi], row, periods)

    assert kpi_row.higher_is_better is False
    assert kpi_row.achievement_day == 125.0
    assert kpi_row.achievement_mtd is None  # no target to divide by


def test_every_report_prompt_starts_with_the_mis_persona() -> None:
    for name in ("report_kpis", "report_focus", "report_section", "report_summary"):
        prompt = load_prompt(name)
        assert prompt.startswith("You are a senior MIS executive at a jewellery brand"), name
        assert "TASK:" in prompt
