"""Which of the user's models hold sales data, and which report tables each can fill (ADR 0016 §2,
design §3; two steps since amendment 1, to keep every request small).

1. Catalog: every table of every model, compact (its measures and a sample of its columns). The
   agent says which models hold sales data and which of their tables matter.
2. Fields: the full column list of just those tables. The agent maps the fields to roles (LOB,
   sub-category, store, price, sales and stock measures) and the report tables each model fills.

The agent judges from each model's visible schema (OLS applied), descriptions and AI instructions;
the server keeps only fields the user can see, and only tables whose needs are met. Admin pins
(`semantic_models.report_lenses`) override the agent: `true` always uses the model for a table,
`false` never. Nothing here names a model, measure or column.
"""

import asyncio
import dataclasses
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.plan_validator import has_column, is_date_column, resolve_measure
from app.agent.planner import untrusted
from app.authz.models import AuthorizedContext, ModelSummary
from app.core.errors import AppError
from app.db.models import SemanticModel
from app.llm.base import ChatMessage, LLMProvider
from app.llm.budget import call_limits
from app.reports.sales.lenses import LENSES, Lens
from app.semantic.normalizer import NormalizedSchema, SchemaObject
from app.semantic.user_schema import UserSchemaService

logger = logging.getLogger(__name__)

MODELS_PER_CALL = 10
MAX_MODEL_CHARS = 12000  # one model's field listing (about 4k tokens)
BATCH_CHARS = 12000  # listings sent in one call: each request stays under 8k tokens
MAX_TABLES = 8  # tables the agent may pick per model
CATALOG_COLUMNS = 10  # sample of columns per table in the catalog
MAX_DESCRIPTION = 80
# Answer room per call (ADR 0016 amendment 1): requests fit free per-minute limits.
TABLES_OUTPUT_TOKENS = 900
FIELDS_OUTPUT_TOKENS = 1400
SCHEMA_CONCURRENCY = 4

# Roles: lob | subcategory | store | item | price_band | price (columns), and
# sales_value | sales_weight | sales_qty | stock_value | stock_weight | stock_turn (measures).
ColumnRole = str
MeasureRole = str
SALES_MEASURES = ("sales_value", "sales_weight", "sales_qty")
STOCK_MEASURES = ("stock_value", "stock_weight")


# --- the agent's answer ---------------------------------------------------------------------------


class RoleColumns(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lob: str | None = None
    subcategory: str | None = None
    store: str | None = None
    item: str | None = None
    price_band: str | None = None
    price: str | None = None


class RoleMeasures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sales_value: str | None = None
    sales_weight: str | None = None
    sales_qty: str | None = None
    stock_value: str | None = None
    stock_weight: str | None = None
    stock_turn: str | None = None


class ModelRoleChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(description="The data source's id, as in DATA SOURCES")
    is_sales: bool = False
    lenses: list[Lens] = Field(default_factory=list, max_length=len(LENSES))
    date_column: str | None = None
    columns: RoleColumns = Field(default_factory=RoleColumns)
    measures: RoleMeasures = Field(default_factory=RoleMeasures)
    lower_is_better: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(default="", max_length=200)


class DiscoveryChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[ModelRoleChoice] = Field(default_factory=list, max_length=MODELS_PER_CALL)


class TablePick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    is_sales: bool = False
    tables: list[str] = Field(default_factory=list, max_length=MAX_TABLES)
    reason: str = Field(default="", max_length=200)


class TablesChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[TablePick] = Field(default_factory=list, max_length=MODELS_PER_CALL)


# --- what the report uses -------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelRoles:
    """A sales model and the tables it fills, with only fields the user can see."""

    model: ModelSummary
    schema: NormalizedSchema
    lenses: tuple[Lens, ...]
    date_column: str | None
    columns: dict[ColumnRole, str]
    measures: dict[MeasureRole, str]
    lower_is_better: frozenset[str]
    reason: str
    notes: tuple[str, ...] = ()  # e.g. a table an admin pinned that the model can't fill
    tables: tuple[str, ...] = ()  # the model's tables that matter (empty: all)

    def higher_is_better(self, measure: str) -> bool:
        return measure not in self.lower_is_better


@dataclass(frozen=True)
class Discovery:
    models: list[ModelRoles]  # sales models with at least one table, in the user's model order
    skipped: list[ModelSummary]  # not sales data (left out of the report, ADR 0016 §3)
    unreadable: list[ModelSummary]  # schema or agent unavailable right now


class SalesDiscovery:
    def __init__(
        self,
        llm: LLMProvider,
        user_schemas: UserSchemaService,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        tables_prompt: str,
        fields_prompt: str,
    ) -> None:
        self._llm = llm
        self._user_schemas = user_schemas
        self._sessionmaker = sessionmaker
        self._tables_system = tables_prompt.replace("{max_tables}", str(MAX_TABLES)).replace(
            "{schema}", json.dumps(TablesChoice.model_json_schema(), separators=(",", ":"))
        )
        self._system = fields_prompt.replace(
            "{schema}", json.dumps(DiscoveryChoice.model_json_schema(), separators=(",", ":"))
        )

    async def discover(self, authz: AuthorizedContext) -> Discovery:
        models = authz.allowed_models
        schemas = await self._schemas(authz, models)
        pins = await self._pins([m.model_uuid for m in models])
        readable = [m for m in models if m.dataset_id in schemas]
        unreadable = [m for m in models if m.dataset_id not in schemas]

        # Step 1: which models hold sales data, and which of their tables matter.
        picks: dict[str, TablePick] = {}
        for batch in _batches(readable, lambda m: len(table_catalog(m, schemas[m.dataset_id]))):
            try:
                picks |= await self._pick_tables(batch, schemas)
            except AppError as exc:
                # Codes only: LLM messages can quote model content (Q18).
                logger.warning("Sales discovery failed for %d models: %s", len(batch), exc.code)
                unreadable += batch
        skipped: list[ModelSummary] = []
        focus: dict[str, tuple[str, ...]] = {}
        for model in readable:
            pick = picks.get(model.dataset_id)
            if pick is None:
                continue  # its batch failed
            pinned = any(pins.get(model.model_uuid, {}).values())
            if not pick.is_sales and not pinned:
                logger.info("Sales discovery: %s is not sales data", model.dataset_id)
                skipped.append(model)
                continue
            focus[model.dataset_id] = _known_tables(pick.tables, schemas[model.dataset_id])

        # Step 2: the fields of those tables, mapped to roles.
        candidates = [m for m in readable if m.dataset_id in focus]
        choices: dict[str, ModelRoleChoice] = {}
        judged: list[ModelSummary] = []

        def size(model: ModelSummary) -> int:
            return len(model_block(model, schemas[model.dataset_id], focus[model.dataset_id]))

        for batch in _batches(candidates, size):
            try:
                choices |= await self._ask(batch, schemas, focus)
                judged += batch
            except AppError as exc:
                logger.warning("Sales discovery failed for %d models: %s", len(batch), exc.code)
                unreadable += batch

        found: list[ModelRoles] = []
        for model in judged:
            choice = choices.get(model.dataset_id) or ModelRoleChoice(dataset_id=model.dataset_id)
            roles = _roles(model, schemas[model.dataset_id], choice, pins.get(model.model_uuid, {}))
            roles = dataclasses.replace(roles, tables=focus[model.dataset_id])
            _log_decision(model, choice, roles)
            if roles.lenses:
                found.append(roles)
            else:
                skipped.append(model)
        logger.info(
            "Sales discovery: %d sales models, %d skipped, %d unreadable",
            len(found),
            len(skipped),
            len(unreadable),
        )
        order = {m.dataset_id: i for i, m in enumerate(models)}
        return Discovery(
            models=found,
            skipped=skipped,
            unreadable=sorted(unreadable, key=lambda m: order[m.dataset_id]),
        )

    async def _schemas(
        self, authz: AuthorizedContext, models: Sequence[ModelSummary]
    ) -> dict[str, NormalizedSchema]:
        """The models whose visible schema could be read (and isn't empty)."""
        limit = asyncio.Semaphore(SCHEMA_CONCURRENCY)

        async def one(model: ModelSummary) -> tuple[str, NormalizedSchema | None]:
            async with limit:
                schema = await self._user_schemas.visible(authz, model.dataset_id)
            return model.dataset_id, schema if schema is not None and schema.objects else None

        found = await asyncio.gather(*(one(m) for m in models))
        return {dataset_id: schema for dataset_id, schema in found if schema is not None}

    async def _pins(self, model_uuids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, bool]]:
        if not model_uuids:
            return {}
        async with self._sessionmaker() as session:
            rows = await session.execute(
                select(SemanticModel.id, SemanticModel.report_lenses).where(
                    SemanticModel.id.in_(model_uuids)
                )
            )
            return {model_id: dict(pins or {}) for model_id, pins in rows.all()}

    async def _pick_tables(
        self, batch: list[ModelSummary], schemas: dict[str, NormalizedSchema]
    ) -> dict[str, TablePick]:
        catalogs = "\n\n".join(table_catalog(m, schemas[m.dataset_id]) for m in batch)
        messages = [
            ChatMessage("system", self._tables_system),
            ChatMessage("user", "DATA SOURCES\n" + untrusted(catalogs)),
        ]
        with call_limits(TABLES_OUTPUT_TOKENS, "low"):
            answer = await self._llm.complete_json(messages, TablesChoice)
        wanted = {m.dataset_id for m in batch}
        return {p.dataset_id: p for p in answer.models if p.dataset_id in wanted}

    async def _ask(
        self,
        batch: list[ModelSummary],
        schemas: dict[str, NormalizedSchema],
        focus: dict[str, tuple[str, ...]],
    ) -> dict[str, ModelRoleChoice]:
        blocks = "\n\n".join(
            model_block(m, schemas[m.dataset_id], focus[m.dataset_id]) for m in batch
        )
        messages = [
            ChatMessage("system", self._system),
            ChatMessage("user", "DATA SOURCES\n" + untrusted(blocks)),
        ]
        with call_limits(FIELDS_OUTPUT_TOKENS, "low"):
            answer = await self._llm.complete_json(messages, DiscoveryChoice)
        choices = _by_model(answer, batch)
        problems = [
            f"{dataset_id}: {problem}"
            for dataset_id, choice in choices.items()
            for problem in _problems(choice, schemas[dataset_id])
        ]
        if not problems:
            return choices
        feedback = (
            "Some fields cannot be used: " + "; ".join(problems)[:1500] + ". Use only fields "
            "listed in that data source's block, written exactly as listed."
        )
        with call_limits(FIELDS_OUTPUT_TOKENS, "low"):
            answer = await self._llm.complete_json(
                [
                    *messages,
                    ChatMessage("assistant", answer.model_dump_json()),
                    ChatMessage("user", feedback),
                ],
                DiscoveryChoice,
            )
        return choices | _by_model(answer, batch)  # what is still invalid is dropped later


def _batches(
    models: list[ModelSummary], size: Callable[[ModelSummary], int]
) -> list[list[ModelSummary]]:
    """Up to MODELS_PER_CALL models per call, and no more listings than BATCH_CHARS."""
    batches: list[list[ModelSummary]] = []
    total = 0
    for model in models:
        length = size(model)
        if not batches or len(batches[-1]) >= MODELS_PER_CALL or total + length > BATCH_CHARS:
            batches.append([])
            total = 0
        batches[-1].append(model)
        total += length
    return batches


def _known_tables(names: list[str], schema: NormalizedSchema) -> tuple[str, ...]:
    """The picked tables that exist for this user (any case). None known: every table."""
    tables = {o.table.lower(): o.table for o in schema.objects if not o.is_hidden}
    return tuple(dict.fromkeys(tables[n.lower()] for n in names if n.lower() in tables))


def _log_decision(model: ModelSummary, choice: ModelRoleChoice, roles: "ModelRoles") -> None:
    """What discovery decided, and what it found (roles only, never field names or data)."""
    logger.info(
        "Sales discovery for %s: tables=%d agent_lenses=%s kept=%s date=%s columns=%s measures=%s",
        model.dataset_id,
        len(roles.tables),
        ",".join(choice.lenses) or "-",
        ",".join(roles.lenses) or "-",
        roles.date_column is not None,
        ",".join(sorted(roles.columns)) or "-",
        ",".join(sorted(roles.measures)) or "-",
    )
    if roles.notes:  # an admin pin that can't be honoured
        logger.warning("Sales discovery for %s: %s", model.dataset_id, roles.notes)


# --- checks -------------------------------------------------------------------------------------


def _by_model(answer: DiscoveryChoice, batch: list[ModelSummary]) -> dict[str, ModelRoleChoice]:
    """The agent's entries for this batch only (an id it wasn't shown is ignored)."""
    wanted = {m.dataset_id for m in batch}
    return {c.dataset_id: c for c in answer.models if c.dataset_id in wanted}


def _problems(choice: ModelRoleChoice, schema: NormalizedSchema) -> list[str]:
    problems: list[str] = []
    if choice.date_column and not is_date_column(choice.date_column, schema):
        problems.append(f"date_column {choice.date_column} is not a listed date column")
    for role, ref in choice.columns.model_dump(exclude_none=True).items():
        if not has_column(ref, schema):
            problems.append(f"{role} column {ref} is not listed")
        elif is_date_column(ref, schema):
            problems.append(f"{role} column {ref} is a date column")
    for role, ref in choice.measures.model_dump(exclude_none=True).items():
        if resolve_measure(ref, schema) is None:
            problems.append(f"{role} measure {ref} is not listed")
    return problems


def _roles(
    model: ModelSummary,
    schema: NormalizedSchema,
    choice: ModelRoleChoice,
    pins: dict[str, bool],
) -> ModelRoles:
    date_column = (
        choice.date_column
        if choice.date_column and is_date_column(choice.date_column, schema)
        else None
    )
    columns = {
        role: ref
        for role, ref in choice.columns.model_dump(exclude_none=True).items()
        if has_column(ref, schema) and not is_date_column(ref, schema)
    }
    measures: dict[MeasureRole, str] = {}
    for role, ref in choice.measures.model_dump(exclude_none=True).items():
        resolved = resolve_measure(ref, schema)
        if resolved is not None:
            measures[role] = resolved
    lower = frozenset(
        resolved
        for ref in choice.lower_is_better
        if (resolved := resolve_measure(ref, schema)) is not None and resolved in measures.values()
    )

    # Pins come from the database as plain strings; unknown names never match a lens.
    wanted: set[str] = set(choice.lenses) if choice.is_sales else set()
    wanted |= {lens for lens, use in pins.items() if use}
    wanted -= {lens for lens, use in pins.items() if not use}
    lenses: list[Lens] = []
    notes: list[str] = []
    for lens in LENSES:
        if lens not in wanted:
            continue
        if fillable(lens, date_column, columns, measures):
            lenses.append(lens)
        elif pins.get(lens):
            notes.append(f"{lens} is pinned but this data source has no fields for it")
    return ModelRoles(
        model=model,
        schema=schema,
        lenses=tuple(lenses),
        date_column=date_column,
        columns=columns,
        measures=measures,
        lower_is_better=lower,
        reason=choice.reason.strip(),
        notes=tuple(notes),
    )


def fillable(
    lens: Lens, date_column: str | None, columns: dict[str, str], measures: dict[str, str]
) -> bool:
    """Whether the server can build this table from these fields (what each needs, design §2)."""
    sales = date_column is not None and any(m in measures for m in SALES_MEASURES)
    if lens == "lob_growth":
        return sales and "lob" in columns
    if lens == "subcategory_loss":
        return sales and "subcategory" in columns
    if lens == "stock_turn":  # its own measure, or sales and stock; over dated records either way
        own = date_column is not None and "stock_turn" in measures
        return own or (sales and any(m in measures for m in STOCK_MEASURES))
    return sales and ("price_band" in columns or "price" in columns)


# --- what the agent sees ------------------------------------------------------------------------


def model_block(model: ModelSummary, schema: NormalizedSchema, tables: Sequence[str] = ()) -> str:
    """Every table of the user's own view (hidden objects left out), compact: its measures and its
    columns by name, dates and numbers marked, descriptions when the model has them.

    Real models have hundreds of columns (one live model: 26 tables, 928 columns), so the listing
    must stay small without leaving any table out. Within MAX_MODEL_CHARS, descriptions go first;
    then each table keeps an equal share of its columns (dates and measures always stay).
    `tables`: only these (the ones picked in step 1); empty means every table."""
    visible = [o for o in schema.objects if not o.is_hidden and o.kind != "table"]
    if tables:
        visible = [o for o in visible if o.table in tables]
    tables = sorted({o.table for o in visible})
    header = f'<model id="{model.dataset_id}" name="{model.name}" domain="{model.domain or ""}">'
    footer = []
    if schema.ai_instructions:
        footer = [
            "AI instructions from the model author:",
            "\n".join(schema.ai_instructions)[:1500],
        ]
    for describe, share in ((True, None), (False, None), (False, _column_share(visible, tables))):
        lines = [header]
        for table in tables:
            lines += _table_lines(table, [o for o in visible if o.table == table], describe, share)
        text = "\n".join([*lines, *footer])
        if len(text) <= MAX_MODEL_CHARS:
            break
    return text[:MAX_MODEL_CHARS] + "\n</model>"


def _table_lines(
    table: str, members: list[SchemaObject], describe: bool, share: int | None
) -> list[str]:
    measures = sorted((o for o in members if o.kind == "measure"), key=lambda o: o.name)
    columns = sorted((o for o in members if o.kind == "column"), key=lambda o: o.name)
    lines = [f"TABLE {table}"]
    if measures:
        lines.append("  measures: " + ", ".join(_entry(o, describe) for o in measures))
    if columns:
        shown = columns
        if share is not None and len(columns) > share:
            dates = [c for c in columns if _kind(c) == "date"]
            shown = dates + [c for c in columns if _kind(c) != "date"][: max(share - len(dates), 0)]
        listed = ", ".join(_entry(o, describe) for o in shown)
        more = f" (and {len(columns) - len(shown)} more)" if len(shown) < len(columns) else ""
        lines.append(f"  columns: {listed}{more}")
    return lines


def _column_share(visible: list[SchemaObject], tables: list[str]) -> int:
    """Columns per table that fit the budget when every table gets the same share (sized from
    the model's own column names)."""
    measures = sum(len(_entry(o, False)) + 2 for o in visible if o.kind == "measure")
    columns = [len(_entry(o, False)) + 2 for o in visible if o.kind == "column"]
    average = sum(columns) / len(columns) if columns else 20
    room = MAX_MODEL_CHARS - measures - 60 * len(tables) - 2000
    return max(int(room / (average * max(len(tables), 1))), 3)


def _entry(obj: SchemaObject, describe: bool) -> str:
    """`Name`, `Name (date)` or `Name (number)`, then the description when asked for."""
    kind = _kind(obj) if obj.kind == "column" else None
    text = f"{obj.name} ({kind})" if kind else obj.name
    if describe and obj.description:
        text += f" [{obj.description[:MAX_DESCRIPTION]}]"
    return text


def _kind(obj: SchemaObject) -> str | None:
    data_type = (obj.data_type or "").lower()
    if data_type in ("date", "datetime"):
        return "date"
    if data_type in ("double", "decimal", "integer", "int64", "currency", "number"):
        return "number"
    return None


def table_catalog(model: ModelSummary, schema: NormalizedSchema) -> str:
    """Step 1: every table of the user's view, with its measures and a sample of its columns
    (dates first). Within MAX_MODEL_CHARS the samples shrink; no table is ever left out."""
    visible = [o for o in schema.objects if not o.is_hidden and o.kind != "table"]
    header = f'<model id="{model.dataset_id}" name="{model.name}" domain="{model.domain or ""}">'
    footer = []
    if schema.ai_instructions:
        footer = ["AI instructions from the model author:", "\n".join(schema.ai_instructions)[:800]]
    tables = sorted({o.table for o in visible})
    for sample in (CATALOG_COLUMNS, 5, 2, 0):
        lines = [header]
        for table in tables:
            lines.append(_catalog_line(table, [o for o in visible if o.table == table], sample))
        text = "\n".join([*lines, *footer])
        if len(text) <= MAX_MODEL_CHARS:
            break
    return text[:MAX_MODEL_CHARS] + "\n</model>"


def _catalog_line(table: str, members: list[SchemaObject], sample: int) -> str:
    measures = sorted(o.name for o in members if o.kind == "measure")
    columns = sorted(
        (o for o in members if o.kind == "column"), key=lambda o: (_kind(o) != "date", o.name)
    )
    line = f"TABLE {table} ({len(columns)} columns)"
    if measures:
        line += " | measures: " + ", ".join(measures)
    if columns and sample:
        listed = ", ".join(_entry(o, False) for o in columns[:sample])
        more = f" ... ({len(columns) - sample} more)" if len(columns) > sample else ""
        line += f" | columns: {listed}{more}"
    return line
