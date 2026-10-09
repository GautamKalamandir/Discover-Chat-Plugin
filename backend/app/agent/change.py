"""'Why did X change between two periods?' (explain-change analysis).

The LLM only picks the measure, filters, the two periods and up to three breakdowns. The server:
1. resolves both periods; if one is still running and the user hasn't said how to compare, asks
   (decision: like-for-like or full period, asked each time);
2. builds one query for the totals and one per breakdown, each returning both periods side by side;
3. computes the total change and each item's change and share of it, deterministically.
The answer then names where the change happened in the data; it can't show external causes.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.agent.analyzer import Facts, StepFacts, to_number
from app.agent.dax_builder import _AGG, _date, _filter_table, column, literal, quote_name
from app.agent.executor import StepOutcome
from app.agent.fiscal import describe_range, resolve_period
from app.agent.models import ChangeAnalysis, PlanStep, parse_ref
from app.core.errors import AppError, ErrorCode

TOP_MOVERS = 3

Range = tuple[date, date]


@dataclass(frozen=True)
class ChangePeriods:
    baseline: Range
    current: Range

    @property
    def baseline_label(self) -> str:
        return describe_range(*self.baseline)

    @property
    def current_label(self) -> str:
        label = describe_range(*self.current)
        return label if label != self.baseline_label else f"{label} (current)"


def resolve_periods(
    change: ChangeAnalysis, today: date, fiscal_start_month: int
) -> ChangePeriods | str:
    """The two ranges to compare, or a clarification question when a period is still running."""
    baseline = resolve_period(change.baseline, today, fiscal_start_month)
    current = resolve_period(change.current, today, fiscal_start_month)
    for start, _ in (baseline, current):
        if start > today:
            raise AppError(
                ErrorCode.QUESTION_INVALID,
                "One of those periods hasn't started yet. Please choose periods up to today.",
                422,
            )
    running = [r for r in (baseline, current) if r[1] > today]
    if not running:
        return ChangePeriods(baseline, current)

    elapsed = min((today - start).days for start, _ in running)
    like = ChangePeriods(_trim(baseline, elapsed), _trim(current, elapsed))
    full = ChangePeriods(_cap(baseline, today), _cap(current, today))
    if change.basis == "like_for_like":
        return like
    if change.basis == "full_period":
        return full
    return (
        f"Data is only available up to {today:%d %b %Y}, so one period is incomplete. "
        f"Should I compare the same dates in both periods ({like.baseline_label} vs "
        f"{like.current_label}), or {full.baseline_label} with {full.current_label} so far?"
    )


def _trim(period: Range, elapsed_days: int) -> Range:
    start, end = period
    return start, min(end, start + timedelta(days=elapsed_days))


def _cap(period: Range, today: date) -> Range:
    return period[0], min(period[1], today)


def build_steps(
    change: ChangeAnalysis, periods: ChangePeriods, *, max_steps: int
) -> list[PlanStep]:
    """One step for the totals, one per breakdown (within the step limit), DAX built here."""
    if change.measure:
        parsed = parse_ref(change.measure)
        name = parsed[1] if parsed else change.measure.strip("[]")
        expression = quote_name(name)
    elif change.aggregation:
        expression = f"{_AGG[change.aggregation.function]}({column(change.aggregation.column)})"
    else:
        raise AppError(
            ErrorCode.QUERY_REJECTED,
            "I couldn't build a query for that.",
            422,
            log_detail="change analysis without measure or aggregation",
        )

    def period_value(time_column: str, period: Range) -> str:
        col = column(time_column)
        start, end = period
        return (
            f"CALCULATE({expression}, FILTER(ALL({col}), "
            f"{col} >= {_date(start)} && {col} <= {_date(end)}))"
        )

    filters = [_filter_table(f) for f in change.filters]
    baseline = period_value(change.baseline.column, periods.baseline)
    current = period_value(change.current.column, periods.current)
    values = [
        f"{literal(periods.baseline_label)}, {baseline}",
        f"{literal(periods.current_label)}, {current}",
    ]

    def dax(driver: str | None) -> str:
        args = ([column(driver)] if driver else []) + filters + values
        return "EVALUATE\nSUMMARIZECOLUMNS(\n    " + ",\n    ".join(args) + "\n)"

    steps = [
        PlanStep(
            model_id=change.model_id,
            label=f"{change.label}: {periods.baseline_label} vs {periods.current_label}"[:120],
            filters=change.filters,
            custom_dax=dax(None),
        )
    ]
    for driver in change.drivers[: max(max_steps - 1, 0)]:
        name = (parse_ref(driver) or ("", driver))[1]
        steps.append(
            PlanStep(
                model_id=change.model_id,
                label=f"{change.label} by {name}"[:120],
                group_by=[driver],
                filters=change.filters,
                custom_dax=dax(driver),
            )
        )
    return steps


def _period_values(row: dict[str, Any], periods: ChangePeriods) -> tuple[float, float]:
    def value(label: str) -> float:
        return to_number(row.get(f"[{label}]")) or 0.0

    return value(periods.baseline_label), value(periods.current_label)


def with_change_columns(
    columns: list[str], rows: list[dict[str, Any]], periods: ChangePeriods
) -> tuple[list[str], list[dict[str, Any]]]:
    """Adds [Change] and [Change %] for display (the table shown in the visual)."""
    shown = []
    for row in rows:
        before, after = _period_values(row, periods)
        shown.append(
            {
                **row,
                "[Change]": round(after - before, 2),
                "[Change %]": round((after - before) / before * 100, 2) if before else None,
            }
        )
    return [*columns, "[Change]", "[Change %]"], shown


def analyze_change(
    change: ChangeAnalysis,
    periods: ChangePeriods,
    outcomes: list[StepOutcome],
    model_name: str,
) -> Facts:
    total_before, total_after = (
        _period_values(outcomes[0].result.rows[0], periods)
        if outcomes and outcomes[0].result.rows
        else (0.0, 0.0)
    )
    total_change = total_after - total_before
    filters = [f"{f.column} {f.op} {', '.join(map(str, f.values))}" for f in change.filters]
    summary: dict[str, Any] = {
        "analysed": change.label,
        "filters": filters,
        "baseline_period": periods.baseline_label,
        "current_period": periods.current_label,
        "baseline_value": round(total_before, 2),
        "current_value": round(total_after, 2),
        "change": round(total_change, 2),
        "percent_change": round(total_change / total_before * 100, 2) if total_before else None,
        "breakdowns": [],
        "note": "These show where the change happened in the data, not external causes.",
    }
    notes: list[str] = []
    for outcome in outcomes[1:]:
        driver = outcome.step.group_by[0]
        name = (parse_ref(driver) or ("", driver))[1]
        pairs = {_period_values(row, periods) for row in outcome.result.rows}
        if outcome.result.row_count >= 3 and len(pairs) == 1:
            # Same values for every item: the measure ignores this breakdown (e.g. built with ALL).
            notes.append(
                f"The measure doesn't break down by {name}, so that breakdown is left out."
            )
            continue
        # Power BI spells the group column its own way ('T'[C] vs T[C]): take it from the result.
        group_column = next((c for c in outcome.result.columns if not c.startswith("[")), driver)
        items: list[dict[str, Any]] = []
        for row in outcome.result.rows:
            before, after = _period_values(row, periods)
            delta = after - before
            items.append(
                {
                    "item": str(row.get(group_column)),
                    "baseline_value": round(before, 2),
                    "current_value": round(after, 2),
                    "change": round(delta, 2),
                    "percent_change": round(delta / before * 100, 2) if before else None,
                    "share_of_total_change": round(delta / total_change * 100, 2)
                    if total_change
                    else None,
                }
            )
        items.sort(key=lambda i: i["change"])
        summary["breakdowns"].append(
            {
                "by": name,
                "biggest_decreases": [i for i in items if i["change"] < 0][:TOP_MOVERS],
                "biggest_increases": [i for i in reversed(items) if i["change"] > 0][:TOP_MOVERS],
                "items": len(items),
            }
        )

    steps = [
        StepFacts(
            label=o.step.label,
            model_name=model_name,
            filters=filters,
            period=None,
            row_count=o.result.row_count,
            truncated=o.result.truncated,
            value_columns=[c for c in o.result.columns if c.startswith("[")],
            group_columns=[c for c in o.result.columns if not c.startswith("[")],
        )
        for o in outcomes
    ]
    return Facts(
        steps, change=summary, data_sources={o.result.gateway for o in outcomes}, notes=notes
    )
