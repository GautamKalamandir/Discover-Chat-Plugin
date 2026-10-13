"""Shapes of a /summary report (ADR 0015).

LLM outputs (`ReportKpiPlan`, `ReportFocus`, `ReportSectionText`, `ReportSummaryText`) are
validated by pydantic and then by the server; the LLM never chooses a model, never writes DAX and
never computes a number. `SummaryReportContent` is what `report.json` holds and the visual and the
PDF show.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.models import Aggregation

Priority = Literal["high", "medium", "low"]
# The comparisons the server computes for every KPI (design §2.1). Targets aren't explained by
# breakdowns, so a focus may only pick one of the first three.
Baseline = Literal["prev_day", "week_ago", "mtd", "target_day", "target_mtd"]
FocusBaseline = Literal["prev_day", "week_ago", "mtd"]
SectionStatus = Literal["ok", "failed", "no_data"]

Urgency = Literal["today", "this_week", "this_month"]

# 2: charts, direction, "no activity now", suggestion where/owner/urgency, overview (amendment 1).
SCHEMA_VERSION = 2


# --- LLM outputs ---------------------------------------------------------------------------------


class KpiChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80, description="Short name shown in the report")
    measure: str | None = Field(default=None, description="A listed measure, Table[Measure]")
    aggregation: Aggregation | None = Field(default=None, description="Only if no measure fits")
    target: str | None = Field(
        default=None, description="A listed measure holding this KPI's target or budget, if any"
    )
    higher_is_better: bool = Field(
        default=True, description="false when a rise is bad news (e.g. returns, discounts, costs)"
    )


class ReportKpiPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_column: str | None = Field(
        description="Listed date column that dates the data, Table[Column]; null if none"
    )
    kpis: list[KpiChoice] = Field(default_factory=list, max_length=12)
    breakdowns: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Listed columns that best explain changes (never a date column)",
    )
    assumptions: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("assumptions")
    @classmethod
    def _short(cls, value: list[str]) -> list[str]:
        return [a.strip()[:160] for a in value if a.strip()][:3]


class FocusPick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kpi: str = Field(description="A KPI id from KPI FACTS, e.g. k1")
    baseline: FocusBaseline
    breakdowns: list[str] = Field(default_factory=list, max_length=6)


class ReportFocus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    focus: list[FocusPick] = Field(default_factory=list, max_length=6)


class SuggestionText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: Priority
    title: str = Field(min_length=1, max_length=120, description="The action to take")
    where: str = Field(
        default="", max_length=160, description="Store, region, category or item, from the data"
    )
    owner: str = Field(default="", max_length=80, description="The function that should act")
    urgency: Urgency = "this_week"
    reason: str = Field(min_length=1, max_length=600)
    notes: str = Field(default="", max_length=600)
    evidence: list[str] = Field(default_factory=list, max_length=8)


class ReportSectionText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(min_length=1, max_length=200)
    narrative: str = Field(min_length=1, max_length=1500)
    suggestions: list[SuggestionText] = Field(default_factory=list, max_length=12)


class ReportSummaryText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1500)


# --- report.json ---------------------------------------------------------------------------------


class KpiComparison(BaseModel):
    fact_id: str
    baseline: Baseline
    label: str  # e.g. "vs previous day (11 Oct 2026)"
    current_value: float | None
    baseline_value: float | None
    change: float | None
    percent_change: float | None


class KpiRow(BaseModel):
    id: str
    label: str
    value: float | None
    comparisons: list[KpiComparison] = Field(default_factory=list)
    higher_is_better: bool = True
    # Actual / target x 100, where the KPI has a target.
    achievement_day: float | None = None
    achievement_mtd: float | None = None


class DriverItem(BaseModel):
    item: str
    baseline_value: float
    current_value: float
    change: float
    percent_change: float | None
    share_of_total_change: float | None


class DriverBreakdown(BaseModel):
    by: str
    biggest_decreases: list[DriverItem] = Field(default_factory=list)
    biggest_increases: list[DriverItem] = Field(default_factory=list)


class FocusAnalysis(BaseModel):
    id: str  # f1, f2 ...
    kpi_id: str
    kpi_label: str
    baseline: FocusBaseline
    baseline_period: str
    current_period: str
    baseline_value: float
    current_value: float
    change: float
    percent_change: float | None
    breakdowns: list[DriverBreakdown] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Suggestion(SuggestionText):
    model_name: str


class TrendPoint(BaseModel):
    day: date
    value: float | None
    earlier_value: float | None = None  # same day one month earlier


class Trend(BaseModel):
    kpi_label: str
    higher_is_better: bool = True
    days: list[TrendPoint] = Field(default_factory=list)


class InactiveItem(BaseModel):
    item: str
    baseline_value: float  # total over the lookback window


class InactiveBreakdown(BaseModel):
    """Items with a value in the lookback window but none on the latest day."""

    by: str
    kpi_label: str
    lookback_days: int
    items: list[InactiveItem] = Field(default_factory=list)
    total: int = 0  # all such items (only the largest are listed)
    truncated: bool = False  # the breakdown had more items than one query returns


class OverviewRow(BaseModel):
    """One line of "All data sources at a glance"."""

    dataset_id: str
    name: str
    status: SectionStatus
    data_date: date | None = None
    kpi_label: str | None = None
    value: float | None = None
    change_percent: float | None = None  # vs the previous day with data
    good: bool | None = None  # the change is good news for this KPI
    alert: str | None = None


class ModelSection(BaseModel):
    dataset_id: str
    name: str
    status: SectionStatus
    message: str | None = None
    data_date: date | None = None
    refreshed_at: datetime | None = None
    kpis: list[KpiRow] = Field(default_factory=list)
    focus: list[FocusAnalysis] = Field(default_factory=list)
    headline: str | None = None
    narrative: str | None = None
    suggestions: list[Suggestion] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)  # gateways that answered
    trend: Trend | None = None
    performers: list[DriverBreakdown] = Field(default_factory=list)
    performers_period: str | None = None  # e.g. "12 Oct 2026 vs 05 Oct 2026"
    inactive: list[InactiveBreakdown] = Field(default_factory=list)
    missing_kpis: list[str] = Field(default_factory=list)  # no value on the latest day


class SummaryReportContent(BaseModel):
    schema_version: int = SCHEMA_VERSION
    report_id: uuid.UUID
    generated_at: datetime
    timezone: str
    user_name: str | None
    summary: str
    sections: list[ModelSection]
    notes: list[str] = Field(default_factory=list)
    overview: list[OverviewRow] = Field(default_factory=list)

    @property
    def suggestion_counts(self) -> dict[str, int]:
        counts = {"high": 0, "medium": 0, "low": 0}
        for section in self.sections:
            for suggestion in section.suggestions:
                counts[suggestion.priority] += 1
        return counts

    def model_status(self) -> list[dict[str, object]]:
        """What `summary_reports.models` keeps for the history list."""
        return [
            {
                "dataset_id": s.dataset_id,
                "name": s.name,
                "status": s.status,
                "data_date": s.data_date.isoformat() if s.data_date else None,
                "refreshed_at": s.refreshed_at.isoformat() if s.refreshed_at else None,
            }
            for s in self.sections
        ]
