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
    change: dict[str, Any] | None = None  # explain-change analysis (app/agent/change.py)
    notes: list[str] = field(default_factory=list)  # must be stated in the answer

    def as_dict(self) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "steps": [vars(s) for s in self.steps],
            "comparisons": self.comparisons,
        }
        if self.change is not None:
            facts["change"] = self.change
        if self.notes:
            facts["notes"] = self.notes
        return facts

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
        if self.change is not None:
            values += _numbers_in(self.change)
        return values


def _numbers_in(node: Any) -> list[float]:
    if isinstance(node, bool):
        return []
    if isinstance(node, int | float):
        return [float(node)]
    if isinstance(node, dict):
        return [n for v in node.values() for n in _numbers_in(v)]
    if isinstance(node, list):
        return [n for v in node for n in _numbers_in(v)]
    return []


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
        if not comparisons:
            comparisons = _grouped_comparison(first, second, outcomes[0], outcomes[1])
    return Facts(steps, comparisons, {o.result.gateway for o in outcomes})


def _grouped_comparison(
    first: StepFacts, second: StepFacts, a: StepOutcome, b: StepOutcome
) -> list[dict[str, Any]]:
    """Two grouped results (possibly from different models) matched on their group values,
    e.g. sales by store vs target by store. Largest differences first."""
    if len(first.group_columns) != 1 or len(second.group_columns) != 1:
        return []
    if not first.value_columns or not second.value_columns:
        return []

    def by_group(outcome: StepOutcome, facts: StepFacts) -> dict[str, tuple[str, float]]:
        group, value = facts.group_columns[0], facts.value_columns[0]
        out: dict[str, tuple[str, float]] = {}
        for row in outcome.result.rows:
            number = to_number(row.get(value))
            if row.get(group) is not None and number is not None:
                out[str(row[group]).strip().lower()] = (str(row[group]), number)
        return out

    left, right = by_group(a, first), by_group(b, second)
    matched: list[dict[str, Any]] = []
    for key in left.keys() & right.keys():
        (group, x), (_, y) = left[key], right[key]
        matched.append(
            {
                "group": group,
                "first": first.label,
                "second": second.label,
                "first_value": x,
                "second_value": y,
                "difference": round(x - y, 2),
                "percent_change": round((x - y) / y * 100, 2) if y else None,
            }
        )
    matched.sort(key=lambda m: abs(m["difference"]), reverse=True)
    return matched[:TOP_ROWS]
