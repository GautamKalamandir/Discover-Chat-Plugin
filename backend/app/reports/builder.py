"""Builds a /summary report as the user (ADR 0015 and amendment 1, design §2 and §9).

Per model the user may access (gate G1), up to REPORT_MODEL_CONCURRENCY at a time:
  visible schema + context -> the agent (a senior MIS executive at a jewellery brand) picks KPIs,
  their direction, a date column and breakdowns (validated against the user's schema; admin pins
  added) -> latest day with data -> every KPI against fixed baselines (one query) -> daily trend
  of the main KPI -> top and bottom performers by every breakdown -> "no activity now" items ->
  the agent's further changes to explain -> the agent writes the section and action points ->
  every number and every piece of evidence is checked, and the text is cleaned.
Then the at-a-glance overview and an executive summary. Every query runs through `Executor` and
`PowerBIService` (DAX validation, G3, the user's own token) and is recorded against the report.

The agent judges what matters, how urgent it is and whether a rise is good news; the server
computes and formats every figure and drops whatever it can't verify (no thresholds, no word
lists, nothing specific to one model).
"""

import asyncio
import dataclasses
import functools
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.answer import ungrounded_numbers
from app.agent.change import ChangePeriods, analyze_change, build_steps
from app.agent.context_builder import ContextBuilder, PlannerContext
from app.agent.executor import Executor, StepOutcome
from app.agent.help import NO_SOURCES
from app.agent.models import Aggregation, ChangeAnalysis, PlanStep, TimeRange, parse_ref
from app.agent.plan_validator import has_column, is_date_column, resolve_measure
from app.agent.planner import Planner, untrusted
from app.authz.models import AuthorizedContext, ModelSummary
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.db.repositories.registry import RegistryRepository
from app.llm.base import ChatMessage, LLMProvider
from app.powerbi.service import PowerBIService
from app.reports import facts
from app.reports.facts import Range, ReportKpi, ReportPeriods
from app.reports.models import (
    DriverBreakdown,
    DriverItem,
    FocusAnalysis,
    FocusPick,
    InactiveBreakdown,
    KpiComparison,
    KpiRow,
    ModelSection,
    OverviewRow,
    ReportFocus,
    ReportKpiPlan,
    ReportSectionText,
    ReportSummaryText,
    Suggestion,
    SummaryReportContent,
    Trend,
)
from app.reports.text import change_words, clean_text, compact_indian, format_indian, percent
from app.semantic.normalizer import NormalizedSchema
from app.semantic.retriever import SemanticRetriever
from app.semantic.user_schema import UserSchemaService

logger = logging.getLogger(__name__)

PROMPTS = Path(__file__).parent / "prompts"
# What the semantic index is searched for when building a section's context.
CONTEXT_QUERY = "key performance measures, targets and the date each record belongs to"
COULD_NOT_LOAD = "Couldn't load {name} data right now."
NO_USABLE_FIELDS = "This data source has no date and measures the daily report can use."
NO_DATA = "No data was found up to today."
NOTHING_LOADED = "I couldn't load any of your data right now. Please try again later."
DEV_DATA_NOTE = "Development data, not real figures."
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
TOP_PERFORMERS = 5
# Evidence ids for the facts that aren't single figures.
PERFORMERS, INACTIVE, TREND = "performers", "no_activity_now", "trend"


@dataclass(frozen=True)
class ReportProgress:
    """`stage`: starting | reading_model | choosing_kpis | querying | explaining | writing |
    model_done | summarising | rendering_pdf. `status` (model_done only): ok | failed | no_data.
    `done` / `total`: data sources finished so far, out of how many."""

    stage: str
    model_name: str | None = None
    status: str | None = None
    done: int | None = None
    total: int | None = None


ProgressCallback = Callable[[ReportProgress], Awaitable[None]]


class StepRunner(Protocol):
    async def __call__(self, step: PlanStep, *, max_rows: int) -> StepOutcome: ...


class ReportBuildError(AppError):
    """Nothing could be built (no models, or every model failed). `reason` is stored on the row."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(ErrorCode.REPORT_FAILED, message, 502, log_detail=f"report: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class _Plan:
    date_column: str
    kpis: list[ReportKpi]
    breakdowns: list[str]
    assumptions: list[str]


@dataclass(frozen=True)
class _Pin:
    label: str
    measure: str


def load_prompt(name: str) -> str:
    """A report prompt, preceded by the shared persona (senior MIS executive, jewellery brand)."""
    persona = (PROMPTS / "report_persona.md").read_text(encoding="utf-8")
    return persona + "\n" + (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


class ReportBuilder:
    def __init__(
        self,
        llm: LLMProvider,
        retriever: SemanticRetriever,
        user_schemas: UserSchemaService,
        powerbi: PowerBIService,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        today: Callable[[], date] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._llm = llm
        self._powerbi = powerbi
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._today = today or (lambda: datetime.now(settings.report_zone).date())
        self._clock = clock
        self._context = ContextBuilder(retriever, user_schemas, settings)
        planner = Planner(
            llm,
            fiscal_start_month=settings.fiscal_year_start_month,
            max_steps=settings.agent_max_query_steps,
        )  # only used to repair DAX that Power BI rejects
        self._executor = Executor(powerbi, planner, sessionmaker, settings, today=self._today)

    async def build(
        self,
        authz: AuthorizedContext,
        report_id: uuid.UUID,
        *,
        user_name: str | None,
        progress: ProgressCallback | None = None,
    ) -> SummaryReportContent:
        models = authz.allowed_models
        if not models:
            raise ReportBuildError("no_models", NO_SOURCES)
        limit = asyncio.Semaphore(self._settings.report_model_concurrency)
        finished = 0

        async def report(event: ReportProgress) -> None:
            if progress:
                await progress(dataclasses.replace(event, done=finished, total=len(models)))

        async def one(model: ModelSummary) -> ModelSection:
            nonlocal finished
            async with limit:
                section = await self._guarded(authz, report_id, model, report)
            finished += 1
            await report(ReportProgress("model_done", model.name, section.status))
            return section

        sections = list(await asyncio.gather(*(one(m) for m in models)))
        if all(s.status == "failed" for s in sections):
            raise ReportBuildError("all_models_failed", NOTHING_LOADED)
        await report(ReportProgress("summarising"))
        summary = await self._summary(sections)
        dev = any("dev_synthetic" in s.sources for s in sections)
        logger.info("Report %s built: sections=%s", report_id, ",".join(s.status for s in sections))
        return SummaryReportContent(
            report_id=report_id,
            generated_at=self._clock(),
            timezone=self._settings.report_timezone,
            user_name=user_name,
            summary=summary,
            sections=sections,
            notes=[DEV_DATA_NOTE] if dev else [],
            overview=[_overview(s) for s in sections],
        )

    # --- one model ------------------------------------------------------------------------------

    async def _guarded(
        self,
        authz: AuthorizedContext,
        report_id: uuid.UUID,
        model: ModelSummary,
        progress: ProgressCallback,
    ) -> ModelSection:
        try:
            return await self._section(authz, report_id, model, progress)
        except asyncio.CancelledError:
            raise
        except AppError as exc:
            # Codes only: messages from Power BI or the LLM can carry data (Q18).
            logger.warning("Report section for %s failed: %s", model.dataset_id, exc.code)
        except Exception:
            logger.exception("Report section for %s failed", model.dataset_id)
        return ModelSection(
            dataset_id=model.dataset_id,
            name=model.name,
            status="failed",
            message=COULD_NOT_LOAD.format(name=model.name),
        )

    async def _section(
        self,
        authz: AuthorizedContext,
        report_id: uuid.UUID,
        model: ModelSummary,
        progress: ProgressCallback,
    ) -> ModelSection:
        async def notify(stage: str) -> None:
            await progress(ReportProgress(stage, model.name))

        base = {"dataset_id": model.dataset_id, "name": model.name}
        await notify("reading_model")
        context = await self._context.build(authz, CONTEXT_QUERY, [model.dataset_id])
        schema = context.schemas.get(model.dataset_id)
        if schema is None:
            return ModelSection(
                **base, status="failed", message=COULD_NOT_LOAD.format(name=model.name)
            )

        await notify("choosing_kpis")
        plan = await self._choose_kpis(context, schema, await self._pins(model, schema))
        if plan is None:
            return ModelSection(**base, status="no_data", message=NO_USABLE_FIELDS)
        refreshed = await self._refreshed(authz, model.dataset_id)

        await notify("querying")
        run_step = functools.partial(self._run_step, authz, report_id, schema, context.text)

        async def query(label: str, dax: str, max_rows: int = 1) -> StepOutcome:
            step = PlanStep(model_id=model.dataset_id, label=label, custom_dax=dax)
            return await run_step(step, max_rows=max_rows)

        latest = await query(
            "Latest day with data",
            facts.latest_dates_dax(plan.date_column, plan.kpis[0].expression, self._today()),
        )
        day, previous = (
            facts.parse_latest_dates(latest.result.rows[0]) if latest.result.rows else (None, None)
        )
        if day is None:
            return ModelSection(
                **base,
                status="no_data",
                message=NO_DATA,
                refreshed_at=refreshed,
                sources=[latest.result.gateway],
            )
        periods = facts.report_periods(day, previous)
        values = await query(
            "KPIs against baselines", facts.kpi_values_dax(plan.kpis, plan.date_column, periods)
        )
        rows = facts.kpi_rows(
            plan.kpis, values.result.rows[0] if values.result.rows else {}, periods
        )
        shown = [r for r in rows if r.value is not None]
        missing = [r.label for r in rows if r.value is None]
        main = next((k for k in plan.kpis if any(r.id == k.id for r in shown)), plan.kpis[0])

        trend = await self._trend(query, plan, main, day)
        performers, performers_period = await self._performers(run_step, model, plan, main, periods)
        inactive = await self._inactive(query, plan, [k for k in plan.kpis if _has(k, shown)], day)

        await notify("explaining")
        analyses = await self._explain(run_step, model, plan, periods, shown, main)

        await notify("writing")
        headline, narrative, suggestions = await self._write(
            model,
            plan,
            periods,
            shown,
            missing,
            analyses,
            performers,
            performers_period,
            inactive,
            trend,
        )
        return ModelSection(
            **base,
            status="ok",
            data_date=day,
            refreshed_at=refreshed,
            kpis=shown,
            focus=analyses,
            headline=headline,
            narrative=narrative,
            suggestions=suggestions,
            assumptions=plan.assumptions,
            sources=sorted({latest.result.gateway, values.result.gateway}),
            trend=trend,
            performers=performers,
            performers_period=performers_period,
            inactive=inactive,
            missing_kpis=missing,
        )

    async def _run_step(
        self,
        authz: AuthorizedContext,
        report_id: uuid.UUID,
        schema: NormalizedSchema,
        context: str,
        step: PlanStep,
        *,
        max_rows: int,
    ) -> StepOutcome:
        # The server built this DAX: a failure is not handed to the LLM to rewrite (ADR 0016).
        return await self._executor.run_step(
            authz,
            None,
            step,
            schema,
            context,
            max_rows=max_rows,
            report_id=report_id,
            repair=False,
        )

    async def _pins(self, model: ModelSummary, schema: NormalizedSchema) -> list[_Pin]:
        """Admin pins the user can see (OLS: invisible pins are skipped without a word)."""
        async with self._sessionmaker() as session:
            entries = await RegistryRepository(session).list_kpi_pins(model.model_uuid)
        pins = []
        for entry in entries:
            measure = resolve_measure(entry.maps_to or "", schema)
            if measure is not None:
                pins.append(_Pin(entry.term[:80], measure))
        return pins

    async def _refreshed(self, authz: AuthorizedContext, model_id: str) -> datetime | None:
        try:
            return await self._powerbi.last_refresh(authz, model_id=model_id)
        except AppError as exc:
            if exc.code is ErrorCode.MODEL_ACCESS_DENIED:
                raise
            return None  # best effort: the report says nothing about the refresh time

    # --- KPI choice -----------------------------------------------------------------------------

    async def _choose_kpis(
        self, context: PlannerContext, schema: NormalizedSchema, pins: list[_Pin]
    ) -> _Plan | None:
        system = (
            load_prompt("report_kpis")
            .replace("{max_kpis}", str(self._settings.report_max_kpis))
            .replace("{max_drivers}", str(self._settings.report_max_drivers))
            .replace("{schema}", _schema(ReportKpiPlan))
        )
        pinned = "\n".join(f"- {p.label}: {p.measure}" for p in pins) or "(none)"
        messages = [
            ChatMessage("system", system),
            ChatMessage(
                "user",
                "\n".join(["CONTEXT", untrusted(context.text), "PINNED KPIS", untrusted(pinned)]),
            ),
        ]
        choice = await self._llm.complete_json(messages, ReportKpiPlan)
        if choice.date_column is None and not choice.kpis and not pins:
            return None  # the agent says this model has nothing to report on
        plan, problems = self._validate(choice, schema, pins)
        if problems:
            feedback = (
                "Your choice cannot be used: " + "; ".join(problems)[:1500] + ". Use only "
                "measures and columns listed in the CONTEXT, written exactly as listed."
            )
            choice = await self._llm.complete_json(
                [
                    *messages,
                    ChatMessage("assistant", choice.model_dump_json()),
                    ChatMessage("user", feedback),
                ],
                ReportKpiPlan,
            )
            plan, _ = self._validate(choice, schema, pins)  # what is still invalid is dropped
        return plan

    def _validate(
        self, choice: ReportKpiPlan, schema: NormalizedSchema, pins: list[_Pin]
    ) -> tuple[_Plan | None, list[str]]:
        problems: list[str] = []
        date_column = choice.date_column
        if not date_column or not (
            has_column(date_column, schema) and is_date_column(date_column, schema)
        ):
            problems.append(f"date_column {date_column!r} is not a listed date column")
            date_column = None

        # (label, measure, aggregation, target, higher_is_better)
        chosen: list[tuple[str, str | None, Aggregation | None, str | None, bool]] = [
            (p.label, p.measure, None, None, True) for p in pins
        ]
        for kpi in choice.kpis:
            target = resolve_measure(kpi.target, schema) if kpi.target else None
            if kpi.target and target is None:
                problems.append(f"unknown target measure {kpi.target}")
            if kpi.measure:
                measure = resolve_measure(kpi.measure, schema)
                if measure is None:
                    problems.append(f"unknown measure {kpi.measure}")
                    continue
                pinned = next((i for i, c in enumerate(chosen) if c[1] == measure), None)
                if pinned is not None:  # a pinned KPI: keep the admin's label, take the rest
                    chosen[pinned] = (
                        chosen[pinned][0],
                        measure,
                        None,
                        target,
                        kpi.higher_is_better,
                    )
                    continue
                chosen.append((kpi.label, measure, None, target, kpi.higher_is_better))
            elif kpi.aggregation and has_column(kpi.aggregation.column, schema):
                chosen.append((kpi.label, None, kpi.aggregation, target, kpi.higher_is_better))
            else:
                problems.append(f"KPI {kpi.label!r} has no listed measure or column")

        breakdowns: list[str] = []
        for ref in choice.breakdowns:
            if not has_column(ref, schema):
                problems.append(f"unknown column {ref}")
            elif is_date_column(ref, schema):
                problems.append(f"breakdown {ref} is a date column")
            elif ref not in breakdowns:
                breakdowns.append(ref)

        limit = max(self._settings.report_max_kpis, len(pins))  # pins are always kept
        kpis = [
            ReportKpi(f"k{i}", label, measure, aggregation, target, better)
            for i, (label, measure, aggregation, target, better) in enumerate(
                chosen[:limit], start=1
            )
        ]
        if date_column is None or not kpis:
            return None, problems or ["choose a date column and at least one KPI"]
        plan = _Plan(
            date_column=date_column,
            kpis=kpis,
            breakdowns=breakdowns[: self._settings.report_max_drivers],
            assumptions=choice.assumptions,
        )
        return plan, problems

    # --- standard analyses (always computed) ----------------------------------------------------

    async def _trend(
        self,
        query: Callable[..., Awaitable[StepOutcome]],
        plan: _Plan,
        main: ReportKpi,
        day: date,
    ) -> Trend | None:
        count = self._settings.report_trend_days
        try:
            outcome = await query(
                "Daily trend", facts.trend_dax(main, plan.date_column, day, count)
            )
        except AppError as exc:
            if exc.code is ErrorCode.MODEL_ACCESS_DENIED:
                raise
            logger.warning("Report trend failed (%s); left out", exc.code)
            return None
        row = outcome.result.rows[0] if outcome.result.rows else {}
        return facts.parse_trend(main, row, day, count)

    async def _performers(
        self,
        run_step: StepRunner,
        model: ModelSummary,
        plan: _Plan,
        main: ReportKpi,
        periods: ReportPeriods,
    ) -> tuple[list[DriverBreakdown], str | None]:
        """Where the main KPI moved against the same day last week, by every breakdown."""
        if not plan.breakdowns:
            return [], None
        baseline, current = (periods.week_ago, periods.week_ago), (periods.day, periods.day)
        result = await self._change(
            run_step, model, plan, main, baseline, current, plan.breakdowns, top=TOP_PERFORMERS
        )
        if result is None:
            return [], None
        label = f"{facts.range_label(current)} vs {facts.range_label(baseline)}"
        return _breakdowns(result), label

    async def _inactive(
        self,
        query: Callable[..., Awaitable[StepOutcome]],
        plan: _Plan,
        kpis: list[ReportKpi],
        day: date,
    ) -> list[InactiveBreakdown]:
        """Items with a value in the lookback window but none on the latest day (only for KPIs
        where activity is good news: returns going quiet isn't an alert)."""
        watched = [k for k in kpis if k.higher_is_better]
        lookback = self._settings.report_inactive_lookback_days
        found: list[InactiveBreakdown] = []
        if not watched:
            return found
        for breakdown in plan.breakdowns:
            try:
                outcome = await query(
                    f"No activity now by {breakdown}",
                    facts.inactive_dax(watched, breakdown, plan.date_column, day, lookback),
                    max_rows=self._settings.powerbi_max_rows_limit,
                )
            except AppError as exc:
                if exc.code is ErrorCode.MODEL_ACCESS_DENIED:
                    raise
                logger.warning("Report inactivity check failed (%s); skipped", exc.code)
                continue
            found += facts.inactive_items(
                watched,
                outcome.result.rows,
                outcome.result.columns,
                by=_column_name(breakdown),
                lookback=lookback,
                truncated=outcome.result.truncated,
            )
        return found

    async def _change(
        self,
        run_step: StepRunner,
        model: ModelSummary,
        plan: _Plan,
        kpi: ReportKpi,
        baseline: Range,
        current: Range,
        drivers: list[str],
        *,
        top: int,
    ) -> dict[str, Any] | None:
        """Explain-change analysis (ADR 0013) of one KPI between two ranges, by `drivers`."""

        def time(period: Range) -> TimeRange:
            return TimeRange(
                column=plan.date_column, period="explicit", start=period[0], end=period[1]
            )

        change = ChangeAnalysis(
            model_id=model.dataset_id,
            label=kpi.label,
            measure=kpi.measure,
            aggregation=kpi.aggregation,
            baseline=time(baseline),
            current=time(current),
            drivers=drivers,
        )
        change_periods = ChangePeriods(baseline, current)
        steps = build_steps(change, change_periods, max_steps=1 + len(drivers), whole_days=True)
        try:
            outcomes = [
                await run_step(step, max_rows=self._settings.powerbi_max_rows_limit)
                for step in steps
            ]
        except AppError as exc:
            if exc.code is ErrorCode.MODEL_ACCESS_DENIED:
                raise
            logger.warning("Report change analysis failed (%s); skipped", exc.code)
            return None
        return analyze_change(change, change_periods, outcomes, model.name, top=top).change or {}

    # --- the agent's further changes ------------------------------------------------------------

    async def _explain(
        self,
        run_step: StepRunner,
        model: ModelSummary,
        plan: _Plan,
        periods: ReportPeriods,
        rows: list[KpiRow],
        main: ReportKpi,
    ) -> list[FocusAnalysis]:
        if not plan.breakdowns or not rows:
            return []
        system = (
            load_prompt("report_focus")
            .replace("{max_focus}", str(self._settings.report_max_focus))
            .replace("{max_drivers}", str(self._settings.report_max_drivers))
            .replace("{schema}", _schema(ReportFocus))
        )
        kpi_facts = json.dumps([_kpi_fact(r) for r in rows], default=str)
        messages = [
            ChatMessage("system", system),
            ChatMessage(
                "user",
                "\n".join(
                    [
                        "KPI FACTS",
                        untrusted(kpi_facts),
                        "BREAKDOWNS",
                        untrusted("\n".join(plan.breakdowns)),
                    ]
                ),
            ),
        ]
        try:
            focus = await self._llm.complete_json(messages, ReportFocus)
        except AppError as exc:
            logger.warning("Report focus choice failed (%s); no changes explained", exc.code)
            return []

        analyses: list[FocusAnalysis] = []
        seen: set[tuple[str, str]] = {(main.id, "week_ago")}  # already the performers
        for pick in focus.focus:
            if len(analyses) >= self._settings.report_max_focus:
                break
            if (pick.kpi, pick.baseline) in seen:
                continue
            seen.add((pick.kpi, pick.baseline))
            analysis = await self._analyse(
                run_step, model, plan, periods, rows, pick, len(analyses) + 1
            )
            if analysis is not None:
                analyses.append(analysis)
        return analyses

    async def _analyse(
        self,
        run_step: StepRunner,
        model: ModelSummary,
        plan: _Plan,
        periods: ReportPeriods,
        rows: list[KpiRow],
        pick: FocusPick,
        number: int,
    ) -> FocusAnalysis | None:
        kpi = next((k for k in plan.kpis if k.id == pick.kpi), None)
        row = next((r for r in rows if r.id == pick.kpi), None)
        drivers = [b for b in dict.fromkeys(pick.breakdowns) if b in plan.breakdowns]
        drivers = drivers[: self._settings.report_max_drivers]
        if kpi is None or row is None or not drivers:
            return None
        compared = next((c for c in row.comparisons if c.baseline == pick.baseline), None)
        if compared is None or compared.change is None:
            return None  # nothing to explain without both values
        ranges = periods.ranges()
        if pick.baseline == "mtd":
            baseline, current = ranges["last_mtd"], ranges["mtd"]
        else:
            baseline, current = ranges[pick.baseline], ranges["day"]
        result = await self._change(run_step, model, plan, kpi, baseline, current, drivers, top=3)
        if result is None:
            return None
        return FocusAnalysis(
            id=f"f{number}",
            kpi_id=kpi.id,
            kpi_label=kpi.label,
            baseline=pick.baseline,
            baseline_period=facts.range_label(baseline),
            current_period=facts.range_label(current),
            baseline_value=float(result.get("baseline_value") or 0.0),
            current_value=float(result.get("current_value") or 0.0),
            change=float(result.get("change") or 0.0),
            percent_change=result.get("percent_change"),
            breakdowns=_breakdowns(result),
        )

    # --- writing --------------------------------------------------------------------------------

    async def _write(
        self,
        model: ModelSummary,
        plan: _Plan,
        periods: ReportPeriods,
        rows: list[KpiRow],
        missing: list[str],
        analyses: list[FocusAnalysis],
        performers: list[DriverBreakdown],
        performers_period: str | None,
        inactive: list[InactiveBreakdown],
        trend: Trend | None,
    ) -> tuple[str, str, list[Suggestion]]:
        better = {r.label: r.higher_is_better for r in rows}
        payload: dict[str, Any] = {
            "data_source": model.name,
            "latest_day_with_data": facts.day_label(periods.day),
            "kpis": [_kpi_fact(r) for r in rows],
        }
        if missing:
            payload["kpis_without_value_today"] = missing
        if performers:
            payload[PERFORMERS] = {
                "id": PERFORMERS,
                "compared": performers_period,
                "breakdowns": [
                    _breakdown_fact(b, better.get(rows[0].label, True)) for b in performers
                ],
            }
        if analyses:
            payload["explained_changes"] = [
                _focus_fact(a, better.get(a.kpi_label, True)) for a in analyses
            ]
        if inactive:
            payload[INACTIVE] = {"id": INACTIVE, "items": [_inactive_fact(b) for b in inactive]}
        if trend is not None:
            payload[TREND] = _trend_fact(trend)
        if plan.assumptions:
            payload["assumptions"] = plan.assumptions

        # The agent only sees formatted figures; grounding compares against the exact ones.
        # Dates written in the text are taken out before checking (`_grounded`), so day and month
        # numbers are never allowed as figures.
        allowed = _numbers(rows, analyses, performers, inactive, trend)
        evidence_ids = {r.id for r in rows} | {a.id for a in analyses}
        evidence_ids |= {c.fact_id for r in rows for c in r.comparisons}
        evidence_ids |= {
            key
            for key, present in ((PERFORMERS, performers), (INACTIVE, inactive), (TREND, trend))
            if present
        }

        template_headline = f"{model.name}: figures for {facts.day_label(periods.day)}."
        template_narrative = _template_narrative(rows)
        system = (
            load_prompt("report_section")
            .replace("{max_suggestions}", str(self._settings.report_max_suggestions))
            .replace("{schema}", _schema(ReportSectionText))
        )
        messages = [
            ChatMessage("system", system),
            ChatMessage("user", "FACTS\n" + untrusted(json.dumps(payload, default=str))),
        ]
        try:
            text = await self._llm.complete_json(messages, ReportSectionText)
        except AppError as exc:
            logger.warning("Report section text failed (%s); using the template", exc.code)
            return template_headline, template_narrative, []

        headline = clean_text(text.headline)
        narrative = clean_text(text.narrative)
        headline = headline if headline and _grounded(headline, allowed) else template_headline
        narrative = narrative if narrative and _grounded(narrative, allowed) else template_narrative
        suggestions: list[Suggestion] = []
        dropped = 0
        for item in text.suggestions:
            evidence = list(dict.fromkeys(item.evidence))
            cleaned = {
                field: clean_text(getattr(item, field))
                for field in ("title", "where", "owner", "reason", "notes")
            }
            words = " ".join(cleaned.values())
            if (
                not evidence
                or not set(evidence) <= evidence_ids
                or not cleaned["title"]
                or not cleaned["reason"]
                or not _grounded(words, allowed)
            ):
                dropped += 1
                continue
            suggestions.append(
                Suggestion(
                    **cleaned,
                    priority=item.priority,
                    urgency=item.urgency,
                    evidence=evidence,
                    model_name=model.name,
                )
            )
        suggestions.sort(key=lambda s: PRIORITY_ORDER[s.priority])
        if dropped:
            logger.info("Report section for %s: %d suggestions dropped", model.dataset_id, dropped)
        return headline, narrative, suggestions[: self._settings.report_max_suggestions]

    async def _summary(self, sections: list[ModelSection]) -> str:
        ok = [s for s in sections if s.status == "ok"]
        others = [s for s in sections if s.status != "ok"]
        template = " ".join(s.headline or "" for s in ok).strip()
        if others:
            template += " " + " ".join(f"{s.name}: {s.message}" for s in others)
        template = template.strip() or NOTHING_LOADED
        if not ok:
            return template
        payload = [
            {
                "data_source": s.name,
                "day": facts.day_label(s.data_date) if s.data_date else None,
                "headline": s.headline,
                "kpis": [_kpi_fact(r) for r in s.kpis[:3]],
                "high_priority_actions": [
                    {"action": x.title, "where": x.where, "owner": x.owner}
                    for x in s.suggestions
                    if x.priority == "high"
                ],
                "items_with_no_activity_now": sum(b.total for b in s.inactive),
            }
            for s in ok
        ] + [{"data_source": s.name, "status": s.message} for s in others]
        allowed = _numbers([r for s in ok for r in s.kpis[:3]], [], [], [], None)
        allowed += facts.numbers_in(payload)
        allowed += [float(len(sections)), float(len(ok))]
        messages = [
            ChatMessage("system", load_prompt("report_summary")),
            ChatMessage("user", "SECTIONS\n" + untrusted(json.dumps(payload, default=str))),
        ]
        try:
            text = clean_text((await self._llm.complete_json(messages, ReportSummaryText)).summary)
        except AppError as exc:
            logger.warning("Report summary failed (%s); using the template", exc.code)
            return template
        return text if text and _grounded(text, allowed) else template


# --- facts for the agent (formatted) ------------------------------------------------------------


def _good(change: float | None, higher_is_better: bool) -> bool | None:
    if not change:
        return None
    return (change > 0) == higher_is_better


def _kpi_fact(row: KpiRow) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "id": row.id,
        "kpi": row.label,
        "higher_is_better": row.higher_is_better,
        "display": format_indian(row.value),
        "compact": compact_indian(row.value),
        "comparisons": [_comparison_fact(c, row.higher_is_better) for c in row.comparisons],
    }
    if row.achievement_day is not None:
        fact["target_achievement_day"] = percent(row.achievement_day)
    if row.achievement_mtd is not None:
        fact["target_achievement_month_to_date"] = percent(row.achievement_mtd)
    return fact


def _comparison_fact(c: KpiComparison, higher_is_better: bool) -> dict[str, Any]:
    return {
        "id": c.fact_id,
        "compared_with": c.label,
        "current": format_indian(c.current_value),
        "baseline": format_indian(c.baseline_value),
        "change": change_words(c.change, c.percent_change),
        "good_news": _good(c.change, higher_is_better),
    }


def _items_fact(items: list[DriverItem], higher_is_better: bool) -> list[dict[str, Any]]:
    return [
        {
            "item": i.item,
            "change": change_words(i.change, i.percent_change),
            "share_of_total_change": percent(i.share_of_total_change),
            "good_news": _good(i.change, higher_is_better),
        }
        for i in items
    ]


def _breakdown_fact(b: DriverBreakdown, higher_is_better: bool) -> dict[str, Any]:
    return {
        "by": b.by,
        "biggest_increases": _items_fact(b.biggest_increases, higher_is_better),
        "biggest_decreases": _items_fact(b.biggest_decreases, higher_is_better),
    }


def _focus_fact(a: FocusAnalysis, higher_is_better: bool) -> dict[str, Any]:
    return {
        "id": a.id,
        "kpi": a.kpi_label,
        "compared": f"{a.current_period} vs {a.baseline_period}",
        "from": format_indian(a.baseline_value),
        "to": format_indian(a.current_value),
        "change": change_words(a.change, a.percent_change),
        "good_news": _good(a.change, higher_is_better),
        "breakdowns": [_breakdown_fact(b, higher_is_better) for b in a.breakdowns],
    }


def _inactive_fact(b: InactiveBreakdown) -> dict[str, Any]:
    return {
        "by": b.by,
        "kpi": b.kpi_label,
        "had_value_in_last_days": b.lookback_days,
        "items": [
            {"item": i.item, "value_in_those_days": format_indian(i.baseline_value)}
            for i in b.items
        ],
        "count": b.total,
    }


def _trend_fact(trend: Trend) -> dict[str, Any]:
    return {
        "id": TREND,
        "kpi": trend.kpi_label,
        "days": [
            {
                "day": facts.day_label(p.day),
                "value": compact_indian(p.value),
                "same_day_month_earlier": compact_indian(p.earlier_value),
            }
            for p in trend.days
        ],
    }


def _numbers(
    rows: list[KpiRow],
    analyses: list[FocusAnalysis],
    performers: list[DriverBreakdown],
    inactive: list[InactiveBreakdown],
    trend: Trend | None,
) -> list[float]:
    """Every exact figure a grounded text may mention (in any of its formatted forms)."""
    nodes: list[Any] = [r.model_dump() for r in rows]
    nodes += [a.model_dump() for a in analyses] + [b.model_dump() for b in performers]
    nodes += [b.model_dump() for b in inactive]
    if trend is not None:
        nodes.append([[p.value, p.earlier_value] for p in trend.days])
    return facts.numbers_in(nodes)


# --- helpers ------------------------------------------------------------------------------------


def _overview(section: ModelSection) -> OverviewRow:
    row = OverviewRow(
        dataset_id=section.dataset_id,
        name=section.name,
        status=section.status,
        data_date=section.data_date,
    )
    if section.status != "ok" or not section.kpis:
        return row.model_copy(update={"alert": section.message})
    main = section.kpis[0]
    compared = next(
        (c for c in main.comparisons if c.baseline in ("prev_day", "week_ago") and c.change),
        None,
    )
    high = next((s for s in section.suggestions if s.priority == "high"), None)
    silent = sum(b.total for b in section.inactive)
    alert = (
        high.title
        if high
        else f"{silent} item(s) with no activity on the latest day"
        if silent
        else None
    )
    return row.model_copy(
        update={
            "kpi_label": main.label,
            "value": main.value,
            "change_percent": compared.percent_change if compared else None,
            "good": _good(compared.change, main.higher_is_better) if compared else None,
            "alert": alert,
        }
    )


def _breakdowns(result: dict[str, Any]) -> list[DriverBreakdown]:
    return [
        DriverBreakdown(
            by=str(b["by"]),
            biggest_decreases=[DriverItem(**i) for i in b["biggest_decreases"]],
            biggest_increases=[DriverItem(**i) for i in b["biggest_increases"]],
        )
        for b in result.get("breakdowns", [])
    ]


def _has(kpi: ReportKpi, rows: list[KpiRow]) -> bool:
    return any(r.id == kpi.id for r in rows)


def _column_name(ref: str) -> str:
    return (parse_ref(ref) or ("", ref))[1]


def _grounded(text: str, allowed: list[float]) -> bool:
    return not ungrounded_numbers(facts.strip_dates(text), allowed)


def _schema(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), separators=(",", ":"))


def _template_narrative(rows: list[KpiRow]) -> str:
    lines = []
    for row in rows:
        changes = [
            f"{change_words(c.change, c.percent_change)} {c.label}"
            for c in row.comparisons
            if c.change is not None
        ]
        detail = f"; {'; '.join(changes)}" if changes else ""
        lines.append(f"- {row.label}: {format_indian(row.value)}{detail}")
    return "\n".join(lines)
