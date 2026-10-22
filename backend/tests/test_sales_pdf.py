"""The /sales-summary PDF (ADR 0016 §7, design §9): coloured summary bullets with ▲ / ▼ marks, the
four tables per data source with coloured changes, findings, action points and drill-downs, the
combined action list, and escaping of everything that comes from the LLM or the data."""

import re
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.reports.models import Trend, TrendPoint
from app.reports.sales.content import (
    Bullet,
    ModelUsed,
    SalesAction,
    SalesReportContent,
    SalesSection,
)
from app.reports.sales.pdf import _rich, render_sales_pdf
from app.reports.sales.tables import Cell, LensTable, Table

HOSTILE = "<a href='javascript:alert(1)'>click</a> <font size=90>big</font> & </para>"


def action(title: str = "Move GOLD stock to Store B", **fields: Any) -> SalesAction:
    values: dict[str, Any] = {
        "priority": "high",
        "title": title,
        "where": "Store B",
        "owner": "Merchandising",
        "urgency": "today",
        "reason": "GOLD fell -20.00% against last year.",
        "evidence": ["lob.1.r1.m1|mtd"],
        "model_name": "Sales",
        "lens": "lob_growth",
    }
    return SalesAction(**{**values, **fields})


def lob_table(findings: str = "- GOLD fell -20.00% against last year.") -> LensTable:
    main = Table(
        title="LOB-wise growth and degrowth against last year",
        columns=["LOB", "MTD", "MTD vs LY", "Status"],
        rows=[
            [
                Cell(text="GOLD"),
                Cell(text="12,34,567.89", fact="lob.1.r1.m1|mtd"),
                Cell(text="-20.00%", tone="bad", fact="lob.1.r1.m1|mtd"),
                Cell(text="Degrowth", tone="bad", fact="lob.1.r1.m1|mtd"),
            ],
            [
                Cell(text="SILVER"),
                Cell(text="500", fact="lob.1.r2.m1|mtd"),
                Cell(text="+25.00%", tone="good", fact="lob.1.r2.m1|mtd"),
                Cell(text="Growth", tone="good", fact="lob.1.r2.m1|mtd"),
            ],
        ],
    )
    drill = Table(
        title="Stores behind GOLD's fall",
        columns=["Store", "MTD", "MTD vs LY"],
        rows=[[Cell(text="Store B"), Cell(text="100"), Cell(text="-50.00%", tone="bad")]],
    )
    return LensTable(
        lens="lob_growth",
        title="LOB-wise growth and degrowth",
        main=main,
        drilldowns=[drill],
        findings=findings,
        suggestions=[action()],
        cut_short=True,
        queries_used=8,
        notes=["Not found in the data of Stock: GOLD."],
    )


def wide_table() -> LensTable:
    months = [f"{m} 2026 vs LY" for m in ("Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct")]
    columns = ["Category", "MTD", "MTD vs LY", "FYTD vs LY", *months]
    row = [Cell(text="Rings"), *(Cell(text="-1.00%", tone="bad") for _ in columns[1:])]
    return LensTable(
        lens="subcategory_loss",
        title="Sub-category loss by store and month",
        main=Table(title="t", columns=columns, rows=[row]),
    )


def section(name: str = "Sales", tables: list[LensTable] | None = None) -> SalesSection:
    trend = Trend(
        kpi_label="Total Net Sales",
        days=[
            TrendPoint(day=date(2026, 10, d), value=100.0 + d, earlier_value=90.0)
            for d in range(1, 13)
        ],
    )
    return SalesSection(
        dataset_id="sales-ds",
        name=name,
        status="ok",
        data_date=date(2026, 10, 12),
        tables=tables if tables is not None else [lob_table(), wide_table()],
        trend=trend,
    )


def content(
    *sections: SalesSection, bullets: list[Bullet] | None = None, **fields: Any
) -> SalesReportContent:
    return SalesReportContent(
        report_id=uuid.uuid4(),
        generated_at=datetime(2026, 10, 13, 3, 5, tzinfo=UTC),
        timezone="Asia/Kolkata",
        user_name="User A",
        bullets=bullets
        if bullets is not None
        else [
            Bullet(text="GOLD fell -20.00% against last year.", tone="bad", emphasis=["GOLD"]),
            Bullet(text="SILVER rose +25.00%.", tone="good"),
            Bullet(text="Stock turn was 1 this year.", tone="neutral"),
        ],
        **{
            "models_used": [ModelUsed(dataset_id="sales-ds", name="Sales", lenses=["lob_growth"])],
            "sales_sections": list(sections) or [section()],
            "actions": [action(), action("Watch SILVER", priority="low", urgency="this_month")],
            "notes": ["Development data, not real figures."],
            **fields,
        },
    )


def render(report: SalesReportContent) -> bytes:
    return render_sales_pdf(report, company_name="Acme Jewels", compress=False)


def test_the_report_renders_with_every_part() -> None:
    pdf = render(content())

    assert pdf.startswith(b"%PDF-")
    for text in (
        b"Daily sales MIS summary",
        b"Prepared for User A",
        b"13 Oct 2026, 08:35",  # in the report's time zone
        b"Data sources used: Sales \\(LOB-wise growth and degrowth\\).",
        b"MIS summary",
        b"Figures up to 12 Oct 2026",
        b"12,34,567.89",
        b"Stores behind GOLD's fall",
        b"Not found in the data of Stock: GOLD.",
        b"cut short",
        b"Daily trend: Total Net Sales",
        b"Action points",
        b"High priority \\(1\\)",
        b"Low priority \\(1\\)",
        b"Table: LOB-wise growth and degrowth",
        b"Where: Store B",
        b"GOLD, MTD: 12,34,567.89, MTD vs LY: -20.00%, Status: Degrowth",  # behind an action
        b"Development data, not real figures.",
        b"Page 1 of",
    ):
        assert text in pdf, text


def test_bullets_carry_a_mark_and_a_colour_for_their_news() -> None:
    pdf = render(content())

    assert b"/ZapfDingbats" in pdf
    for mark in (b"(s) Tj", b"(t) Tj", b"(l) Tj"):  # ▲ good, ▼ bad, ● neutral
        assert mark in pdf, mark
    assert b".705882 .137255 .094118 rg" in pdf  # #b42318 (needs attention)
    assert b".101961 .498039 .215686 rg" in pdf  # #1a7f37 (better)


def test_key_phrases_are_bold_only_when_they_are_in_the_text() -> None:
    assert _rich("GOLD fell.", ["GOLD", "SILVER"]) == "<b>GOLD</b> fell."
    assert _rich("A & B fell.", ["A & B"]) == "<b>A &amp; B</b> fell."  # escaped first


@pytest.mark.scenario(12)
def test_text_from_the_llm_or_the_data_cannot_add_links_or_markup() -> None:
    hostile = lob_table(findings=f"- {HOSTILE}")
    report = content(
        section(name=f"Sales {HOSTILE}", tables=[hostile]),
        bullets=[Bullet(text=HOSTILE, tone="bad", emphasis=[HOSTILE])],
    )

    pdf = render(report)

    assert b"/URI" not in pdf and b"/JavaScript" not in pdf and b"/Annot" not in pdf
    assert b"/F1 90" not in pdf


def test_data_sources_without_figures_and_failed_tables_say_why() -> None:
    failed = LensTable(
        lens="stock_turn",
        title="Stock turn",
        status="failed",
        message="Couldn't load Sales data right now.",
    )
    report = content(
        section(tables=[lob_table(), failed]),
        SalesSection(
            dataset_id="hr",
            name="Retail East",
            status="no_data",
            message="No data was found up to today.",
        ),
    )

    pdf = render(report)

    for text in (
        b"Couldn't load Sales data right now.",
        b"Data sources without figures today",
        b"No data was found up to today.",
    ):
        assert text in pdf, text


def test_a_report_without_bullets_or_actions_still_renders() -> None:
    pdf = render(content(bullets=[], actions=[]))

    assert b"No summary could be written today" in pdf and b"No action points for today." in pdf


def test_many_data_sources_span_numbered_pages() -> None:
    pdf = render(content(*(section(name=f"Sales {n}") for n in range(6))))

    pages = len(re.findall(rb"/Type /Page\b", pdf))
    assert pages >= 6 and f"Page {pages} of {pages}".encode() in pdf


def test_the_same_report_renders_the_same_bytes() -> None:
    report = content()
    assert render(report) == render(report)
