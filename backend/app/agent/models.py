"""The structured plan the LLM must produce (validated by pydantic, then by the server)."""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# "Table[Name]" or "'Table Name'[Name]"
_REF = re.compile(r"^\s*'?(?P<table>[^'\[\]]+?)'?\s*\[(?P<name>[^\]]+)\]\s*$")

Scalar = str | int | float | bool


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str = Field(description="Column as listed in the context, e.g. Product[LOB]")
    op: Literal["=", "in", "<>", "between", ">", ">=", "<", "<="] = "="
    values: list[Scalar] = Field(min_length=1, max_length=50)


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str = Field(description="A date column as listed in the context, e.g. Date[Date]")
    period: Literal[
        "current_fy",
        "last_fy",
        "fytd",
        "current_month",
        "last_month",
        "last_n_months",
        "current_year",
        "last_year",
        "explicit",
    ]
    n: int | None = Field(default=None, ge=1, le=60)
    start: date | None = None
    end: date | None = None


class Aggregation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str
    function: Literal["sum", "average", "min", "max", "count", "distinctcount"]


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    label: str = Field(max_length=120, description="Short description of what this step returns")
    measures: list[str] = Field(default_factory=list, max_length=8)
    aggregations: list[Aggregation] = Field(default_factory=list, max_length=8)
    group_by: list[str] = Field(default_factory=list, max_length=4)
    filters: list[Filter] = Field(default_factory=list, max_length=10)
    time: TimeRange | None = None
    top_n: int | None = Field(default=None, ge=1, le=1000)
    order: Literal["value_desc", "value_asc", "group_asc"] | None = None
    custom_dax: str | None = Field(
        default=None, description="Only when the question can't be expressed with the fields above"
    )


class ChangeAnalysis(BaseModel):
    """'Why did X change between two periods?' The server builds the queries and works out where
    the change came from; the LLM only chooses what to compare and which breakdowns to use."""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    label: str = Field(max_length=120, description="What is analysed, e.g. 'GOLD net sales'")
    measure: str | None = Field(
        default=None,
        description="A general measure (not one fixed to a year), e.g. Sales[Total Net Sales]",
    )
    aggregation: Aggregation | None = Field(default=None, description="Only if no measure fits")
    filters: list[Filter] = Field(default_factory=list, max_length=10)
    baseline: TimeRange = Field(description="The earlier / reference period")
    current: TimeRange = Field(description="The period being explained")
    drivers: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Up to 3 columns to break the change down by (e.g. store, category, month)",
    )
    basis: Literal["like_for_like", "full_period"] | None = Field(
        default=None,
        description="Only when the user said how to compare a period that is still running",
    )


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "clarify", "cannot_answer", "help"]
    help_topic: Literal["greeting", "capabilities", "data_sources", "out_of_scope"] | None = None
    clarification: str | None = Field(default=None, max_length=300)
    unresolved_terms: list[str] = Field(default_factory=list, max_length=10)
    steps: list[PlanStep] = Field(default_factory=list, max_length=8)
    combine: Literal["none", "compare"] = "none"
    change: ChangeAnalysis | None = Field(
        default=None, description="For 'why did X change / what drove it' questions, not steps"
    )


class RepairedDax(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dax: str


def parse_ref(ref: str) -> tuple[str, str] | None:
    """'Product[LOB]' -> ('Product', 'LOB'); None when not a qualified reference."""
    match = _REF.match(ref)
    if not match:
        return None
    return match.group("table").strip(), match.group("name").strip()
