"""DAX for a checked `LensStep` (design §4.2). Built only from the escaping helpers that chat uses,
so names and values from the agent can never change the query's shape.

    EVALUATE [TOPN(n,] SUMMARIZECOLUMNS(<group-by>, <filters>,
        "m1|mtd", CALCULATE(<m1>, <mtd days> [, <band>]), "m1|mtd_ly", ... ) [, <first figure>])
"""

from dataclasses import dataclass

from app.agent.dax_builder import _AGG, _filter_table, column, literal, quote_name
from app.agent.models import parse_ref
from app.reports.facts import in_days
from app.reports.sales.periods import PeriodKey, SalesPeriods
from app.reports.sales.steps import Band, LensStep


@dataclass(frozen=True)
class Figure:
    """One value column of the result."""

    alias: str  # e.g. "m1|mtd", "m1|months_fy|2026-04", "m1|mtd|b2"
    measure_id: str  # m1, m2 ...
    measure: str  # what the reader sees, e.g. "Total Net Sales" or "Sum of Weight"
    period: PeriodKey
    month: str | None = None  # "2026-04" for month-by-month periods
    band: str | None = None  # band label


def measure_labels(step: LensStep) -> list[tuple[str, str, str]]:
    """(id, label, DAX expression) of every measure and aggregation, in order."""
    found: list[tuple[str, str, str]] = []
    for ref in step.measures:
        parsed = parse_ref(ref)
        name = parsed[1] if parsed else ref.strip("[]")
        found.append((f"m{len(found) + 1}", name, quote_name(name)))
    for agg in step.aggregations:
        parsed = parse_ref(agg.column)
        label = f"{agg.function.title()} of {parsed[1] if parsed else agg.column}"
        found.append((f"m{len(found) + 1}", label, f"{_AGG[agg.function]}({column(agg.column)})"))
    return found


def compile_step(
    step: LensStep, date_column: str, periods: SalesPeriods
) -> tuple[str, list[Figure]]:
    figures: list[Figure] = []
    values: list[str] = []
    bands: list[tuple[int, Band] | None] = list(enumerate(step.bands, start=1)) or [None]
    for measure_id, label, expression in measure_labels(step):
        for period in step.periods:
            for month, (start, end) in periods.ranges(period):
                for banded in bands:
                    filters = [in_days(date_column, start, end)]
                    band_label = None
                    alias_parts = [measure_id, period, *([month] if month else [])]
                    if banded is not None and step.band_column:
                        number, band = banded
                        filters.append(_band_filter(step.band_column, band))
                        band_label = band.label
                        alias_parts.append(f"b{number}")
                    alias = "|".join(alias_parts)
                    values.append(
                        f"{literal(alias)}, CALCULATE({expression}, {', '.join(filters)})"
                    )
                    figures.append(Figure(alias, measure_id, label, period, month, band_label))

    args = [column(g) for g in step.group_by] + [_filter_table(f) for f in step.filters] + values
    table = "SUMMARIZECOLUMNS(\n    " + ",\n    ".join(args) + "\n)"
    first = quote_name(figures[0].alias)
    descending = step.order != "value_asc"
    if step.top_n is not None:
        table = f"TOPN({step.top_n}, {table}, {first}, {'DESC' if descending else 'ASC'})"
    dax = f"EVALUATE\n{table}"
    if step.order == "group_asc" and step.group_by:
        dax += "\nORDER BY " + ", ".join(column(g) + " ASC" for g in step.group_by)
    elif step.order in ("value_desc", "value_asc") or step.top_n is not None:
        dax += f"\nORDER BY {first} {'DESC' if descending else 'ASC'}"
    return dax, figures


def _band_filter(band_column: str, band: Band) -> str:
    col = column(band_column)
    conditions = []
    if band.min is not None:
        conditions.append(f"{col} >= {literal(float(band.min))}")
    if band.max is not None:
        conditions.append(f"{col} < {literal(float(band.max))}")
    return f"FILTER(ALL({col}), {' && '.join(conditions) or 'TRUE()'})"
