"""Round trip 1 for large models: pick the tables and map the user's words to field names.

Large models (hundreds of columns, several look-alike tables) can't be shown to the planner in full,
and a text search alone misses business words ("store" -> Location Name). So the LLM first sees a
compact catalog of EVERY table (measure and column names only) and returns the tables that answer
the question plus "word -> field" mappings. The server keeps only names that exist in the user's
own visible schema; the planner (round trip 2) then gets those tables in full.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import parse_ref
from app.agent.planner import load_prompt, untrusted
from app.authz.models import AuthorizedContext
from app.core.errors import AppError
from app.llm.base import ChatMessage, LLMProvider
from app.semantic.normalizer import NormalizedSchema, object_key

logger = logging.getLogger(__name__)

MAX_TABLES = 6
CATALOG_CHARS = 60_000


class TableRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    table: str


class TermMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str = Field(max_length=60, description="A word or phrase from the question")
    field: str = Field(description="The exact Table[Name] it refers to")


class TableSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[TableRef] = Field(default_factory=list, max_length=MAX_TABLES)
    mappings: list[TermMapping] = Field(default_factory=list, max_length=20)


@dataclass(frozen=True)
class Selection:
    """Validated: only tables and fields the user can see."""

    tables: dict[str, set[str]]  # model id -> table names
    mappings: list[tuple[str, str]]  # (word, Table[Name])


def needs_selection(schemas: dict[str, NormalizedSchema], min_columns: int) -> bool:
    columns = sum(
        1 for s in schemas.values() for o in s.objects if o.kind == "column" and not o.is_hidden
    )
    return columns > min_columns


def catalog(authz: AuthorizedContext, schemas: dict[str, NormalizedSchema]) -> str:
    """Every visible table with its measure and column names (names only, compact)."""
    blocks: list[str] = []
    for dataset_id, schema in schemas.items():
        model = authz.allowed[dataset_id]
        lines = [f'<model id="{dataset_id}" name="{model.name}" domain="{model.domain or ""}">']
        visible = [o for o in schema.objects if not o.is_hidden]
        for table in sorted({o.table for o in visible if o.kind == "table"}):
            measures = [o.name for o in visible if o.kind == "measure" and o.table == table]
            columns = [o.name for o in visible if o.kind == "column" and o.table == table]
            line = f"TABLE {table}"
            if measures:
                line += " | measures: " + ", ".join(measures)
            if columns:
                line += " | columns: " + ", ".join(columns)
            lines.append(line)
        if schema.relationships:
            lines.append("RELATIONSHIPS:")
            lines += [f"  {r.describe()}" for r in schema.relationships if r.active]
        if schema.ai_instructions:
            lines.append("AI instructions from the model author:")
            lines.append("\n".join(schema.ai_instructions)[:2000])
        lines.append("</model>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)[:CATALOG_CHARS]


class TableSelector:
    def __init__(self, llm: LLMProvider, *, fiscal_start_month: int) -> None:
        self._llm = llm
        self._fiscal_start_month = fiscal_start_month
        self._schema = json.dumps(TableSelection.model_json_schema(), separators=(",", ":"))

    async def select(
        self,
        question: str,
        authz: AuthorizedContext,
        schemas: dict[str, NormalizedSchema],
        *,
        today: date,
        previous: dict[str, Any] | None = None,
    ) -> Selection | None:
        system = (
            load_prompt("table_selector")
            .replace("{fiscal_start_month}", str(self._fiscal_start_month))
            .replace("{today}", today.isoformat())
            .replace("{schema}", self._schema)
        )
        parts = ["CATALOG", untrusted(catalog(authz, schemas))]
        if previous:
            parts += [
                "PREVIOUS TURN (for follow-up questions)",
                untrusted(json.dumps(previous, default=str)[:3000]),
            ]
        parts += ["QUESTION", untrusted(question)]
        try:
            raw = await self._llm.complete_json(
                [ChatMessage("system", system), ChatMessage("user", "\n".join(parts))],
                TableSelection,
            )
        except AppError as exc:
            logger.warning("Table selection failed (%s); planning without it", exc.code)
            return None
        return validate_selection(raw, schemas)


def validate_selection(
    raw: TableSelection, schemas: dict[str, NormalizedSchema]
) -> Selection | None:
    """Keeps only tables/fields that exist in the user's visible schema; None if nothing is left."""
    tables: dict[str, set[str]] = {}
    for ref in raw.tables:
        schema = schemas.get(ref.model_id)
        if schema is not None and object_key("table", ref.table, ref.table) in schema.visible_keys:
            tables.setdefault(ref.model_id, set()).add(ref.table)
    mappings: list[tuple[str, str]] = []
    for mapping in raw.mappings:
        parsed = parse_ref(mapping.field)
        if parsed is None:
            continue
        for model_id, schema in schemas.items():
            keys = {object_key(k, *parsed) for k in ("column", "measure")}
            if keys & schema.visible_keys:
                mappings.append((mapping.term.strip()[:60], f"{parsed[0]}[{parsed[1]}]"))
                tables.setdefault(model_id, set()).add(parsed[0])  # a mapped field's table counts
                break
    if not tables:
        return None
    logger.info(
        "Table selection: %d tables in %d models, %d word mappings",
        sum(map(len, tables.values())),
        len(tables),
        len(mappings),
    )
    return Selection(tables, mappings)
