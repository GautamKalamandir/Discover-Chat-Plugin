"""Deterministic DAX from a validated plan step (template-first, Q13).

Shape: EVALUATE [TOPN(n,] SUMMARIZECOLUMNS(group-by columns, filter tables, "Alias", expr ...)
[, sort)] [ORDER BY ...]. Identifiers and literals are always escaped here, never by the LLM.
"""

from datetime import date

from app.agent.fiscal import resolve_period
from app.agent.models import Filter, PlanStep, Scalar, parse_ref
from app.core.errors import AppError, ErrorCode

_AGG = {
    "sum": "SUM",
    "average": "AVERAGE",
    "min": "MIN",
    "max": "MAX",
    "count": "COUNT",
    "distinctcount": "DISTINCTCOUNT",
}


def quote_table(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def quote_name(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def column(ref: str) -> str:
    parsed = parse_ref(ref)
    if parsed is None:
        raise AppError(
            ErrorCode.QUERY_REJECTED,
            "I couldn't build a query for that.",
            422,
            log_detail=f"Unparseable column reference {ref!r}",
        )
    table, name = parsed
    return f"{quote_table(table)}{quote_name(name)}"


def literal(value: Scalar) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return repr(value) if isinstance(value, float) else str(value)
    return '"' + str(value).replace('"', '""') + '"'


def _date(value: date) -> str:
    return f"DATE({value.year}, {value.month}, {value.day})"


def measure_alias(ref: str) -> str:
    parsed = parse_ref(ref)
    return parsed[1] if parsed else ref.strip("[]")


def _filter_table(flt: Filter) -> str:
    col = column(flt.column)
    if flt.op in ("=", "in"):
        values = ", ".join(literal(v) for v in flt.values)
        return f"TREATAS({{{values}}}, {col})"
    if flt.op == "between":
        if len(flt.values) != 2:
            raise AppError(
                ErrorCode.QUERY_REJECTED,
                "I couldn't build a query for that.",
                422,
                log_detail="between needs exactly two values",
            )
        low, high = (literal(v) for v in flt.values)
        return f"FILTER(ALL({col}), {col} >= {low} && {col} <= {high})"
    if flt.op == "<>":
        values = ", ".join(literal(v) for v in flt.values)
        return f"FILTER(ALL({col}), NOT ({col} IN {{{values}}}))"
    return f"FILTER(ALL({col}), {col} {flt.op} {literal(flt.values[0])})"


def build_dax(step: PlanStep, *, today: date, fiscal_start_month: int) -> str:
    args: list[str] = [column(g) for g in step.group_by]
    args += [_filter_table(f) for f in step.filters]
    if step.time is not None:
        start, end = resolve_period(step.time, today, fiscal_start_month)
        col = column(step.time.column)
        args.append(f"FILTER(ALL({col}), {col} >= {_date(start)} && {col} <= {_date(end)})")

    values: list[tuple[str, str]] = []
    for ref in step.measures:
        parsed = parse_ref(ref)
        name = parsed[1] if parsed else ref.strip("[]")
        values.append((name, quote_name(name)))
    for agg in step.aggregations:
        parsed = parse_ref(agg.column)
        alias = f"{agg.function.title()} of {parsed[1] if parsed else agg.column}"
        values.append((alias, f"{_AGG[agg.function]}({column(agg.column)})"))
    if not values:
        raise AppError(
            ErrorCode.QUERY_REJECTED,
            "I couldn't build a query for that.",
            422,
            log_detail="plan step has no measures or aggregations",
        )
    args += [f"{literal(alias)}, {expr}" for alias, expr in values]

    table = "SUMMARIZECOLUMNS(\n    " + ",\n    ".join(args) + "\n)"
    first_value = quote_name(values[0][0])
    descending = step.order != "value_asc"
    if step.top_n is not None:
        table = f"TOPN({step.top_n}, {table}, {first_value}, {'DESC' if descending else 'ASC'})"

    dax = f"EVALUATE\n{table}"
    if step.order == "group_asc" and step.group_by:
        dax += "\nORDER BY " + ", ".join(column(g) + " ASC" for g in step.group_by)
    elif step.order in ("value_desc", "value_asc") or step.top_n is not None:
        dax += f"\nORDER BY {first_value} {'DESC' if descending else 'ASC'}"
    return dax
