"""Stage 5: map filter words to the exact stored values (Fabric IQ ValueSearch, as the user).

Never blocks the answer: if value search is unavailable or finds nothing for the column, the
user's wording is kept (Power BI returns no rows for a non-matching value, which the answer states).
"""

import logging
from typing import Any

from app.agent.models import PlanStep, QueryPlan, parse_ref
from app.authz.models import AuthorizedContext
from app.core.errors import AppError
from app.powerbi.service import PowerBIService

logger = logging.getLogger(__name__)


def _matches(payload: Any) -> list[tuple[str, str]]:
    """(column, value) pairs from a ValueSearch payload, whatever its exact nesting.

    Spike S3 shape: {"Results": {"<term>": [{"Table", "Column", "Value", "Score"}]}}, where
    Column is the bare column name; it's qualified with its Table here."""
    found: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            lowered = {str(k).lower(): v for k, v in node.items()}
            column, value = lowered.get("column"), lowered.get("value")
            table = lowered.get("table")
            if isinstance(column, str) and isinstance(table, str) and parse_ref(column) is None:
                column = f"'{table}'[{column}]"
            if isinstance(column, str) and isinstance(value, str | int | float):
                found.append((column, str(value)))
            for child in node.values():
                if isinstance(child, list | dict):
                    walk(child)

    walk(payload)
    return found


HINTS_PER_TERM = 3


def value_hints(payload: Any) -> list[str]:
    """Readable "word -> column = stored value" lines from a ValueSearch payload, best matches
    first, so the planner can place words it didn't recognise (typos like "surar" -> SURAT)."""
    results = payload.get("Results") if isinstance(payload, dict) else None
    if not isinstance(results, dict):
        return []
    hints: list[str] = []
    for term, matches in results.items():
        if not isinstance(matches, list):
            continue
        scored = []
        for match in matches:
            if not isinstance(match, dict):
                continue
            table, column, value = match.get("Table"), match.get("Column"), match.get("Value")
            score = match.get("Score")
            if isinstance(table, str) and isinstance(column, str) and value is not None:
                number = score if isinstance(score, int | float) else 0.0
                scored.append((number, f"'{table}'[{column}]", str(value)))
        scored.sort(key=lambda s: -s[0])
        for score, column_ref, value in scored[:HINTS_PER_TERM]:
            hints.append(f'"{term}": {column_ref} = "{value}" (match score {score:.2f})')
    return hints


def _same_column(a: str, b: str) -> bool:
    pa, pb = parse_ref(a), parse_ref(b)
    if pa is None or pb is None:
        return False
    return (pa[0].lower(), pa[1].lower()) == (pb[0].lower(), pb[1].lower())


async def resolve_values(
    plan: QueryPlan, authz: AuthorizedContext, powerbi: PowerBIService
) -> QueryPlan:
    steps = [await _resolve_step(step, authz, powerbi) for step in plan.steps]
    return plan.model_copy(update={"steps": steps})


async def _resolve_step(
    step: PlanStep, authz: AuthorizedContext, powerbi: PowerBIService
) -> PlanStep:
    terms = sorted(
        {
            str(v)
            for f in step.filters
            if f.op in ("=", "in", "<>")
            for v in f.values
            if isinstance(v, str)
        }
    )
    if not terms:
        return step
    try:
        payload = (await powerbi.search_values(authz, model_id=step.model_id, terms=terms)).data
    except AppError as exc:
        logger.info("Value search unavailable for %s: %s", step.model_id, exc.code)
        return step
    matches = _matches(payload)
    filters = []
    for flt in step.filters:
        values = list(flt.values)
        for i, value in enumerate(values):
            if not isinstance(value, str):
                continue
            for column, canonical in matches:
                if _same_column(column, flt.column) and canonical.lower() == value.lower():
                    values[i] = canonical
                    break
        filters.append(flt.model_copy(update={"values": values}))
    return step.model_copy(update={"filters": filters})
