"""Server-side periods, DAX and figures for a /summary section (design §2.1).

The LLM only chooses which KPIs to show and which changes to explain; every date range, query and
number is built here. Ranges are whole days (`>= start && < next day`), so a date-time column keeps
the rows later in a day.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.agent.analyzer import to_number
from app.agent.dax_builder import _AGG, _date, column, literal, quote_name
from app.agent.models import Aggregation, parse_ref
from app.powerbi.dev_synthetic import LATEST_DATE, PREVIOUS_DATE
from app.reports.models import (
    Baseline,
    InactiveBreakdown,
    InactiveItem,
    KpiComparison,
    KpiRow,
    Trend,
    TrendPoint,
)

Range = tuple[date, date]


@dataclass(frozen=True)
class ReportKpi:
    """A validated KPI: `measure` is a visible Table[Measure] (or `aggregation` a visible
    column), `target` a visible measure."""

    id: str
    label: str
    measure: str | None
    aggregation: Aggregation | None
    target: str | None
    higher_is_better: bool = True

    @property
    def expression(self) -> str:
        return expression_of(self.measure, self.aggregation)

    @property
    def target_expression(self) -> str | None:
        return expression_of(self.target, None) if self.target else None


@dataclass(frozen=True)
class ReportPeriods:
    day: date
    prev_day: date | None  # the latest earlier date that has data
    week_ago: date
    mtd: Range
    last_mtd: Range

    def ranges(self) -> dict[str, Range]:
        ranges = {
            "day": (self.day, self.day),
            "week_ago": (self.week_ago, self.week_ago),
            "mtd": self.mtd,
            "last_mtd": self.last_mtd,
        }
        if self.prev_day is not None:
            ranges["prev_day"] = (self.prev_day, self.prev_day)
        return ranges


def report_periods(latest: date, previous: date | None) -> ReportPeriods:
    first = latest.replace(day=1)
    last_month_end = first - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    same_day = min(last_month_start + timedelta(days=latest.day - 1), last_month_end)
    return ReportPeriods(
        day=latest,
        prev_day=previous,
        week_ago=latest - timedelta(days=7),
        mtd=(first, latest),
        last_mtd=(last_month_start, same_day),
    )


def expression_of(measure: str | None, aggregation: Aggregation | None) -> str:
    if measure:
        parsed = parse_ref(measure)
        return quote_name(parsed[1] if parsed else measure.strip("[]"))
    if aggregation is None:  # pragma: no cover - validated before
        raise ValueError("a KPI needs a measure or an aggregation")
    return f"{_AGG[aggregation.function]}({column(aggregation.column)})"


def in_days(date_column: str, start: date, end: date) -> str:
    col = column(date_column)
    return (
        f"FILTER(ALL({col}), {col} >= {_date(start)} && {col} < {_date(end + timedelta(days=1))})"
    )


def latest_dates_dax(date_column: str, expression: str, today: date) -> str:
    """The latest date up to today with data for `expression`, and the latest date before it."""
    col = column(date_column)
    has_data = f"NOT ISBLANK(CALCULATE({expression}))"
    tomorrow = _date(today + timedelta(days=1))
    return (
        "EVALUATE\n"
        f"VAR _latest = MAXX(FILTER(ALL({col}), {col} < {tomorrow} && {has_data}), {col})\n"
        f"VAR _previous = MAXX(FILTER(ALL({col}), {col} < INT(_latest) && {has_data}), {col})\n"
        f'RETURN ROW({literal(LATEST_DATE)}, FORMAT(_latest, "yyyy-mm-dd"), '
        f'{literal(PREVIOUS_DATE)}, FORMAT(_previous, "yyyy-mm-dd"))'
    )


def parse_latest_dates(row: dict[str, Any]) -> tuple[date | None, date | None]:
    return _as_date(row.get(f"[{LATEST_DATE}]")), _as_date(row.get(f"[{PREVIOUS_DATE}]"))


def kpi_values_dax(kpis: list[ReportKpi], date_column: str, periods: ReportPeriods) -> str:
    """One ROW query: every KPI over every period (and its target over the day and the month)."""
    values: list[str] = []
    for kpi in kpis:
        for key, (start, end) in periods.ranges().items():
            filtered = f"CALCULATE({kpi.expression}, {in_days(date_column, start, end)})"
            values.append(f"{literal(f'{kpi.id}|{key}')}, {filtered}")
        if kpi.target_expression:
            for key, (start, end) in (
                ("target_day", (periods.day, periods.day)),
                ("target_mtd", periods.mtd),
            ):
                filtered = f"CALCULATE({kpi.target_expression}, {in_days(date_column, start, end)})"
                values.append(f"{literal(f'{kpi.id}|{key}')}, {filtered}")
    return "EVALUATE\nROW(\n    " + ",\n    ".join(values) + "\n)"


def kpi_rows(kpis: list[ReportKpi], row: dict[str, Any], periods: ReportPeriods) -> list[KpiRow]:
    def value(kpi: ReportKpi, key: str) -> float | None:
        number = to_number(row.get(f"[{kpi.id}|{key}]"))
        return None if number is None or not math.isfinite(number) else round(number, 2)

    rows: list[KpiRow] = []
    for kpi in kpis:
        today, month = value(kpi, "day"), value(kpi, "mtd")
        pairs: list[tuple[Baseline, str, float | None, float | None]] = []
        if periods.prev_day is not None:
            pairs.append(
                ("prev_day", f"vs {day_label(periods.prev_day)}", today, value(kpi, "prev_day"))
            )
        pairs.append(
            ("week_ago", f"vs {day_label(periods.week_ago)}", today, value(kpi, "week_ago"))
        )
        pairs.append(
            (
                "mtd",
                f"{range_label(periods.mtd)} vs {range_label(periods.last_mtd)}",
                month,
                value(kpi, "last_mtd"),
            )
        )
        if kpi.target:
            pairs.append(("target_day", "vs target for the day", today, value(kpi, "target_day")))
            pairs.append(("target_mtd", "month to date vs target", month, value(kpi, "target_mtd")))
        comparisons = [
            comparison(f"{kpi.id}.{key}", key, label, current, base)
            for key, label, current, base in pairs
            if current is not None or base is not None
        ]
        rows.append(
            KpiRow(
                id=kpi.id,
                label=kpi.label,
                value=today,
                comparisons=comparisons,
                higher_is_better=kpi.higher_is_better,
                achievement_day=_achievement(today, value(kpi, "target_day")),
                achievement_mtd=_achievement(month, value(kpi, "target_mtd")),
            )
        )
    return rows


def _achievement(actual: float | None, target: float | None) -> float | None:
    if actual is None or not target:
        return None
    return round(actual / target * 100, 2)


def shift_month(day: date, months: int) -> date:
    """The same day `months` months away, capped at that month's last day."""
    year, month0 = divmod(day.year * 12 + day.month - 1 + months, 12)
    first = date(year, month0 + 1, 1)
    last = date(year + (month0 + 1) // 12, (month0 + 1) % 12 + 1, 1) - timedelta(days=1)
    return min(first + timedelta(days=day.day - 1), last)


def trend_days(day: date, count: int) -> list[date]:
    return [day - timedelta(days=offset) for offset in range(count - 1, -1, -1)]


def trend_dax(kpi: ReportKpi, date_column: str, day: date, count: int) -> str:
    """One ROW query: the KPI for each of the last `count` days up to `day`, and for the same days
    one month earlier ("t|YYYY-MM-DD" and "e|YYYY-MM-DD")."""
    values: list[str] = []
    for current in trend_days(day, count):
        for prefix, when in (("t", current), ("e", shift_month(current, -1))):
            filtered = f"CALCULATE({kpi.expression}, {in_days(date_column, when, when)})"
            values.append(f"{literal(f'{prefix}|{current.isoformat()}')}, {filtered}")
    return "EVALUATE\nROW(\n    " + ",\n    ".join(values) + "\n)"


def parse_trend(kpi: ReportKpi, row: dict[str, Any], day: date, count: int) -> Trend:
    def value(key: str) -> float | None:
        number = to_number(row.get(f"[{key}]"))
        return None if number is None or not math.isfinite(number) else round(number, 2)

    points = [
        TrendPoint(
            day=current,
            value=value(f"t|{current.isoformat()}"),
            earlier_value=value(f"e|{current.isoformat()}"),
        )
        for current in trend_days(day, count)
    ]
    return Trend(kpi_label=kpi.label, higher_is_better=kpi.higher_is_better, days=points)


def inactive_dax(
    kpis: list[ReportKpi], breakdown: str, date_column: str, day: date, lookback: int
) -> str:
    """Per item of `breakdown`: each KPI over the lookback window before `day`, and on `day`."""
    start, end = day - timedelta(days=lookback), day - timedelta(days=1)
    values: list[str] = []
    for kpi in kpis:
        values.append(
            f"{literal(f'{kpi.id}|before')}, "
            f"CALCULATE({kpi.expression}, {in_days(date_column, start, end)})"
        )
        on_day = in_days(date_column, day, day)
        values.append(f"{literal(f'{kpi.id}|day')}, CALCULATE({kpi.expression}, {on_day})")
    return (
        "EVALUATE\nSUMMARIZECOLUMNS(\n    " + ",\n    ".join([column(breakdown), *values]) + "\n)"
    )


def inactive_items(
    kpis: list[ReportKpi],
    rows: list[dict[str, Any]],
    columns: list[str],
    *,
    by: str,
    lookback: int,
    truncated: bool,
    top: int = 20,
) -> list[InactiveBreakdown]:
    """Items with a positive value in the window but none (blank or 0) on the latest day."""
    group = next((c for c in columns if not c.startswith("[")), None)
    if group is None:
        return []
    found: list[InactiveBreakdown] = []
    for kpi in kpis:
        items: list[InactiveItem] = []
        for row in rows:
            before = to_number(row.get(f"[{kpi.id}|before]"))
            now = to_number(row.get(f"[{kpi.id}|day]"))
            if before is not None and before > 0 and not now and row.get(group) is not None:
                items.append(InactiveItem(item=str(row[group]), baseline_value=round(before, 2)))
        if items:
            items.sort(key=lambda i: i.baseline_value, reverse=True)
            found.append(
                InactiveBreakdown(
                    by=by,
                    kpi_label=kpi.label,
                    lookback_days=lookback,
                    items=items[:top],
                    total=len(items),
                    truncated=truncated,
                )
            )
    return found


def comparison(
    fact_id: str, baseline: Baseline, label: str, current: float | None, base: float | None
) -> KpiComparison:
    change = round(current - base, 2) if current is not None and base is not None else None
    percent = round(change / base * 100, 2) if change is not None and base else None
    return KpiComparison(
        fact_id=fact_id,
        baseline=baseline,
        label=label,
        current_value=current,
        baseline_value=base,
        change=change,
        percent_change=percent,
    )


def day_label(day: date) -> str:
    return f"{day:%d %b %Y}"


def range_label(period: Range) -> str:
    start, end = period
    if start == end:
        return day_label(start)
    if (start.year, start.month) == (end.year, end.month):
        return f"{start:%d} to {end:%d %b %Y}"
    return f"{day_label(start)} to {day_label(end)}"


def numbers_in(node: Any) -> list[float]:
    """Every number in a JSON-like structure: what a grounded text may mention."""
    if isinstance(node, bool):
        return []
    if isinstance(node, int | float):
        return [float(node)] if math.isfinite(node) else []
    if isinstance(node, dict):
        return [n for v in node.values() for n in numbers_in(v)]
    if isinstance(node, list | tuple):
        return [n for v in node for n in numbers_in(v)]
    return []


def date_numbers(*days: date | None) -> list[float]:
    """Day and month numbers of the dates shown, so "12 Oct" or "11/10" aren't flagged."""
    return [float(n) for d in days if d is not None for n in (d.day, d.month, d.year)]


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None
