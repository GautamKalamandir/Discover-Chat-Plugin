"""What a /sales-summary report holds: `report.json` schema 3 (ADR 0016, design §8).

Schema-2 reports (`app.reports.models.SummaryReportContent`) built before /sales-summary stay
readable for their 7 days; `parse_report` picks the shape from the file.
"""

import json
import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.reports.models import InactiveBreakdown, Suggestion, SummaryReportContent, Trend
from app.reports.sales.lenses import Lens
from app.reports.sales.tables import LensTable, Tone

SCHEMA_VERSION = 3


class Bullet(BaseModel):
    """One line of the MIS summary. Its colour comes from the facts it cites (design §7)."""

    text: str
    tone: Tone
    emphasis: list[str] = Field(default_factory=list)  # phrases shown in bold
    evidence: list[str] = Field(default_factory=list)


class ModelUsed(BaseModel):
    dataset_id: str
    name: str
    lenses: list[Lens]
    reason: str = ""


class SalesAction(Suggestion):
    """An action point of one table, in the report's combined list."""

    lens: Lens


class SalesSection(BaseModel):
    """One sales data source: its tables, trend and "no activity now", never mixed with another."""

    dataset_id: str
    name: str
    status: Literal["ok", "failed", "no_data"]
    message: str | None = None
    data_date: date | None = None
    refreshed_at: datetime | None = None
    tables: list[LensTable] = Field(default_factory=list)
    trend: Trend | None = None
    inactive: list[InactiveBreakdown] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)  # gateways that answered
    notes: list[str] = Field(default_factory=list)


class SalesReportContent(BaseModel):
    schema_version: int = SCHEMA_VERSION
    kind: Literal["sales"] = "sales"
    report_id: uuid.UUID
    generated_at: datetime
    timezone: str
    user_name: str | None
    bullets: list[Bullet] = Field(default_factory=list)
    summary: str = ""  # the bullets as plain text (history headline, older views)
    models_used: list[ModelUsed] = Field(default_factory=list)
    sales_sections: list[SalesSection] = Field(default_factory=list)
    actions: list[SalesAction] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # Schema-2 fields, always empty here: a visual that predates schema 3 still opens the report.
    sections: list[Any] = Field(default_factory=list)
    overview: list[Any] = Field(default_factory=list)

    @property
    def suggestion_counts(self) -> dict[str, int]:
        counts = {"high": 0, "medium": 0, "low": 0}
        for action in self.actions:
            counts[action.priority] += 1
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
            for s in self.sales_sections
        ]


ReportContent = SummaryReportContent | SalesReportContent


def parse_report(raw: bytes | str) -> ReportContent:
    """A stored report.json, of either schema."""
    if json.loads(raw).get("kind") == "sales":
        return SalesReportContent.model_validate_json(raw)
    return SummaryReportContent.model_validate_json(raw)
