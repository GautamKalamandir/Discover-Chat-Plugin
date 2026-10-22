"""Builds the analysis of a /sales-summary as the user (ADR 0016, design §1).

discovery (which allowed models hold sales, and which tables each fills; admin pins applied)
  -> per sales model, REPORT_MODEL_CONCURRENCY at a time: latest day with data, then each of its
     tables: the server's first query, the agent's investigation loop, the table, its findings and
     action points; plus the trend and "no activity now" of its main sales measure.

Every query runs through `Executor` and `PowerBIService` as the user (G3, read-only DAX, recorded
against the report), at most SALES_REPORT_QUERY_CONCURRENCY at once across the build. Figures are
never added across models (ADR 0016 §9). The report content and PDF are assembled from these
sections (16d).
"""

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.executor import Executor
from app.agent.help import NO_SOURCES
from app.agent.models import parse_ref
from app.agent.planner import Planner
from app.authz.models import AuthorizedContext, ModelSummary
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.llm.base import LLMProvider
from app.powerbi.service import PowerBIService
from app.reports import facts
from app.reports.builder import (
    COULD_NOT_LOAD,
    DEV_DATA_NOTE,
    NO_DATA,
    NOTHING_LOADED,
    ProgressCallback,
    ReportBuildError,
    ReportProgress,
)
from app.reports.models import InactiveBreakdown, Trend
from app.reports.sales.content import ModelUsed, SalesReportContent, SalesSection
from app.reports.sales.discovery import SALES_MEASURES, Discovery, ModelRoles, SalesDiscovery
from app.reports.sales.lenses import TITLES, Lens
from app.reports.sales.loop import InvestigationLoop, Runner, executor_runner
from app.reports.sales.periods import SalesPeriods
from app.reports.sales.prompts import lens_goals, load_compact_prompt, load_sales_prompt
from app.reports.sales.seeds import seed_step
from app.reports.sales.tables import LensTable, build_table
from app.reports.sales.writer import merge_actions, write_bullets, write_findings
from app.semantic.user_schema import UserSchemaService

logger = logging.getLogger(__name__)

NO_SALES_MODELS = "None of your data sources hold sales data the daily report can use."
NO_FIELDS = "This data source has no fields for this table."
STOCK_MEASURES = ("stock_value", "stock_weight", "stock_turn")


@dataclasses.dataclass(frozen=True)
class SalesBuild:
    discovery: Discovery
    sections: list[SalesSection]


class SalesReportBuilder:
    def __init__(
        self,
        llm: LLMProvider,
        user_schemas: UserSchemaService,
        powerbi: PowerBIService,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        today: Callable[[], date] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._llm = llm
        self._clock = clock
        self._powerbi = powerbi
        self._settings = settings
        self._today = today or (lambda: datetime.now(settings.report_zone).date())
        planner = Planner(  # never used to repair: every report query is built by the server
            llm,
            fiscal_start_month=settings.fiscal_year_start_month,
            max_steps=settings.agent_max_query_steps,
        )
        self._executor = Executor(powerbi, planner, sessionmaker, settings, today=self._today)
        self._discovery = SalesDiscovery(
            llm,
            user_schemas,
            sessionmaker,
            tables_prompt=load_compact_prompt("sales_discovery_tables"),
            fields_prompt=load_compact_prompt("sales_discovery"),
        )
        self._goals = lens_goals(settings)
        self._loop_prompt = load_compact_prompt("sales_loop")
        self._findings_prompt = load_sales_prompt("sales_findings")
        self._bullets_prompt = load_sales_prompt("sales_bullets")

    async def build(
        self,
        authz: AuthorizedContext,
        report_id: uuid.UUID,
        *,
        user_name: str | None,
        progress: ProgressCallback | None = None,
    ) -> SalesReportContent:
        """The whole report: the analysis, then the summary bullets and the action list."""
        built = await self.build_sections(authz, report_id, progress=progress)
        if progress:
            await progress(ReportProgress("summarising"))
        bullets = await write_bullets(self._llm, self._bullets_prompt, built.sections)
        notes = [DEV_DATA_NOTE] if any("dev_synthetic" in s.sources for s in built.sections) else []
        if built.discovery.unreadable:
            names = ", ".join(m.name for m in built.discovery.unreadable)
            notes.append(f"Couldn't check these data sources right now: {names}.")
        return SalesReportContent(
            report_id=report_id,
            generated_at=self._clock(),
            timezone=self._settings.report_timezone,
            user_name=user_name,
            bullets=bullets,
            summary="\n".join(b.text for b in bullets),
            models_used=[
                ModelUsed(
                    dataset_id=r.model.dataset_id,
                    name=r.model.name,
                    lenses=list(r.lenses),
                    reason=r.reason,
                )
                for r in built.discovery.models
            ],
            sales_sections=built.sections,
            actions=merge_actions(built.sections),
            notes=notes,
        )

    async def build_sections(
        self,
        authz: AuthorizedContext,
        report_id: uuid.UUID,
        *,
        progress: ProgressCallback | None = None,
    ) -> SalesBuild:
        if not authz.allowed:
            raise ReportBuildError("no_models", NO_SOURCES)
        finished = 0
        total = 0

        async def report(event: ReportProgress) -> None:
            if progress:
                await progress(dataclasses.replace(event, done=finished, total=total))

        await report(ReportProgress("discovering"))
        found = await self._discovery.discover(authz)
        if not found.models:
            if found.unreadable:  # some couldn't be checked: say so, not "no sales data"
                raise ReportBuildError("all_models_failed", NOTHING_LOADED)
            raise ReportBuildError("no_sales_models", NO_SALES_MODELS)
        total = len(found.models)
        schemas = {r.model.dataset_id: r.schema for r in found.models}
        limit = asyncio.Semaphore(self._settings.sales_report_query_concurrency)
        run = executor_runner(self._executor, authz, report_id, schemas, limit)
        stock = [r for r in found.models if any(m in r.measures for m in STOCK_MEASURES)]
        models = asyncio.Semaphore(self._settings.report_model_concurrency)

        async def one(roles: ModelRoles) -> SalesSection:
            nonlocal finished
            async with models:
                section = await self._guarded(authz, roles, run, stock, report)
            finished += 1
            await report(ReportProgress("model_done", roles.model.name, section.status))
            return section

        sections = list(await asyncio.gather(*(one(r) for r in found.models)))
        if all(s.status == "failed" for s in sections):
            raise ReportBuildError("all_models_failed", NOTHING_LOADED)
        logger.info(
            "Sales report %s analysed: sections=%s",
            report_id,
            ",".join(s.status for s in sections),
        )
        return SalesBuild(found, sections)

    # --- one sales model ------------------------------------------------------------------------

    async def _guarded(
        self,
        authz: AuthorizedContext,
        roles: ModelRoles,
        run: Runner,
        stock: list[ModelRoles],
        progress: ProgressCallback,
    ) -> SalesSection:
        model = roles.model
        try:
            return await self._section(authz, roles, run, stock, progress)
        except asyncio.CancelledError:
            raise
        except AppError as exc:
            # Codes only: messages from Power BI or the LLM can carry data (Q18).
            logger.warning("Sales section for %s failed: %s", model.dataset_id, exc.code)
        except Exception:
            logger.exception("Sales section for %s failed", model.dataset_id)
        return _failed(model, COULD_NOT_LOAD.format(name=model.name))

    async def _section(
        self,
        authz: AuthorizedContext,
        roles: ModelRoles,
        run: Runner,
        stock: list[ModelRoles],
        progress: ProgressCallback,
    ) -> SalesSection:
        model = roles.model
        await progress(ReportProgress("reading_model", model.name))
        refreshed = await self._refreshed(authz, model.dataset_id)
        measure = next((roles.measures[m] for m in SALES_MEASURES if m in roles.measures), None)
        if roles.date_column is None or measure is None:  # pragma: no cover - discovery checks
            return SalesSection(
                dataset_id=model.dataset_id, name=model.name, status="no_data", message=NO_DATA
            )
        kpi = facts.ReportKpi("k1", _name(measure), measure, None, None)
        latest = await run(
            model.dataset_id,
            "Latest day with data",
            facts.latest_dates_dax(roles.date_column, kpi.expression, self._today()),
            max_rows=1,
        )
        day = facts.parse_latest_dates(latest.rows[0])[0] if latest.rows else None
        if day is None:
            return SalesSection(
                dataset_id=model.dataset_id,
                name=model.name,
                status="no_data",
                message=NO_DATA,
                refreshed_at=refreshed,
                sources=[latest.gateway],
            )
        periods = SalesPeriods(day, self._settings.fiscal_year_start_month)
        others = [s for s in stock if s.model.dataset_id != model.dataset_id]

        async def table(lens: Lens) -> LensTable:
            """One table; a failure here doesn't take the other tables down (design §12)."""
            await progress(ReportProgress(lens, model.name))
            try:
                return await self._table(lens, roles, periods, run, others)
            except asyncio.CancelledError:
                raise
            except AppError as exc:
                if exc.code is ErrorCode.MODEL_ACCESS_DENIED:
                    raise  # the whole data source stops
                logger.warning("Sales table %s for %s failed: %s", lens, model.dataset_id, exc.code)
            except Exception:
                logger.exception("Sales table %s for %s failed", lens, model.dataset_id)
            message = COULD_NOT_LOAD.format(name=model.name)
            return LensTable(lens=lens, title=TITLES[lens], status="failed", message=message)

        tables = list(await asyncio.gather(*(table(lens) for lens in roles.lenses)))
        if tables and all(t.status == "failed" for t in tables):
            return _failed(model, COULD_NOT_LOAD.format(name=model.name))
        trend = await self._trend(run, roles, kpi, day)
        inactive = await self._inactive(run, roles, kpi, day)
        return SalesSection(
            dataset_id=model.dataset_id,
            name=model.name,
            status="ok",
            data_date=day,
            refreshed_at=refreshed,
            tables=tables,
            trend=trend,
            inactive=inactive,
            sources=[latest.gateway],
            notes=list(roles.notes),
        )

    async def _table(
        self,
        lens: Lens,
        roles: ModelRoles,
        periods: SalesPeriods,
        run: Runner,
        stock: list[ModelRoles],
    ) -> LensTable:
        seed = seed_step(lens, roles)
        if seed is None:
            return LensTable(lens=lens, title=TITLES[lens], status="no_data", message=NO_FIELDS)
        loop = InvestigationLoop(
            self._llm,
            run,
            system_prompt=self._loop_prompt,
            goals=self._goals,
            budget=self._settings.sales_report_queries_per_table,
            max_rows=self._settings.powerbi_max_rows_limit,
            max_suggestions=self._settings.report_max_suggestions,
        )
        extra = stock if lens == "lob_growth" else []  # the stock check of degrowing LOBs
        result = await loop.investigate(lens, roles, periods, seed=seed, extra=extra)
        if not result.steps or result.steps[0].error:
            message = COULD_NOT_LOAD.format(name=roles.model.name)
            return LensTable(lens=lens, title=TITLES[lens], status="failed", message=message)
        built, derived = build_table(lens, result, roles, periods)
        built.facts = result.facts() + derived
        return await write_findings(
            self._llm,
            self._findings_prompt,
            built,
            result,
            derived,
            roles,
            periods,
            max_suggestions=self._settings.report_max_suggestions,
        )

    # --- supporting views (kept from Phase 15, ADR 0016 §7) -------------------------------------

    async def _trend(
        self, run: Runner, roles: ModelRoles, kpi: facts.ReportKpi, day: date
    ) -> Trend | None:
        count = self._settings.report_trend_days
        if roles.date_column is None:  # pragma: no cover - checked by the caller
            return None
        try:
            result = await run(
                roles.model.dataset_id,
                "Daily trend",
                facts.trend_dax(kpi, roles.date_column, day, count),
                max_rows=1,
            )
        except AppError as exc:
            if exc.code is ErrorCode.MODEL_ACCESS_DENIED:
                raise
            logger.warning("Sales trend failed (%s); left out", exc.code)
            return None
        return facts.parse_trend(kpi, result.rows[0] if result.rows else {}, day, count)

    async def _inactive(
        self, run: Runner, roles: ModelRoles, kpi: facts.ReportKpi, day: date
    ) -> list[InactiveBreakdown]:
        """Stores (or else LOBs) that sold in the lookback window but not on the latest day."""
        breakdown = roles.columns.get("store") or roles.columns.get("lob")
        if breakdown is None or roles.date_column is None:
            return []
        lookback = self._settings.report_inactive_lookback_days
        try:
            result = await run(
                roles.model.dataset_id,
                f"No activity now by {_name(breakdown)}",
                facts.inactive_dax([kpi], breakdown, roles.date_column, day, lookback),
                max_rows=self._settings.powerbi_max_rows_limit,
            )
        except AppError as exc:
            if exc.code is ErrorCode.MODEL_ACCESS_DENIED:
                raise
            logger.warning("Sales inactivity check failed (%s); skipped", exc.code)
            return []
        return facts.inactive_items(
            [kpi],
            result.rows,
            result.columns,
            by=_name(breakdown),
            lookback=lookback,
            truncated=result.truncated,
        )

    async def _refreshed(self, authz: AuthorizedContext, model_id: str) -> datetime | None:
        try:
            return await self._powerbi.last_refresh(authz, model_id=model_id)
        except AppError as exc:
            if exc.code is ErrorCode.MODEL_ACCESS_DENIED:
                raise
            return None  # best effort: the report says nothing about the refresh time


def _failed(model: ModelSummary, message: str) -> SalesSection:
    return SalesSection(
        dataset_id=model.dataset_id, name=model.name, status="failed", message=message
    )


def _name(ref: str) -> str:
    parsed = parse_ref(ref)
    return parsed[1] if parsed else ref.strip("[]")
