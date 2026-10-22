"""Findings and action points for one table (ADR 0016 §13, design §6).

The agent writes from formatted facts only; the server checks everything it wrote:
- every number must match a computed figure at the precision shown (dates in the text are left
  out of the check, never allowed as figures);
- every action point must cite known fact ids and pass the same number check, or it is dropped;
- findings that fail are replaced by a server-written summary of the table's largest changes.
"""

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.answer import ungrounded_numbers
from app.agent.planner import untrusted
from app.core.errors import AppError
from app.llm.base import ChatMessage, LLMProvider
from app.llm.budget import call_limits
from app.reports import facts as report_facts
from app.reports.models import Suggestion, SuggestionText
from app.reports.sales.content import Bullet, SalesAction, SalesSection
from app.reports.sales.discovery import ModelRoles
from app.reports.sales.loop import LoopResult
from app.reports.sales.periods import SalesPeriods
from app.reports.sales.results import Fact, compact
from app.reports.sales.tables import LensTable, Table, Tone
from app.reports.text import clean_text, format_indian

logger = logging.getLogger(__name__)

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
URGENCY_ORDER = {"today": 0, "this_week": 1, "this_month": 2}
CUT_SHORT = "The analysis was cut short: the query limit for this table was reached."
# Answer room per call (ADR 0016 amendment 1): requests fit free per-minute limits.
FINDINGS_OUTPUT_TOKENS = 1400
BULLETS_OUTPUT_TOKENS = 1100
TEMPLATE_ROWS = 3


class LensFindings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: str = Field(default="", max_length=1500)
    suggestions: list[SuggestionText] = Field(default_factory=list, max_length=12)


async def write_findings(
    llm: LLMProvider,
    system_prompt: str,
    table: LensTable,
    result: LoopResult,
    derived: list[Fact],
    roles: ModelRoles,
    periods: SalesPeriods,
    *,
    max_suggestions: int,
) -> LensTable:
    facts = result.facts() + derived
    known = {f.id for f in facts}
    allowed = _numbers(facts) + [float(s.rows) for s in result.steps]
    template = template_findings(table.main)
    if result.written:
        # The agent wrote them when it finished its investigation: no further call needed.
        return _accept(
            table, result.findings, result.suggestions, known, allowed, template, roles,
            max_suggestions,
        )  # fmt: skip
    notes = [*table.notes, *([CUT_SHORT] if table.cut_short else [])]
    payload: dict[str, Any] = {
        "table": table.title,
        "data_source": roles.model.name,
        "latest_day_with_data": f"{periods.day:%d %b %Y}",
        "queries": [compact(s) for s in result.steps],
    }
    if derived:
        payload["computed_by_the_server"] = [
            {"id": f.id, "row": f.row, "measure": f.measure, "value": format_indian(f.value)}
            for f in derived
        ]
    if result.note:
        payload["investigation_note"] = result.note
    if notes:
        payload["notes"] = notes
    system = system_prompt.replace("{max_suggestions}", str(max_suggestions)).replace(
        "{schema}", json.dumps(LensFindings.model_json_schema(), separators=(",", ":"))
    )
    messages = [
        ChatMessage("system", system),
        ChatMessage("user", "FACTS\n" + untrusted(json.dumps(payload, default=str))),
    ]
    try:
        with call_limits(FINDINGS_OUTPUT_TOKENS, "low"):
            answer = await llm.complete_json(messages, LensFindings)
    except AppError as exc:
        logger.warning("Report table %s: findings failed (%s)", table.lens, exc.code)
        return table.model_copy(update={"findings": template})
    return _accept(
        table, answer.findings, answer.suggestions, known, allowed, template, roles,
        max_suggestions,
    )  # fmt: skip


def _accept(
    table: LensTable,
    text: str,
    items: list[SuggestionText],
    known: set[str],
    allowed: list[float],
    template: str,
    roles: ModelRoles,
    max_suggestions: int,
) -> LensTable:
    """Findings that pass the number check (else the template), and the action points whose
    evidence ids are known and whose numbers match the facts."""
    findings = clean_text(text)
    if not findings or not _grounded(findings, allowed):
        findings = template
    suggestions: list[Suggestion] = []
    dropped = 0
    for item in items:
        evidence = list(dict.fromkeys(item.evidence))
        cleaned = {
            name: clean_text(getattr(item, name))
            for name in ("title", "where", "owner", "reason", "notes")
        }
        if (
            not evidence
            or not set(evidence) <= known
            or not cleaned["title"]
            or not cleaned["reason"]
            or not _grounded(" ".join(cleaned.values()), allowed)
        ):
            dropped += 1
            continue
        suggestions.append(
            Suggestion(
                **cleaned,
                priority=item.priority,
                urgency=item.urgency,
                evidence=evidence,
                model_name=roles.model.name,
            )
        )
    if dropped:
        logger.info("Report table %s: %d action points dropped", table.lens, dropped)
    suggestions.sort(key=lambda s: (PRIORITY_ORDER[s.priority], URGENCY_ORDER[s.urgency]))
    return table.model_copy(
        update={"findings": findings, "suggestions": suggestions[:max_suggestions]}
    )


def template_findings(main: Table | None) -> str:
    """The table's largest falls and rises against last year, as the server sees them."""
    if main is None or not main.rows:
        return ""
    column = next((i for i, c in enumerate(main.columns) if c.endswith("vs LY")), None)
    if column is None:
        return ""
    lines = []
    for tone, words in (("bad", "Largest falls"), ("good", "Largest rises")):
        rows = [r for r in main.rows if r[column].tone == tone]
        if rows:
            listed = ", ".join(f"{r[0].text} ({r[column].text})" for r in rows[:TEMPLATE_ROWS])
            lines.append(f"- {words} against last year ({main.columns[column]}): {listed}.")
    return "\n".join(lines)


def _numbers(facts: list[Fact]) -> list[float]:
    return report_facts.numbers_in(
        [[f.value, f.ly_value, f.change, f.percent_change] for f in facts]
    )


def _grounded(text: str, allowed: list[float]) -> bool:
    return not ungrounded_numbers(report_facts.strip_dates(text), allowed)


# --- the report's summary bullets and combined action list (design §7) ---------------------------

MAX_BULLETS = 8
MAX_ACTIONS = 40
MAIN_ROWS_SHOWN = 6


class BulletText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=300)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    emphasis: list[str] = Field(default_factory=list, max_length=2)


class SummaryBullets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bullets: list[BulletText] = Field(default_factory=list, max_length=12)


async def write_bullets(
    llm: LLMProvider, system_prompt: str, sections: list[SalesSection]
) -> list[Bullet]:
    """The opening bullets. The server keeps a bullet only when its evidence ids are known and its
    numbers match those facts, and sets its colour from the facts it cites."""
    tables = [(s, t) for s in sections for t in s.tables if t.status == "ok"]
    facts = {f.id: f for _, t in tables for f in t.facts}
    allowed = _numbers(list(facts.values()))
    payload = [
        {
            "data_source": section.name,
            "latest_day_with_data": f"{section.data_date:%d %b %Y}" if section.data_date else None,
            "tables": [_table_view(t) for t in section.tables if t.status == "ok"],
        }
        for section in sections
        if section.status == "ok"
    ]
    system = system_prompt.replace("{max_bullets}", str(MAX_BULLETS)).replace(
        "{schema}", json.dumps(SummaryBullets.model_json_schema(), separators=(",", ":"))
    )
    messages = [
        ChatMessage("system", system),
        ChatMessage("user", "TABLES\n" + untrusted(json.dumps(payload, default=str))),
    ]
    try:
        with call_limits(BULLETS_OUTPUT_TOKENS, "low"):
            answer = await llm.complete_json(messages, SummaryBullets)
    except AppError as exc:
        logger.warning("Report summary bullets failed (%s); using the template", exc.code)
        return template_bullets(sections)

    bullets: list[Bullet] = []
    for item in answer.bullets:
        text = clean_text(item.text)
        evidence = list(dict.fromkeys(item.evidence))
        if not text or not evidence or not set(evidence) <= facts.keys():
            continue
        if not _grounded(text, allowed):
            continue
        emphasis = [p for p in (clean_text(e) for e in item.emphasis) if p and p in text]
        tone = tone_of([facts[e] for e in evidence])
        bullets.append(Bullet(text=text, tone=tone, emphasis=emphasis, evidence=evidence))
    return bullets[:MAX_BULLETS] or template_bullets(sections)


def tone_of(cited: list[Fact]) -> Tone:
    """Green when every cited change is good news, red when any is bad news, else neutral."""
    goods = [f.good for f in cited if f.good is not None]
    if not goods:
        return "neutral"
    return "good" if all(goods) else "bad"


def template_bullets(sections: list[SalesSection]) -> list[Bullet]:
    """Each table's largest fall and rise against last year, as the server sees them."""
    bullets: list[Bullet] = []
    for section in sections:
        for table in section.tables:
            main = table.main
            if table.status != "ok" or main is None:
                continue
            column = next((i for i, c in enumerate(main.columns) if c.endswith("vs LY")), None)
            if column is None:
                continue
            for tone in ("bad", "good"):
                row = next((r for r in main.rows if r[column].tone == tone), None)
                fact = row[column].fact if row is not None else None
                if row is None or fact is None:
                    continue
                text = (
                    f"{section.name}, {table.title}: {row[0].text} {row[column].text} "
                    f"({main.columns[column]})."
                )
                bullets.append(
                    Bullet(text=text, tone=row[column].tone or "neutral", evidence=[fact])
                )
    return bullets[:MAX_BULLETS]


def merge_actions(sections: list[SalesSection]) -> list[SalesAction]:
    """Every table's action points in one list: priority, then urgency."""
    actions = [
        SalesAction(**suggestion.model_dump(), lens=table.lens)
        for section in sections
        for table in section.tables
        for suggestion in table.suggestions
    ]
    actions.sort(key=lambda a: (PRIORITY_ORDER[a.priority], URGENCY_ORDER[a.urgency]))
    return actions[:MAX_ACTIONS]


def _table_view(table: LensTable) -> dict[str, Any]:
    """A table as the agent sees it: each cell's text, with the fact id of cells that show one."""
    view: dict[str, Any] = {"title": table.title, "findings": table.findings}
    if table.main is not None:
        view["columns"] = table.main.columns
        view["rows"] = [
            [{"text": c.text, "id": c.fact} if c.fact else c.text for c in row]
            for row in table.main.rows[:MAIN_ROWS_SHOWN]
        ]
    if table.suggestions:
        view["action_points"] = [
            {"priority": s.priority, "title": s.title, "where": s.where} for s in table.suggestions
        ]
    if table.notes or table.cut_short:
        view["notes"] = [*table.notes, *([CUT_SHORT] if table.cut_short else [])]
    return view
