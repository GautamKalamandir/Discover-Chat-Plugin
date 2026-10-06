"""Stage 11: deterministic facts from query results, so the LLM narrates and never calculates."""

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.agent.executor import StepOutcome
from app.agent.fiscal import describe_range, resolve_period
from app.agent.models import QueryPlan

TOP_ROWS = 10


def to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None
    return None


@dataclass
class StepFacts:
    label: str
    model_name: str
    filters: list[str]
    period: str | None
    row_count: int
    truncated: bool
    value_columns: list[str]
    group_columns: list[str]
    single_values: dict[str, float] = field(default_factory=dict)
    top_rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Facts:
    steps: list[StepFacts]
    comparisons: list[dict[str, Any]] = field(default_factory=list)
    data_sources: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": [vars(s) for s in self.steps],
            "comparisons": self.comparisons,
        }

    def numbers(self) -> list[float]:
        """Every number the answer may legitimately mention."""
        values: list[float] = []
        for step in self.steps:
            values += list(step.single_values.values())
            values += [step.row_count, len(step.top_rows)]
            for row in step.top_rows:
                values += [n for n in (to_number(v) for v in row.values()) if n is not None]
        for comparison in self.comparisons:
            values += [v for v in comparison.values() if isinstance(v, int | float)]
        return values


def analyze(
    plan: QueryPlan,
    outcomes: list[StepOutcome],
    model_names: dict[str, str],
    *,
    today: date,
    fiscal_start_month: int,
) -> Facts:
    steps: list[StepFacts] = []
    for outcome in outcomes:
        step, result = outcome.step, outcome.result
        # Power BI names calculated columns "[Alias]" and group-by columns "Table[Column]".
        value_columns = [c for c in result.columns if c.startswith("[")]
        group_columns = [c for c in result.columns if not c.startswith("[")]
        period = None
        if step.time is not None:
            period = describe_range(*resolve_period(step.time, today, fiscal_start_month))
        facts = StepFacts(
            label=step.label,
            model_name=model_names.get(step.model_id, step.model_id),
            filters=[f"{f.column} {f.op} {', '.join(map(str, f.values))}" for f in step.filters],
            period=period,
            row_count=result.row_count,
            truncated=result.truncated,
            value_columns=value_columns,
            group_columns=group_columns,
        )
        if result.row_count == 1 and not group_columns:
            row = result.rows[0]
            facts.single_values = {
                c.strip("[]"): n for c in value_columns if (n := to_number(row.get(c))) is not None
            }
        else:
            key = value_columns[0] if value_columns else None
            rows = list(result.rows)
            if key and step.order != "group_asc":
                rows.sort(
                    key=lambda r: to_number(r.get(key)) or 0.0, reverse=step.order != "value_asc"
                )
            facts.top_rows = rows[:TOP_ROWS]
        steps.append(facts)

    comparisons: list[dict[str, Any]] = []
    if plan.combine == "compare" and len(steps) >= 2:
        first, second = steps[0], steps[1]
        for name in first.single_values.keys() & second.single_values.keys():
            a, b = first.single_values[name], second.single_values[name]
            comparisons.append(
                {
                    "measure": name,
                    "first": first.label,
                    "second": second.label,
                    "difference": round(a - b, 2),
                    "percent_change": round((a - b) / b * 100, 2) if b else None,
                }
            )
    return Facts(steps, comparisons, {o.result.gateway for o in outcomes})
