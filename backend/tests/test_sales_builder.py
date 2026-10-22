"""The /sales-summary analysis end to end (ADR 0016, design §1-§6): discovery, the latest day, each
table's first query and investigation loop, the tables, grounded findings and action points, trend
and "no activity now".

Real database, authorization and Executor; dev_synthetic data (latest day 12 Oct 2026); an LLM
that plays each role from a script. user-a may use Sales and HR (not Finance).
"""

import json
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text

from app.auth.token_broker import UnavailableTokenBroker
from app.core.errors import AppError, ErrorCode
from app.llm.base import ChatMessage, LLMProvider, LLMResponse
from app.powerbi.service import PowerBIService
from app.reports.builder import ReportBuildError, ReportProgress
from app.reports.sales.builder import NO_SALES_MODELS, SalesBuild, SalesReportBuilder
from app.reports.sales.content import SalesReportContent, parse_report
from app.reports.sales.tables import LensTable
from tests.authz_helpers import request_ctx
from tests.test_agent_pipeline import SETTINGS, Env, build_env
from tests.test_report_builder import TODAY, start_report
from tests.test_sales_loop import Gateway

pytestmark = pytest.mark.integration

SALES = {
    "dataset_id": "sales-ds",
    "is_sales": True,
    "lenses": ["lob_growth", "subcategory_loss", "price_range"],
    "date_column": "Sales[InvoiceDate]",
    "columns": {
        "lob": "Product[LOB]",
        "subcategory": "Product[Category]",
        "price": "Customer[CreditLimit]",
    },
    "measures": {"sales_value": "Sales[Total Net Sales]"},
}
HR = {"dataset_id": "hr-ds", "is_sales": False}
DRILL = {
    "action": "query",
    "step": {
        "model_id": "sales-ds",
        "measures": ["Sales[Total Net Sales]"],
        "group_by": ["Product[Category]"],
        "filters": [{"column": "Product[LOB]", "op": "=", "values": ["DEV-A"]}],
        "periods": ["mtd", "mtd_ly"],
        "why": "Categories behind DEV-A",
    },
}
BANDS = {
    "action": "query",
    "step": {
        "model_id": "sales-ds",
        "measures": ["Sales[Total Net Sales]"],
        "band_column": "Customer[CreditLimit]",
        "bands": [{"label": "Under 50k", "max": 50000}, {"label": "50k+", "min": 50000}],
        "periods": ["mtd", "mtd_ly", "fytd", "fytd_ly"],
        "why": "Sales by price range",
    },
}
DONE = {"action": "done", "note": "Enough."}
Handler = Callable[[Sequence[ChatMessage]], Any]


def facts_of(messages: Sequence[ChatMessage]) -> Any:
    block = messages[1].content.split("<untrusted_data>\n", 1)[1].split("\n</untrusted_data>")[0]
    return json.loads(block)


def lens_of(messages: Sequence[ChatMessage]) -> str:
    for marker, lens in (
        ("LOB-WISE GROWTH", "lob_growth"),
        ("SUB-CATEGORY LOSS", "subcategory_loss"),
        ("STOCK TURN.", "stock_turn"),
        ("SALES BY PRICE RANGE", "price_range"),
    ):
        if marker in messages[0].content:
            return lens
    return facts_of(messages)["table"]


def grounded_findings(messages: Sequence[ChatMessage]) -> Any:
    """Cites a real figure as given, plus action points the server must drop."""
    figures = [
        f for q in facts_of(messages)["queries"] for r in q.get("results", []) for f in r["figures"]
    ]
    first = next((f for f in figures if "last_year" in f), None)
    if first is None:  # e.g. a price table whose bands were never set
        return {"findings": "- No comparison with last year here.", "suggestions": []}
    return {
        "findings": f"- {first['measure']} was {first['value']} against {first['last_year']}.",
        "suggestions": [
            {
                "priority": "high",
                "title": "Review the weakest line",
                "where": "DEV-A",
                "owner": "Merchandising",
                "urgency": "today",
                "reason": f"Sales went {first['change']}.",
                "evidence": [first["id"]],
            },
            {"priority": "high", "title": "Made-up id", "reason": "x", "evidence": ["lob.9.r1.m1"]},
            {
                "priority": "medium",
                "title": "Invented figure",
                "reason": "Sales fell by 98,765.43.",
                "evidence": [first["id"]],
            },
        ],
    }


def grounded_bullets(messages: Sequence[ChatMessage]) -> Any:
    """A bullet on a real fall and one on a real rise (as shown), plus ones the server drops."""
    cells = [
        cell
        for source in facts_of(messages)
        for table in source["tables"]
        for row in table.get("rows", [])
        for cell in row
        if isinstance(cell, dict) and "vs LY" not in cell["text"]
    ]
    fall = next(c for c in cells if c["text"].startswith("-"))
    rise = next(c for c in cells if c["text"].startswith("+"))
    return {
        "bullets": [
            {
                "text": f"Sales fell {fall['text']} against last year.",
                "evidence": [fall["id"]],
                "emphasis": [fall["text"], "not in the text"],
            },
            {"text": f"Sales rose {rise['text']}.", "evidence": [rise["id"]]},
            {"text": "Sales fell 77.77% overall.", "evidence": [fall["id"]]},
            {"text": "A made-up fact.", "evidence": ["lob.9.r9.m1|mtd"]},
            {"text": "No evidence at all.", "evidence": []},
        ]
    }


class SalesLLM(LLMProvider):
    """Answers by role: discovery, each table's loop turns (in order; then "done"), findings,
    the summary bullets."""

    name = "scripted"
    model = "scripted"

    def __init__(
        self,
        *,
        discovery: Any = None,
        turns: dict[str, list[Any]] | None = None,
        findings: Handler = grounded_findings,
        bullets: Handler = grounded_bullets,
    ) -> None:
        self.discovery = discovery or {"models": [SALES, HR]}
        self.tables = (
            discovery
            if isinstance(discovery, AppError)
            else {
                "models": [
                    {"dataset_id": "sales-ds", "is_sales": True, "tables": ["Sales", "Product"]},
                    {"dataset_id": "hr-ds", "is_sales": False},
                ]
            }
        )
        self.turns = turns or {
            "lob_growth": [DRILL, DONE],
            "subcategory_loss": [DONE],
            "price_range": [BANDS, DONE],
        }
        self.findings = findings
        self.bullets = bullets
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def complete(
        self, messages: Sequence[ChatMessage], *, json_mode: bool = False
    ) -> LLMResponse:
        system = messages[0].content
        if "TASK: pick the tables" in system:
            kind, reply = "tables", self.tables
        elif "TASK: find which of the reader's data sources hold SALES" in system:
            kind, reply = "discovery", self.discovery
        elif "TASK: investigate one table" in system:
            kind = f"loop:{lens_of(messages)}"
            queue = self.turns.get(lens_of(messages), [])
            reply = queue.pop(0) if queue else DONE
            reply = reply(messages) if callable(reply) else reply
        elif "TASK: write the findings" in system:
            kind, reply = "findings", self.findings(messages)
        elif "TASK: write the summary bullets" in system:
            kind, reply = "bullets", self.bullets(messages)
        else:
            raise AssertionError("unexpected LLM call")
        self.calls.append((kind, list(messages)))
        if isinstance(reply, AppError):
            raise reply
        return LLMResponse(json.dumps(reply))

    def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:  # pragma: no cover
        raise NotImplementedError

    def count(self, kind: str) -> int:
        return sum(k == kind for k, _ in self.calls)


@pytest.fixture
async def env(committed_sessionmaker: Any) -> Env:
    return await build_env(committed_sessionmaker)


async def analyse(
    env: Env,
    llm: SalesLLM,
    *,
    gateway: Gateway | None = None,
    progress: list[ReportProgress] | None = None,
) -> tuple[SalesBuild, Any, Gateway]:
    gateway = gateway or Gateway()
    authz = await env.authz_service.build_context(request_ctx("user-a"))
    report_id = await start_report(env.sm, authz)
    builder = SalesReportBuilder(
        llm,
        env.user_schemas,
        PowerBIService(gateway, None, UnavailableTokenBroker(), env.authz_service, SETTINGS),
        env.sm,
        SETTINGS,
        today=lambda: TODAY,
    )

    async def record(event: ReportProgress) -> None:
        if progress is not None:
            progress.append(event)

    built = await builder.build_sections(authz, report_id, progress=record)
    return built, report_id, gateway


def table(built: SalesBuild, lens: str) -> LensTable:
    return next(t for t in built.sections[0].tables if t.lens == lens)


# --- the whole analysis -------------------------------------------------------------------------


async def test_sales_models_get_their_tables_investigated_and_explained(env: Env) -> None:
    progress: list[ReportProgress] = []

    built, report_id, gateway = await analyse(env, SalesLLM(), progress=progress)

    [sales] = built.sections
    assert (sales.name, sales.status, sales.data_date) == ("Sales", "ok", date(2026, 10, 12))
    assert [t.lens for t in sales.tables] == ["lob_growth", "subcategory_loss", "price_range"]
    assert [m.dataset_id for m in built.discovery.skipped] == ["hr-ds"]  # not sales: left out

    lob = table(built, "lob_growth")
    assert lob.main is not None and lob.main.columns[0] == "LOB"
    assert {r[0].text for r in lob.main.rows} == {"DEV-A", "DEV-B", "DEV-C"}
    assert [d.title for d in lob.drilldowns] == ["Categories behind DEV-A"]
    assert lob.queries_used == 2 and not lob.cut_short

    price = table(built, "price_range")  # the agent set the bands; they make the table
    assert price.main is not None
    assert [r[0].text for r in price.main.rows] == ["Under 50k", "50k+"]

    assert sales.trend is not None and len(sales.trend.days) == 30
    assert set(gateway.datasets) == {"sales-ds"}  # HR, not sales, is never queried
    async with env.sm() as session:
        recorded = await session.scalar(
            text("SELECT count(*) FROM query_executions WHERE report_id = :r"), {"r": report_id}
        )
    assert recorded == len(gateway.queries)
    stages = [(p.stage, p.model_name) for p in progress]
    assert stages[0] == ("discovering", None)
    assert ("lob_growth", "Sales") in stages and ("price_range", "Sales") in stages
    assert progress[-1].stage == "model_done" and (progress[-1].done, progress[-1].total) == (1, 1)


@pytest.mark.scenario(12)
async def test_findings_are_grounded_and_action_points_need_real_evidence(env: Env) -> None:
    built, _, _ = await analyse(env, SalesLLM())

    lob = table(built, "lob_growth")
    assert lob.findings.startswith("- Total Net Sales was ")
    assert [s.title for s in lob.suggestions] == ["Review the weakest line"]
    [kept] = lob.suggestions
    assert kept.model_name == "Sales" and kept.evidence[0].startswith("lob.1.r")


async def test_invented_figures_in_findings_fall_back_to_the_server_summary(env: Env) -> None:
    def invented(_: Sequence[ChatMessage]) -> Any:
        return {"findings": "- Sales fell 18.00% on 12 Oct 2026.", "suggestions": []}

    built, _, _ = await analyse(env, SalesLLM(findings=invented))

    findings = table(built, "subcategory_loss").findings
    assert "18.00%" not in findings
    assert findings.startswith("- Largest") or findings == ""


async def test_the_agent_being_unavailable_still_gives_tables(env: Env) -> None:
    down = AppError(ErrorCode.LLM_UNAVAILABLE, "down", 503)
    llm = SalesLLM(
        turns={lens: [down] for lens in ("lob_growth", "subcategory_loss", "price_range")},
        findings=lambda _: down,
    )

    built, _, _ = await analyse(env, llm)

    lob = table(built, "lob_growth")
    assert lob.main is not None and lob.cut_short and lob.suggestions == []
    assert table(built, "price_range").main is None  # no bands were set


# --- failures -----------------------------------------------------------------------------------


async def test_a_table_whose_first_query_fails_fails_alone(env: Env) -> None:
    gateway = Gateway(fail_on="|months_fy|")  # only the sub-category query asks for months

    built, _, _ = await analyse(env, SalesLLM(), gateway=gateway)

    sub = table(built, "subcategory_loss")
    assert (sub.status, sub.message) == ("failed", "Couldn't load Sales data right now.")
    assert table(built, "lob_growth").status == "ok"
    assert built.sections[0].status == "ok"


async def test_no_sales_data_source_is_an_error(env: Env) -> None:
    llm = SalesLLM(discovery={"models": [{**SALES, "is_sales": False, "lenses": []}, HR]})

    with pytest.raises(ReportBuildError) as raised:
        await analyse(env, llm)

    assert (raised.value.reason, raised.value.message) == ("no_sales_models", NO_SALES_MODELS)


async def test_an_unexpected_error_in_one_table_leaves_the_others(env: Env) -> None:
    def broken_for_prices(messages: Sequence[ChatMessage]) -> Any:
        if facts_of(messages)["table"] == "Sales by price range":
            raise RuntimeError("bug")
        return grounded_findings(messages)

    built, _, _ = await analyse(env, SalesLLM(findings=broken_for_prices))

    assert table(built, "price_range").status == "failed"
    assert table(built, "lob_growth").status == "ok" and built.sections[0].status == "ok"


# --- the report: summary bullets and the action list (16d) --------------------------------------


async def report(env: Env, llm: SalesLLM) -> SalesReportContent:
    authz = await env.authz_service.build_context(request_ctx("user-a"))
    report_id = await start_report(env.sm, authz)
    builder = SalesReportBuilder(
        llm,
        env.user_schemas,
        PowerBIService(Gateway(), None, UnavailableTokenBroker(), env.authz_service, SETTINGS),
        env.sm,
        SETTINGS,
        today=lambda: TODAY,
        clock=lambda: datetime(2026, 10, 13, 3, 0, tzinfo=UTC),
    )
    return await builder.build(authz, report_id, user_name="User A")


@pytest.mark.scenario(12)
async def test_bullets_are_coloured_by_the_server_and_must_cite_real_figures(env: Env) -> None:
    content = await report(env, SalesLLM())

    fell, rose = content.bullets  # the invented, unknown and unsupported bullets are dropped
    assert fell.text.startswith("Sales fell -") and fell.tone == "bad"
    assert fell.emphasis == [fell.text.split()[2]]  # a phrase not in the text is left out
    assert rose.text.startswith("Sales rose +") and rose.tone == "good"
    assert content.summary == f"{fell.text}\n{rose.text}"


async def test_the_report_lists_its_sources_actions_and_notes(env: Env) -> None:
    content = await report(env, SalesLLM())

    assert (content.schema_version, content.kind, content.user_name) == (3, "sales", "User A")
    assert [(m.name, m.lenses) for m in content.models_used] == [
        ("Sales", ["lob_growth", "subcategory_loss", "price_range"])
    ]
    assert [s.name for s in content.sales_sections] == ["Sales"]
    assert {a.lens for a in content.actions} >= {"lob_growth"}
    assert all(a.title == "Review the weakest line" for a in content.actions)
    assert content.suggestion_counts == {"high": len(content.actions), "medium": 0, "low": 0}
    assert content.notes == ["Development data, not real figures."]
    assert content.model_status()[0]["data_date"] == "2026-10-12"
    stored = content.model_dump_json()
    assert '"facts"' not in stored  # the figures behind the tables stay out of report.json
    assert isinstance(parse_report(stored), SalesReportContent)


async def test_without_the_agent_the_bullets_come_from_the_tables(env: Env) -> None:
    down = AppError(ErrorCode.LLM_UNAVAILABLE, "down", 503)

    content = await report(env, SalesLLM(bullets=lambda _: down))

    assert content.bullets and len(content.bullets) <= 8
    first = content.bullets[0]
    assert first.text.startswith("Sales, LOB-wise growth and degrowth: ")
    assert first.tone in ("good", "bad") and first.evidence


async def test_findings_written_by_the_agent_save_the_findings_call(env: Env) -> None:
    def done_with_findings(messages: Sequence[ChatMessage]) -> Any:
        block = (
            messages[-1].content.split("<untrusted_data>\n", 1)[1].split("\n</untrusted_data>")[0]
        )
        figure = json.loads(block)["queries_so_far"][0]["results"][0]["figures"][0]
        return {
            "action": "done",
            "findings": f"- {figure['measure']} was {figure['value']}.",
            "suggestions": [{"priority": "medium", "title": "Check it", "reason": "Watch it.",
                             "evidence": [figure["id"]]}],
        }  # fmt: skip

    llm = SalesLLM(turns={"lob_growth": [done_with_findings]})

    built, _, _ = await analyse(env, llm)

    lob = table(built, "lob_growth")
    assert lob.findings.startswith("- Total Net Sales was ")
    assert [s.title for s in lob.suggestions] == ["Check it"]
    # Only the two tables whose "done" carried no findings needed a findings call.
    assert llm.count("findings") == 2
