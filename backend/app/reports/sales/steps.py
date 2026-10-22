"""The query the agent asks for in the investigation loop (ADR 0016 §12, design §4).

The agent describes what it wants (measures, group-by columns, filters, periods, price bands); it
never writes DAX. Every step is checked against the user's own visible schema with the same rules as
chat (`plan_validator.check_step`, gate G2) before the server builds its DAX.
"""

import json
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import Aggregation, Filter, PlanStep
from app.agent.plan_validator import check_step, has_column, is_date_column
from app.reports.sales.periods import PeriodKey, is_monthly
from app.semantic.normalizer import NormalizedSchema

MAX_FIGURES = 48  # value columns in one query (measures x periods x months or bands)
MONTHS_PER_YEAR = 12


class Band(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=40, description="e.g. '25k to 50k'")
    min: float | None = Field(default=None, description="Inclusive; null for the first band")
    max: float | None = Field(default=None, description="Exclusive; null for the last band")


class LensStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(description="The data source to query, as listed")
    measures: list[str] = Field(default_factory=list, max_length=4)
    aggregations: list[Aggregation] = Field(default_factory=list, max_length=4)
    group_by: list[str] = Field(default_factory=list, max_length=3)
    filters: list[Filter] = Field(default_factory=list, max_length=10)
    periods: list[PeriodKey] = Field(min_length=1, max_length=8)
    band_column: str | None = Field(
        default=None, description="A numeric column to split into price bands (with bands)"
    )
    bands: list[Band] = Field(default_factory=list, max_length=12)
    top_n: int | None = Field(default=None, ge=1, le=200)
    order: Literal["value_desc", "value_asc", "group_asc"] | None = None
    why: str = Field(default="", max_length=160, description="What this query is for")

    def key(self) -> str:
        """Identity of the query (its purpose aside), to refuse running the same one twice."""
        return json.dumps(self.model_dump(exclude={"why"}), sort_keys=True, default=str)


def figure_count(step: LensStep) -> int:
    values = len(step.measures) + len(step.aggregations)
    per_value = sum(MONTHS_PER_YEAR if is_monthly(p) else 1 for p in step.periods)
    return values * per_value * max(len(step.bands), 1)


def check_lens_step(
    step: LensStep, schema: NormalizedSchema, date_column: str | None
) -> tuple[LensStep, list[str]]:
    """The step with measures resolved, and what is wrong with it (empty when it can run)."""
    plan = PlanStep(
        model_id=step.model_id,
        label=step.why[:120] or "report query",
        measures=step.measures,
        aggregations=step.aggregations,
        group_by=step.group_by,
        filters=step.filters,
    )
    checked, problems = check_step(plan, schema)
    if date_column is None:
        problems.append("this data source has no date column, so no period can be compared")
    for period in step.periods:
        if period.endswith("_ly") and period.removesuffix("_ly") not in step.periods:
            problems.append(f"{period} needs {period.removesuffix('_ly')} as well")
    if len(set(step.periods)) != len(step.periods):
        problems.append("a period is listed twice")
    problems += _band_problems(step, schema)
    if step.bands and any(is_monthly(p) for p in step.periods):
        problems.append("price bands can't be combined with month-by-month periods")
    if figure_count(step) > MAX_FIGURES:
        problems.append(
            f"{figure_count(step)} figures in one query; ask for at most {MAX_FIGURES} "
            "(fewer measures, periods or bands)"
        )
    return step.model_copy(update={"measures": checked.measures}), problems


def _band_problems(step: LensStep, schema: NormalizedSchema) -> list[str]:
    if not step.bands and step.band_column is None:
        return []
    if not step.bands or step.band_column is None:
        return ["price bands need both band_column and bands"]
    problems: list[str] = []
    if not has_column(step.band_column, schema) or is_date_column(step.band_column, schema):
        problems.append(f"band_column {step.band_column} is not a listed number column")
    bounds = [(b.min, b.max) for b in step.bands]
    for index, (low, high) in enumerate(bounds):
        if low is None and index != 0:
            problems.append("only the first band may have no lower limit")
        if high is None and index != len(bounds) - 1:
            problems.append("only the last band may have no upper limit")
        if low is not None and high is not None and low >= high:
            problems.append(f"band {step.bands[index].label!r} has min >= max")
    for (_, high), (low, _) in pairwise(bounds):
        if high is not None and low is not None and low < high:
            problems.append("bands must be in ascending order and must not overlap")
            break
    if len({b.label for b in step.bands}) != len(step.bands):
        problems.append("band labels must be different")
    return problems
