"""Query results as facts (design §4.3): every figure the report may show or the agent may cite,
with its id, its value against last year and whether the change is good news for that measure.

The agent only ever sees these facts (formatted by the server, trimmed to the largest movers), never
raw rows; grounding later compares text with the exact values kept here.
"""

import math
from typing import Any

from pydantic import BaseModel, Field

from app.agent.analyzer import to_number
from app.reports.sales.dax import Figure
from app.reports.sales.periods import BASE_PERIODS
from app.reports.sales.steps import LensStep
from app.reports.text import change_words, format_indian

ROWS_SHOWN = 5  # largest and smallest movers per step, each way, in the agent's history
EARLIER_ROWS_SHOWN = 2  # the same for the queries before the latest one


class Fact(BaseModel):
    id: str  # e.g. "lob.2.r3.m1|mtd"
    row_number: int = 0  # the result row it came from (0: computed by the server)
    row: dict[str, str] = Field(default_factory=dict)  # group-by column -> value
    measure_id: str = ""  # m1, m2 ... in the step's order
    measure: str
    period: str  # day | mtd | fytd | months_fy | month_ends_fy
    month: str | None = None
    band: str | None = None
    value: float | None
    ly_value: float | None = None  # same period last year, when it was asked for
    change: float | None = None
    percent_change: float | None = None
    good: bool | None = None  # the change is good news for this measure


class StepResult(BaseModel):
    number: int  # 1 is the seed
    model_id: str
    why: str
    step: LensStep
    facts: list[Fact] = Field(default_factory=list)
    rows: int = 0
    truncated: bool = False
    error: str | None = None  # error code when the query failed
    unmatched: list[str] = Field(default_factory=list)  # filter names another source lacks


def parse_result(
    lens_key: str,
    number: int,
    step: LensStep,
    figures: list[Figure],
    columns: list[str],
    rows: list[dict[str, Any]],
    *,
    higher_is_better: dict[str, bool],
) -> list[Fact]:
    """`higher_is_better` by measure id (m1, m2 ...); missing means a rise is good news."""
    groups = [c for c in columns if not c.startswith("[")]
    by_alias = {f.alias: f for f in figures}
    facts: list[Fact] = []
    for row_number, row in enumerate(rows, start=1):
        labels = {group: _text(row.get(group)) for group in groups}
        for figure in figures:
            if figure.period not in BASE_PERIODS:
                continue  # last year's figures are paired with their period below
            value = _value(row.get(f"[{figure.alias}]"))
            ly_alias = figure.alias.replace(f"|{figure.period}", f"|{figure.period}_ly", 1)
            has_ly = ly_alias in by_alias
            ly_value = _value(row.get(f"[{ly_alias}]")) if has_ly else None
            if value is None and ly_value is None:
                continue
            change = pct = None
            if has_ly:
                # Blank on one side means nothing was recorded then: a store that sold last year
                # and nothing this year is a 100% fall, not a missing figure.
                value, ly_value = value or 0.0, ly_value or 0.0
                change = round(value - ly_value, 2)
                pct = round(change / ly_value * 100, 2) if ly_value else None
            better = higher_is_better.get(figure.measure_id, True)
            suffix = "|".join([figure.period, *filter(None, [figure.month])])
            band = f"|{figure.alias.rsplit('|', 1)[1]}" if figure.band else ""
            facts.append(
                Fact(
                    id=f"{lens_key}.{number}.r{row_number}.{figure.measure_id}|{suffix}{band}",
                    row_number=row_number,
                    row=labels,
                    measure_id=figure.measure_id,
                    measure=figure.measure,
                    period=figure.period,
                    month=figure.month,
                    band=figure.band,
                    value=value,
                    ly_value=ly_value,
                    change=change,
                    percent_change=pct,
                    good=(change > 0) == better if change else None,
                )
            )
    return facts


def compact(result: StepResult, rows_shown: int = ROWS_SHOWN) -> dict[str, Any]:
    """One step as the agent sees it: the query, its measures, and its largest movers. Each figure
    is one short line (ADR 0016 amendment 1: every request stays small):

        "lob.1.r3.m1|mtd: 12,34,567.89 vs LY 11,00,000 (up 12.23%, good)"
    """
    shown: dict[str, Any] = {
        "step": result.number,
        "data_source": result.model_id,
        "why": result.why,
        "query": result.step.model_dump(exclude={"why", "model_id"}, exclude_defaults=True),
        "rows": result.rows,
    }
    if result.error:
        shown["error"] = f"the query failed ({result.error}); it has no figures"
        return shown
    if result.truncated:
        shown["note"] = "more rows than one query returns: only the first ones are known"
    if result.unmatched:
        shown["not_found_in_this_data_source"] = result.unmatched
    shown["measures"] = {f.measure_id: f.measure for f in result.facts}
    rows = _rows(result.facts)
    if len(rows) > 2 * rows_shown:
        rows.sort(key=_row_change)
        rows = rows[:rows_shown] + rows[-rows_shown:]
        shown["note_rows"] = f"{rows_shown} largest falls and rises of {result.rows} rows shown"
    shown["results"] = [
        {"row": _row_text(row[0].row), "figures": [figure_text(f) for f in row]} for row in rows
    ]
    return shown


def figure_text(fact: Fact) -> str:
    """`<id>: <value>`, then against last year `vs LY <value> (<change>, good|bad)`."""
    text = f"{fact.id}: {format_indian(fact.value)}"
    if fact.band:
        text += f" [{fact.band}]"
    if fact.ly_value is None and fact.change is None:
        return text
    if fact.percent_change is not None:
        change = (
            "no change"
            if fact.percent_change == 0
            else f"{'up' if fact.percent_change > 0 else 'down'} {abs(fact.percent_change):.2f}%"
        )
    else:
        change = change_words(fact.change, None)
    news = "" if fact.good is None else (", good" if fact.good else ", bad")
    return f"{text} vs LY {format_indian(fact.ly_value)} ({change}{news})"


def _row_text(row: dict[str, str]) -> str:
    """`Lob=GOLD, Location Name=STR - VAPI` (column names without their table)."""
    return ", ".join(
        f"{column.rsplit('[', 1)[-1].rstrip(']')}={value}" for column, value in row.items()
    )


def _rows(facts: list[Fact]) -> list[list[Fact]]:
    rows: dict[str, list[Fact]] = {}
    for fact in facts:
        rows.setdefault(fact.id.split(".")[2], []).append(fact)
    return list(rows.values())


def _row_change(row: list[Fact]) -> float:
    first = row[0]
    return first.percent_change if first.percent_change is not None else (first.change or 0.0)


def _value(raw: Any) -> float | None:
    number = to_number(raw)
    return None if number is None or not math.isfinite(number) else round(number, 2)


def _text(value: Any) -> str:
    return "" if value is None else str(value)
