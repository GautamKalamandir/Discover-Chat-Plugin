"""The /summary report builder end to end (ADR 0015): real database, real authorization, fixture
schemas, the dev_synthetic gateway (dates fixed to 13 Oct 2026) and an LLM that answers each
report prompt with a test handler.

user-a may use Sales and HR (not Finance); user-b only Sales. HR has no date column.
"""

import json
import re
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.token_broker import UnavailableTokenBroker
from app.authz.models import AuthorizedContext
from app.core.errors import AppError, ErrorCode
from app.db.repositories.registry import RegistryRepository
from app.db.repositories.reports import ReportRepository
from app.db.repositories.users import UserRepository
from app.llm.base import ChatMessage, LLMProvider, LLMResponse
from app.powerbi.base import GatewayCapability, QueryResult
from app.powerbi.dev_synthetic import LATEST_DATE, PREVIOUS_DATE, DevSyntheticGateway
from app.powerbi.errors import DaxQueryError, PowerBITimeoutError
from app.powerbi.service import PowerBIService
from app.reports.builder import (
    DEV_DATA_NOTE,
    NO_DATA,
    NO_USABLE_FIELDS,
    ReportBuilder,
    ReportBuildError,
    ReportProgress,
)
from app.reports.models import ModelSection, SummaryReportContent
from app.reports.text import format_indian
from tests.authz_helpers import request_ctx
from tests.test_agent_pipeline import SETTINGS, Env, build_env

pytestmark = pytest.mark.integration

TODAY = date(2026, 10, 13)
SALES_PLAN = {
    "date_column": "Sales[InvoiceDate]",
    "kpis": [{"label": "Net sales", "measure": "Sales[Total Net Sales]"}],
    "breakdowns": ["Product[LOB]", "Product[Category]"],
    "assumptions": [],
}
NOTHING: dict[str, Any] = {"date_column": None, "kpis": [], "breakdowns": []}
Handler = Callable[[str, Sequence[ChatMessage]], Any]


def kind_of(messages: Sequence[ChatMessage]) -> str:
    system = messages[0].content
    for marker, kind in (
        ("TASK: choose what today's report shows", "kpis"),
        ("TASK: decide which further changes", "focus"),
        ("TASK: write one data source's section", "section"),
        ("TASK: write the executive summary", "summary"),
        ("DAX", "repair"),
    ):
        if marker in system:
            return kind
    raise AssertionError("unexpected prompt")


def model_of(messages: Sequence[ChatMessage]) -> str:
    match = re.search(r'<model id="([^"]+)"', messages[1].content)
    return match.group(1) if match else ""


def data(message: ChatMessage) -> Any:
    block = message.content.split("<untrusted_data>\n", 1)[1].split("\n</untrusted_data>", 1)[0]
    return json.loads(block)


class ReportLLM(LLMProvider):
    """Answers by prompt kind (models are built in parallel, so call order isn't fixed)."""

    name = "scripted"
    model = "scripted"

    def __init__(self, **handlers: Handler) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def complete(
        self, messages: Sequence[ChatMessage], *, json_mode: bool = False
    ) -> LLMResponse:
        kind = kind_of(messages)
        self.calls.append((kind, list(messages)))
        reply = self.handlers[kind](kind, messages)
        return LLMResponse(reply if isinstance(reply, str) else json.dumps(reply))

    def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:  # pragma: no cover
        raise NotImplementedError

    def count(self, kind: str) -> int:
        return sum(k == kind for k, _ in self.calls)


def kpis_by_model(**plans: Any) -> Handler:
    def handler(_: str, messages: Sequence[ChatMessage]) -> Any:
        plan = plans.get(model_of(messages), NOTHING)
        return plan(messages) if callable(plan) else plan

    return handler


def focus_prev_day(_: str, messages: Sequence[ChatMessage]) -> Any:
    # k1 against last week is already the performers analysis; ask for the previous day instead.
    return {"focus": [{"kpi": "k1", "baseline": "prev_day", "breakdowns": ["Product[LOB]"]}]}


def good_section(_: str, messages: Sequence[ChatMessage]) -> Any:
    """Cites real figures, plus three suggestions the server must drop."""
    facts = data(messages[1])
    week = next(c for c in facts["kpis"][0]["comparisons"] if c["id"] == "k1.week_ago")
    suggestions = [
        {
            "priority": "low",
            "title": "Keep watching net sales",
            "where": "",
            "owner": "MIS",
            "urgency": "this_week",
            "reason": f"Net sales went {week['change']} {week['compared_with']}.",
            "notes": "Check again tomorrow.",
            "evidence": ["k1.week_ago"],
        },
        {"priority": "high", "title": "Made-up id", "reason": "x", "evidence": ["k9.nope"]},
        {
            "priority": "high",
            "title": "Invented figure",
            "reason": "Sales fell by 123,456.78 overall.",
            "evidence": ["k1.week_ago"],
        },
        {"priority": "medium", "title": "No evidence", "reason": "Trust me.", "evidence": []},
    ]
    if facts.get("explained_changes"):
        change = facts["explained_changes"][0]
        suggestions.append(
            {
                "priority": "high",
                "title": "Review the biggest mover",
                "where": "DEV-A",
                "owner": "Store manager",
                "urgency": "today",
                "reason": f"Net sales went {change['change']}.",
                "notes": "",
                "evidence": ["f1", "performers"],
            }
        )
    return {
        "headline": f"Net sales on {facts['latest_day_with_data']}.",
        "narrative": "Figures are in the table.",
        "suggestions": suggestions,
    }


def plain_summary(_: str, __: Sequence[ChatMessage]) -> Any:
    return {"summary": "All good today."}


def llm(**overrides: Handler) -> ReportLLM:
    handlers: dict[str, Handler] = {
        "kpis": kpis_by_model(**{"sales-ds": SALES_PLAN}),
        "focus": focus_prev_day,
        "section": good_section,
        "summary": plain_summary,
    }
    return ReportLLM(**{**handlers, **overrides})


class ReportGateway(DevSyntheticGateway):
    def __init__(
        self,
        *,
        fail: frozenset[str] = frozenset(),
        no_data: bool = False,
        refreshed: datetime | None = None,
        inactive_rows: list[dict[str, Any]] | None = None,
        blank: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(SETTINGS, today=lambda: TODAY)
        self.fail = fail
        self.no_data = no_data
        self.refreshed = refreshed
        self.inactive_rows = inactive_rows
        self.blank = blank  # result columns returned empty, e.g. "[k2|day]"
        if refreshed is not None:
            self.capabilities = DevSyntheticGateway.capabilities | {GatewayCapability.REFRESH_INFO}
        self.queries: list[tuple[str, str]] = []

    async def execute_dax(
        self, user_token: str, dataset_id: str, dax: str, *, max_rows: int, user_key: str
    ) -> QueryResult:
        self.queries.append((dataset_id, dax))
        if dataset_id in self.fail:
            raise PowerBITimeoutError("slow")
        if self.no_data and LATEST_DATE in dax:
            row = {f"[{LATEST_DATE}]": "", f"[{PREVIOUS_DATE}]": ""}
            return QueryResult(list(row), [row], False, self.name)
        if self.inactive_rows is not None and "|before" in dax:
            rows = self.inactive_rows
            return QueryResult(list(rows[0]), rows, False, self.name)
        result = await super().execute_dax(
            user_token, dataset_id, dax, max_rows=max_rows, user_key=user_key
        )
        rows = [{k: (None if k in self.blank else v) for k, v in r.items()} for r in result.rows]
        return QueryResult(result.columns, rows, result.truncated, result.gateway)

    async def last_refresh(self, user_token: str, dataset_id: str) -> datetime | None:
        return self.refreshed


async def build(
    env: Env,
    model: ReportLLM,
    gateway: ReportGateway | None = None,
    oid: str = "user-a",
    progress: list[ReportProgress] | None = None,
) -> tuple[SummaryReportContent, uuid.UUID, ReportGateway]:
    gateway = gateway or ReportGateway()
    authz = await env.authz_service.build_context(request_ctx(oid))
    report_id = await start_report(env.sm, authz)
    builder = ReportBuilder(
        model,
        env.retriever,
        env.user_schemas,
        PowerBIService(gateway, None, UnavailableTokenBroker(), env.authz_service, SETTINGS),
        env.sm,
        SETTINGS,
        today=lambda: TODAY,
        clock=lambda: datetime(2026, 10, 13, 3, 0, tzinfo=UTC),
    )

    async def record(event: ReportProgress) -> None:
        if progress is not None:
            progress.append(event)

    content = await builder.build(authz, report_id, user_name="User A", progress=record)
    return content, report_id, gateway


async def start_report(sm: async_sessionmaker[AsyncSession], authz: AuthorizedContext) -> uuid.UUID:
    async with sm() as session, session.begin():
        user = await UserRepository(session).upsert_from_identity(authz.request.user)
        repo = ReportRepository(session, retention_days=7, build_timeout_seconds=600)
        report, _ = await repo.start_build(user, TODAY)
        return report.id


def section(content: SummaryReportContent, name: str) -> ModelSection:
    return next(s for s in content.sections if s.name == name)


@pytest.fixture
async def env(committed_sessionmaker: async_sessionmaker[AsyncSession]) -> Env:
    return await build_env(committed_sessionmaker)


# --- the whole report ---------------------------------------------------------------------------


@pytest.mark.scenario(1)
async def test_a_report_covers_every_allowed_model_with_grounded_figures(env: Env) -> None:
    progress: list[ReportProgress] = []

    content, report_id, gateway = await build(env, llm(), progress=progress)

    assert [(s.name, s.status) for s in content.sections] == [("HR", "no_data"), ("Sales", "ok")]
    assert section(content, "HR").message == NO_USABLE_FIELDS
    sales = section(content, "Sales")
    assert sales.data_date == date(2026, 10, 12)  # the latest day with data, not today
    assert [c.baseline for c in sales.kpis[0].comparisons] == ["prev_day", "week_ago", "mtd"]
    assert sales.headline == "Net sales on 12 Oct 2026."
    assert content.summary == "All good today."
    assert content.notes == [DEV_DATA_NOTE]
    assert content.user_name == "User A" and content.report_id == report_id
    # Only Sales is queried; Finance (not allowed) never is.
    assert {dataset for dataset, _ in gateway.queries} == {"sales-ds"}
    stages = [(p.stage, p.model_name, p.status) for p in progress]
    assert ("model_done", "Sales", "ok") in stages and ("model_done", "HR", "no_data") in stages
    assert stages[-1] == ("summarising", None, None)


@pytest.mark.scenario(12)
async def test_suggestions_without_valid_evidence_or_with_invented_numbers_are_dropped(
    env: Env,
) -> None:
    content, _, _ = await build(env, llm())

    sales = section(content, "Sales")
    assert [(s.priority, s.title) for s in sales.suggestions] == [
        ("high", "Review the biggest mover"),  # sorted high first
        ("low", "Keep watching net sales"),
    ]
    assert sales.suggestions[0].evidence == ["f1", "performers"]
    assert (sales.suggestions[0].where, sales.suggestions[0].owner) == ("DEV-A", "Store manager")
    assert sales.suggestions[0].urgency == "today"
    assert sales.suggestions[0].model_name == "Sales"
    assert content.suggestion_counts == {"high": 1, "medium": 0, "low": 1}


async def test_the_chosen_change_is_explained_with_the_change_analysis(env: Env) -> None:
    content, _, gateway = await build(env, llm())

    focus = section(content, "Sales").focus
    assert len(focus) == 1
    analysis = focus[0]
    assert (analysis.id, analysis.kpi_id, analysis.baseline) == ("f1", "k1", "prev_day")
    assert (analysis.baseline_period, analysis.current_period) == ("11 Oct 2026", "12 Oct 2026")
    assert analysis.breakdowns[0].by == "LOB"
    movers = analysis.breakdowns[0].biggest_decreases + analysis.breakdowns[0].biggest_increases
    assert {m.item for m in movers} <= {"DEV-A", "DEV-B", "DEV-C"} and movers
    grouped = [dax for _, dax in gateway.queries if "'Product'[LOB]" in dax]
    assert grouped and "< DATE(2026, 10, 13)" in grouped[0]  # whole days


async def test_report_queries_are_recorded_against_the_report(env: Env) -> None:
    _, report_id, gateway = await build(env, llm())

    async with env.sm() as session:
        rows = (
            await session.execute(
                text("SELECT report_id, session_id, status FROM query_executions")
            )
        ).all()
    # latest day, KPIs, trend, performers (total + 2 breakdowns), no activity (2 breakdowns),
    # the agent's change (total + 1 breakdown)
    assert len(rows) == len(gateway.queries) == 10
    assert all(r.report_id == report_id and r.session_id is None for r in rows)
    assert {r.status for r in rows} == {"succeeded"}


# --- KPI choice ---------------------------------------------------------------------------------


@pytest.mark.scenario(16)
async def test_admin_pins_are_always_included_and_invisible_pins_skipped(env: Env) -> None:
    async with env.sm() as session, session.begin():
        registry = RegistryRepository(session)
        sales = await registry.get_model_by_pbi_id("sales-ds")
        assert sales is not None
        await registry.add_kpi_pin(sales, measure="Sales[Total Net Sales]", label="Pinned sales")
        await registry.add_kpi_pin(sales, measure="Sales[Hidden Margin]", label="Secret")
    no_kpis = {**SALES_PLAN, "kpis": []}
    model = llm(kpis=kpis_by_model(**{"sales-ds": no_kpis}))

    content, _, _ = await build(env, model)

    assert [k.label for k in section(content, "Sales").kpis] == ["Pinned sales"]
    sales_prompt = next(m for k, m in model.calls if k == "kpis" and model_of(m) == "sales-ds")
    assert "Pinned sales: Sales[Total Net Sales]" in sales_prompt[1].content
    assert "Hidden Margin" not in sales_prompt[1].content


async def test_an_invalid_choice_is_replanned_once_then_invalid_parts_dropped(env: Env) -> None:
    first = {
        "date_column": "Sales[InvoiceDate]",
        "kpis": [{"label": "Bad", "measure": "Sales[Nope]"}],
        "breakdowns": ["Sales[InvoiceDate]"],
    }
    second = {**SALES_PLAN, "breakdowns": ["Product[LOB]", "Product[Missing]"]}
    replies = [first, second]
    model = llm(kpis=kpis_by_model(**{"sales-ds": lambda _: replies.pop(0)}), focus=focus_prev_day)

    content, _, _ = await build(env, model, oid="user-b")

    assert model.count("kpis") == 2
    feedback = next(m for k, m in model.calls if k == "kpis" and len(m) > 2)[-1].content
    assert "unknown measure Sales[Nope]" in feedback
    assert "breakdown Sales[InvoiceDate] is a date column" in feedback
    sales = section(content, "Sales")
    assert [k.label for k in sales.kpis] == ["Net sales"]
    assert sales.status == "ok"


@pytest.mark.scenario(7)
async def test_objects_of_models_the_user_cannot_use_are_rejected(env: Env) -> None:
    """The LLM can't add a model: KPIs from Finance (not allowed) aren't in the user's schema."""
    finance = {
        "date_column": "Sales[InvoiceDate]",
        "kpis": [{"label": "Budget", "measure": "GL[Budget Variance]"}],
        "breakdowns": ["GL[CostCentre]"],
    }
    model = llm(kpis=kpis_by_model(**{"sales-ds": finance}))

    content, _, gateway = await build(env, model)

    assert section(content, "Sales").status == "no_data"
    assert gateway.queries == []
    assert all("finance" not in model_of(m) for k, m in model.calls if k == "kpis")


# --- failures -----------------------------------------------------------------------------------


async def test_a_failing_model_gets_a_marked_section_and_the_rest_is_built(env: Env) -> None:
    hr_plan = {"date_column": "Sales[InvoiceDate]", "kpis": [], "breakdowns": []}
    model = llm(kpis=kpis_by_model(**{"sales-ds": SALES_PLAN, "hr-ds": hr_plan}))

    content, _, _ = await build(env, model, ReportGateway(fail=frozenset({"sales-ds"})))

    sales = section(content, "Sales")
    assert (sales.status, sales.message) == ("failed", "Couldn't load Sales data right now.")
    assert sales.kpis == [] and sales.suggestions == []
    assert section(content, "HR").status == "no_data"
    assert model.count("summary") == 0  # nothing to summarise: the template says what happened
    assert "Couldn't load Sales data right now." in content.summary


async def test_a_report_with_every_model_failing_is_an_error(env: Env) -> None:
    with pytest.raises(ReportBuildError) as caught:
        await build(env, llm(), ReportGateway(fail=frozenset({"sales-ds"})), oid="user-b")

    assert caught.value.reason == "all_models_failed"
    assert caught.value.code is ErrorCode.REPORT_FAILED


async def test_a_user_without_data_sources_gets_no_report(env: Env) -> None:
    model = llm()

    with pytest.raises(ReportBuildError) as caught:
        await build(env, model, oid="user-without-access")

    assert caught.value.reason == "no_models"
    assert model.calls == []


async def test_no_data_up_to_today_is_said_plainly(env: Env) -> None:
    content, _, gateway = await build(env, llm(), ReportGateway(no_data=True), oid="user-b")

    sales = section(content, "Sales")
    assert (sales.status, sales.message) == ("no_data", NO_DATA)
    assert len(gateway.queries) == 1  # nothing else is queried


async def test_ungrounded_text_falls_back_to_templates(env: Env) -> None:
    def invented(_: str, __: Sequence[ChatMessage]) -> Any:
        return {"headline": "Sales hit 999,999.99!", "narrative": "Up 87.65%.", "suggestions": []}

    model = llm(section=invented, summary=lambda *_: {"summary": "Total 4,242,424.42."})

    content, _, _ = await build(env, model, oid="user-b")

    sales = section(content, "Sales")
    assert sales.headline == "Sales: figures for 12 Oct 2026."
    assert sales.narrative is not None and sales.narrative.startswith("- Net sales: ")
    assert content.summary == "Sales: figures for 12 Oct 2026."


async def test_day_and_month_numbers_are_never_accepted_as_figures(env: Env) -> None:
    # The trend covers 30 days, so every number from 1 to 31 is a day shown in the report. A
    # made-up "18.00%" must still be rejected; numbers that are part of a date are fine.
    def dated(_: str, __: Sequence[ChatMessage]) -> Any:
        return {
            "headline": "Figures from 01 to 12 Oct 2026, compared with Oct 5.",
            "narrative": "Net sales fell 18.00% on 12/10.",
            "suggestions": [],
        }

    content, _, _ = await build(env, llm(section=dated), oid="user-b")

    sales = section(content, "Sales")
    assert sales.headline == "Figures from 01 to 12 Oct 2026, compared with Oct 5."
    assert sales.narrative is not None and sales.narrative.startswith("- Net sales: ")


async def test_the_llm_being_unavailable_still_gives_template_sections(env: Env) -> None:
    def down(_: str, __: Sequence[ChatMessage]) -> Any:
        raise AppError(ErrorCode.LLM_UNAVAILABLE, "down", 503)

    content, _, _ = await build(env, llm(focus=down, section=down, summary=down), oid="user-b")

    sales = section(content, "Sales")
    assert sales.status == "ok" and sales.focus == [] and sales.suggestions == []
    assert sales.headline == "Sales: figures for 12 Oct 2026."


async def test_the_refresh_time_is_shown_when_power_bi_provides_it(env: Env) -> None:
    refreshed = datetime(2026, 10, 13, 0, 40, tzinfo=UTC)

    content, _, _ = await build(env, llm(), ReportGateway(refreshed=refreshed), oid="user-b")
    without, _, _ = await build(env, llm(), oid="user-b")

    assert section(content, "Sales").refreshed_at == refreshed
    assert section(without, "Sales").refreshed_at is None


# --- report quality (ADR 0015 amendment 1) ------------------------------------------------------

TWO_KPIS = {
    **SALES_PLAN,
    "kpis": [
        {"label": "Net sales", "measure": "Sales[Total Net Sales]", "higher_is_better": True},
        {
            "label": "Invoices",
            "aggregation": {"column": "Sales[SalesKey]", "function": "count"},
            "higher_is_better": False,
        },
    ],
}


async def test_a_failing_report_query_is_not_rewritten_by_the_llm(env: Env) -> None:
    # The server built this DAX; the LLM repair loop is only for DAX the agent wrote (ADR 0016).
    class FailingTrend(ReportGateway):
        async def execute_dax(
            self, user_token: str, dataset_id: str, dax: str, *, max_rows: int, user_key: str
        ) -> QueryResult:
            if '"t|' in dax:
                raise DaxQueryError("Column 'X' cannot be found")
            return await super().execute_dax(
                user_token, dataset_id, dax, max_rows=max_rows, user_key=user_key
            )

    model = llm()
    content, _, _ = await build(env, model, FailingTrend(), oid="user-b")

    assert section(content, "Sales").status == "ok" and section(content, "Sales").trend is None
    assert model.count("repair") == 0


async def test_every_section_gets_a_trend_and_top_and_bottom_performers(env: Env) -> None:
    content, _, _ = await build(env, llm(), oid="user-b")

    sales = section(content, "Sales")
    assert sales.trend is not None
    assert len(sales.trend.days) == 30 and sales.trend.days[-1].day == date(2026, 10, 12)
    assert all(p.value is not None and p.earlier_value is not None for p in sales.trend.days)
    assert [b.by for b in sales.performers] == ["LOB", "Category"]
    assert sales.performers_period == "12 Oct 2026 vs 05 Oct 2026"
    movers = sales.performers[0].biggest_increases + sales.performers[0].biggest_decreases
    assert 0 < len(movers) <= 5


async def test_items_with_no_activity_on_the_latest_day_are_listed(env: Env) -> None:
    rows = [
        {"Product[LOB]": "SILVER", "[k1|before]": 5000.0, "[k1|day]": None},
        {"Product[LOB]": "GOLD", "[k1|before]": 9000.0, "[k1|day]": 1200.0},
    ]

    content, _, _ = await build(env, llm(), ReportGateway(inactive_rows=rows), oid="user-b")

    inactive = section(content, "Sales").inactive
    first = inactive[0]
    assert (first.by, first.kpi_label, [i.item for i in first.items]) == (
        "LOB",
        "Net sales",
        ["SILVER"],
    )
    assert first.lookback_days == 7


async def test_kpis_without_a_value_today_move_to_a_note(env: Env) -> None:
    model = llm(kpis=kpis_by_model(**{"sales-ds": TWO_KPIS}))

    content, _, _ = await build(
        env, model, ReportGateway(blank=frozenset({"[k2|day]"})), oid="user-b"
    )

    sales = section(content, "Sales")
    assert [k.label for k in sales.kpis] == ["Net sales"]
    assert sales.missing_kpis == ["Invoices"]


async def test_the_agent_gets_formatted_figures_and_the_direction_of_good_news(env: Env) -> None:
    model = llm(kpis=kpis_by_model(**{"sales-ds": TWO_KPIS}))

    content, _, _ = await build(env, model, oid="user-b")

    _, messages = next(c for c in model.calls if c[0] == "section")
    facts = data(messages[1])
    sales, invoices = facts["kpis"]
    value = section(content, "Sales").kpis[0].value
    assert sales["display"] == format_indian(value)
    assert sales["higher_is_better"] is True and invoices["higher_is_better"] is False
    week = next(c for c in invoices["comparisons"] if c["id"] == "k2.week_ago")
    assert week["change"].startswith(("up ", "down ")) and "-" not in week["change"]
    assert week["good_news"] is week["change"].startswith("down ")  # fewer is better here
    assert "trend" in facts and "performers" in facts
    assert str(value) not in messages[1].content  # only formatted figures reach the agent


async def test_text_is_cleaned_before_it_is_shown(env: Env) -> None:
    def messy(_: str, messages: Sequence[ChatMessage]) -> Any:
        return {
            "headline": "Sales day\u2011over\u2011day (k1.prev_day) were steady.",
            "narrative": "Steady \u2014 see k1.week_ago.",
            "suggestions": [
                {
                    "priority": "medium",
                    "title": "Review\u2011stock",
                    "where": "All stores",
                    "owner": "Merchandising",
                    "urgency": "this_week",
                    "reason": "Steady (k1.mtd).",
                    "evidence": ["k1.mtd"],
                }
            ],
        }

    content, _, _ = await build(env, llm(section=messy), oid="user-b")

    sales = section(content, "Sales")
    assert sales.headline == "Sales day-over-day were steady."
    assert sales.narrative == "Steady - see."
    assert (sales.suggestions[0].title, sales.suggestions[0].reason) == ("Review-stock", "Steady.")


async def test_the_overview_has_one_row_per_data_source(env: Env) -> None:
    content, _, _ = await build(env, llm())

    rows = {r.name: r for r in content.overview}
    assert set(rows) == {"HR", "Sales"}
    assert rows["HR"].status == "no_data" and rows["HR"].alert == NO_USABLE_FIELDS
    sales = rows["Sales"]
    assert (sales.status, sales.kpi_label, sales.data_date) == (
        "ok",
        "Net sales",
        date(2026, 10, 12),
    )
    assert sales.value == section(content, "Sales").kpis[0].value
    assert sales.good is not None and sales.change_percent is not None
    assert sales.alert == "Review the biggest mover"  # the top high-priority action


async def test_progress_counts_the_data_sources_done(env: Env) -> None:
    progress: list[ReportProgress] = []

    await build(env, llm(), progress=progress)

    done = [(p.model_name, p.done, p.total) for p in progress if p.stage == "model_done"]
    assert sorted(d or 0 for _, d, _ in done) == [1, 2] and {t for _, _, t in done} == {2}
    assert all(p.total == 2 for p in progress)
