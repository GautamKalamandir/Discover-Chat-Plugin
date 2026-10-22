"""The /sales-summary PDF (ADR 0016 §7, design §9), with the Phase 15 renderer's fonts, styles, page
footer, trend chart and "no activity now" table.

1. cover: company, the reader, generated time, the data sources used;
2. MIS summary: one bullet per line, green (better) or red (needs attention) with ▲ / ▼ / ●, so the
   meaning never rests on colour alone, and key phrases in bold;
3. per data source (a new page each, unless most of the page is free): its four tables, each with
   coloured changes, findings, action points and the agent's drill-downs; the daily trend and
   "no activity now";
4. the combined action list, High / Medium / Low;
5. data sources without figures.

Every string from the LLM or the data is escaped before it reaches a Paragraph (`_escape`).
"""

import io
from collections.abc import Iterator
from typing import Any
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    CondPageBreak,
    Flowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)
from reportlab.platypus import Table as PdfTable

from app.reports.facts import day_label
from app.reports.pdf import (
    BAD,
    CONTENT_WIDTH,
    GOOD,
    INK,
    MARGIN,
    MAX_PDF_BYTES,
    PANEL_BG,
    PRIORITY_COLOURS,
    STATUS_TEXT,
    STYLES,
    URGENCY_TEXT,
    ReportRenderError,
    _chart_block,
    _escape,
    _inactive,
    _local,
    _logo,
    _NumberedCanvas,
    _p,
    _table,
    _text,
    _trend_chart,
    _trend_legend,
)
from app.reports.sales.content import Bullet, SalesAction, SalesReportContent, SalesSection
from app.reports.sales.lenses import TITLES
from app.reports.sales.tables import Cell, LensTable, Table, Tone

SECTION_ROOM = 150 * mm
TONE_COLOURS: dict[Tone, colors.Color] = {"good": GOOD, "bad": BAD, "neutral": INK}
# Drawn in ZapfDingbats (built into every PDF reader; Helvetica has no triangles).
TONE_MARKS: dict[Tone, str] = {"good": "▲", "bad": "▼", "neutral": "●"}  # ▲ ▼ ●
WIDE_TABLE = 9  # columns from which a table uses the small font


def render_sales_pdf(
    content: SalesReportContent,
    *,
    company_name: str,
    logo_path: str | None = None,
    compress: bool = True,
) -> bytes:
    """The report as PDF bytes. `compress=False` keeps page text searchable (tests)."""
    zone = ZoneInfo(content.timezone)
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=20 * mm,
        title=f"Daily sales summary {_local(content.generated_at, zone):%d %b %Y}",
        author=company_name,
        subject="Daily sales MIS summary",
        creator="Discover Chat Bot",
        pageCompression=1 if compress else 0,
        invariant=1,
    )
    story: list[Flowable] = [*_cover(content, company_name, logo_path, zone), *_bullets(content)]
    for section in content.sales_sections:
        if section.status == "ok":
            story += [CondPageBreak(SECTION_ROOM), *_section(section, zone)]
    evidence = {
        fact: text
        for section in content.sales_sections
        for table in section.tables
        for fact, text in _evidence(table).items()
    }
    story += list(_actions(content.actions, evidence))
    story += list(_not_included(content.sales_sections))
    document.build(story, canvasmaker=_NumberedCanvas)
    pdf = buffer.getvalue()
    if len(pdf) > MAX_PDF_BYTES:
        raise ReportRenderError(f"report PDF is {len(pdf)} bytes")
    return pdf


# --- page 1 -------------------------------------------------------------------------------------


def _cover(
    content: SalesReportContent, company_name: str, logo_path: str | None, zone: ZoneInfo
) -> Iterator[Flowable]:
    logo = _logo(logo_path) if logo_path else None
    if logo is not None:
        yield logo
        yield Spacer(1, 3 * mm)
    yield _p(company_name, "title")
    yield _p("Daily sales MIS summary", "subtitle")
    who = f"Prepared for {content.user_name}. " if content.user_name else ""
    generated = _local(content.generated_at, zone)
    yield _p(f"{who}Generated {generated:%d %b %Y, %H:%M} ({content.timezone}).", "body")
    if content.models_used:
        used = "; ".join(
            f"{m.name} ({', '.join(TITLES[lens] for lens in m.lenses)})"
            for m in content.models_used
        )
        yield _p(f"Data sources used: {used}.", "small")
    for note in content.notes:
        yield _p(note, "small")


def _bullets(content: SalesReportContent) -> Iterator[Flowable]:
    yield _p("MIS summary", "h1")
    if not content.bullets:
        yield _p("No summary could be written today; see the tables below.", "body")
        return
    for bullet in content.bullets:
        yield _bullet(bullet)
        yield Spacer(1, 1.2 * mm)


def _bullet(bullet: Bullet) -> Paragraph:
    colour = TONE_COLOURS[bullet.tone]
    style = ParagraphStyle(
        f"bullet-{bullet.tone}",
        parent=STYLES["body"],
        fontSize=10,
        leading=14,
        leftIndent=14,
        bulletIndent=0,
        bulletFontName="ZapfDingbats",
        bulletFontSize=8,
        bulletColor=colour,
        textColor=colour,
    )
    return Paragraph(_rich(bullet.text, bullet.emphasis), style, bulletText=TONE_MARKS[bullet.tone])


def _rich(text: str, emphasis: list[str]) -> str:
    """Escaped text with its key phrases in bold (only phrases that are really in it)."""
    escaped = _escape(text)
    for phrase in emphasis:
        part = _escape(phrase)
        if part and part in escaped:
            escaped = escaped.replace(part, f"<b>{part}</b>", 1)
    return escaped


# --- one data source ----------------------------------------------------------------------------


def _section(section: SalesSection, zone: ZoneInfo) -> Iterator[Flowable]:
    details = []
    if section.data_date:
        details.append(f"Figures up to {day_label(section.data_date)}")
    if section.refreshed_at:
        details.append(f"data refreshed {_local(section.refreshed_at, zone):%d %b %Y, %H:%M}")
    header: list[Flowable] = [_p(section.name, "h1")]
    if details:
        header.append(_p(" | ".join(details), "small"))
    yield KeepTogether(header)
    for table in section.tables:
        yield from _lens(table)
    if section.trend is not None:
        chart = _trend_chart(section.trend)
        if chart is not None:
            title = f"Daily trend: {section.trend.kpi_label}"
            yield _chart_block(title, chart, _trend_legend(section.trend))
    if section.inactive:
        yield from _inactive(section.inactive)
    for note in section.notes:
        yield _p(note, "small")


def _lens(table: LensTable) -> Iterator[Flowable]:
    title = _p(table.title, "h2")
    if table.status != "ok":
        yield KeepTogether([title, _p(table.message or STATUS_TEXT[table.status], "body")])
        return
    head: list[Flowable] = [title]
    if table.main is not None:
        head.append(_grid(table.main))
        if table.main.note:
            head.append(_p(table.main.note, "small"))
    yield KeepTogether(head)
    if table.findings:
        yield Spacer(1, 2 * mm)
        yield from _text(table.findings)
    evidence = _evidence(table)
    for suggestion in table.suggestions:
        yield Spacer(1, 1.5 * mm)
        yield _action_box(suggestion, evidence, None)
    for drill in table.drilldowns:
        block: list[Flowable] = [Spacer(1, 2 * mm), _p(drill.title, "bold"), _grid(drill)]
        if drill.note:
            block.append(_p(drill.note, "small"))
        yield KeepTogether(block)
    notes = [*table.notes]
    if table.cut_short:
        notes.append("The analysis of this table was cut short: its query limit was reached.")
    for note in notes:
        yield _p(note, "small")


def _grid(table: Table) -> Flowable:
    """A table with each change coloured by whether it is good news for that measure."""
    wide = len(table.columns) >= WIDE_TABLE
    cell_style = ParagraphStyle(
        "grid", parent=STYLES["cell"], fontSize=6.5 if wide else 8, leading=8 if wide else 10
    )
    number_style = ParagraphStyle("gridnum", parent=cell_style, alignment=2)
    head_style = ParagraphStyle("gridhead", parent=cell_style, fontName="Helvetica-Bold")
    rows: list[list[Any]] = [
        [
            Paragraph(_escape(c), head_style if i == 0 else _right(head_style))
            for i, c in enumerate(table.columns)
        ]
    ]
    for row in table.rows:
        rows.append([_cell(c, cell_style if i == 0 else number_style) for i, c in enumerate(row)])
    return _table(rows, _widths(table))


def _widths(table: Table) -> list[float]:
    """Columns as wide as their longest entry (a header wraps at its words), within limits, so
    numbers never break across lines; the rest of the page width is shared out by size."""
    sizes = []
    for index, header in enumerate(table.columns):
        longest = max((len(row[index].text) for row in table.rows if index < len(row)), default=0)
        words = max((len(w) for w in header.split()), default=0)
        sizes.append(max(longest, words, 4) + 2)
    total = sum(sizes)
    return [CONTENT_WIDTH * size / total for size in sizes]


def _cell(cell: Cell, style: ParagraphStyle) -> Paragraph:
    text = _escape(cell.text)
    if cell.tone in ("good", "bad"):
        colour = "#1a7f37" if cell.tone == "good" else "#b42318"
        text = f"<font color='{colour}'>{text}</font>"
    return Paragraph(text, style)


def _right(style: ParagraphStyle) -> ParagraphStyle:
    return ParagraphStyle(f"{style.name}-r", parent=style, alignment=2)


def _evidence(table: LensTable) -> dict[str, str]:
    """Fact id -> what a reader sees for it in the tables, every cell of that fact in its row
    ("GOLD, MTD: 12,34,567.89, MTD vs LY: -20.00%")."""
    index: dict[str, str] = {}
    for grid in [g for g in (table.main, *table.drilldowns) if g is not None]:
        for row in grid.rows:
            shown: dict[str, list[str]] = {}
            for column, cell in zip(grid.columns, row, strict=False):
                if cell.fact and cell.text:
                    shown.setdefault(cell.fact, []).append(f"{column}: {cell.text}")
            for fact, parts in shown.items():
                index.setdefault(fact, f"{row[0].text}, {', '.join(parts)}")
    return index


# --- action points ------------------------------------------------------------------------------


def _action_box(suggestion: Any, evidence: dict[str, str], table_title: str | None) -> Flowable:
    meta = [f"Data source: {suggestion.model_name}"]
    if table_title:
        meta.append(f"Table: {table_title}")
    if suggestion.where:
        meta.append(f"Where: {suggestion.where}")
    if suggestion.owner:
        meta.append(f"Who: {suggestion.owner}")
    meta.append(f"By: {URGENCY_TEXT[suggestion.urgency]}")
    parts: list[Flowable] = [
        _p(suggestion.title, "bold"),
        _p("  |  ".join(meta), "small"),
        _p(suggestion.reason, "body"),
    ]
    if suggestion.notes:
        parts.append(_p(f"Notes: {suggestion.notes}", "body"))
    for figure in (evidence[e] for e in suggestion.evidence if e in evidence):
        parts.append(Paragraph(_escape(figure), STYLES["small"], bulletText="•"))
    box = PdfTable([[parts]], colWidths=[CONTENT_WIDTH])
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PANEL_BG),
                ("LINEBEFORE", (0, 0), (0, -1), 3, PRIORITY_COLOURS[suggestion.priority]),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return box


def _actions(actions: list[SalesAction], evidence: dict[str, str]) -> Iterator[Flowable]:
    heading = _p("Action points", "h1")
    if not actions:
        yield KeepTogether([heading, _p("No action points for today.", "body")])
        return
    yield CondPageBreak(60 * mm)
    first = True
    for priority in ("high", "medium", "low"):
        chosen = [a for a in actions if a.priority == priority]
        if not chosen:
            continue
        style = ParagraphStyle(
            f"h2-{priority}", parent=STYLES["h2"], textColor=PRIORITY_COLOURS[priority]
        )
        title = Paragraph(f"{priority.capitalize()} priority ({len(chosen)})", style)
        for index, action in enumerate(chosen):
            box = _action_box(action, evidence, TITLES[action.lens])
            lead = ([heading] if first else []) + ([title] if index == 0 else [])
            yield KeepTogether([*lead, box, Spacer(1, 2.5 * mm)])
            first = False


def _not_included(sections: list[SalesSection]) -> Iterator[Flowable]:
    others = [s for s in sections if s.status != "ok"]
    if not others:
        return
    rows: list[list[Any]] = [[_p("Data source", "head"), _p("Why", "head")]]
    rows += [[_p(s.name, "cell"), _p(s.message or STATUS_TEXT[s.status], "cell")] for s in others]
    yield Spacer(1, 4 * mm)
    yield KeepTogether(
        [
            _p("Data sources without figures today", "h1"),
            _table(rows, [60 * mm, CONTENT_WIDTH - 60 * mm]),
        ]
    )
