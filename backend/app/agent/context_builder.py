"""Stage 2-3: choose candidate models and build the planner's context.

Only allowed models are ever considered, and only objects from the user's own visible schema are
listed, so the LLM never learns about models or objects the user can't access.
"""

import logging
from dataclasses import dataclass

from app.agent.table_selector import Selection
from app.authz.models import AuthorizedContext
from app.core.config import Settings
from app.semantic.normalizer import NormalizedSchema, SchemaObject
from app.semantic.retriever import SemanticRetriever
from app.semantic.user_schema import UserSchemaService

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 3
MAX_MEASURES = 60
MAX_COLUMNS = 60
SMALL_MODEL_COLUMNS = 80
MAX_INSTRUCTION_CHARS = 2000
_DATE_TYPES = ("date", "datetime")
MAX_FOCUSED_OBJECTS = 400  # per model, after table selection


@dataclass(frozen=True)
class PlannerContext:
    text: str
    schemas: dict[str, NormalizedSchema]


class ContextBuilder:
    def __init__(
        self, retriever: SemanticRetriever, user_schemas: UserSchemaService, settings: Settings
    ) -> None:
        self._retriever = retriever
        self._user_schemas = user_schemas
        self._docs = settings.agent_context_docs

    async def candidates(
        self, authz: AuthorizedContext, question: str, primary_model_id: str | None
    ) -> list[str]:
        chosen: list[str] = []
        if primary_model_id and authz.is_allowed(primary_model_id):
            chosen.append(primary_model_id)
        for routed in await self._retriever.route_models(authz, question, k=MAX_CANDIDATES):
            if routed.model.dataset_id not in chosen:
                chosen.append(routed.model.dataset_id)
        if not chosen:  # index not built yet: fall back to the user's models
            chosen = [m.dataset_id for m in authz.allowed_models]
        return chosen[:MAX_CANDIDATES]

    async def build(
        self, authz: AuthorizedContext, question: str, candidates: list[str]
    ) -> PlannerContext:
        schemas: dict[str, NormalizedSchema] = {}
        for dataset_id in candidates:
            schema = await self._user_schemas.visible(authz, dataset_id)
            if schema is not None and schema.objects:
                schemas[dataset_id] = schema
            else:
                logger.warning("No visible schema for %s; excluded from context", dataset_id)
        if not schemas:
            return PlannerContext("", {})

        docs = await self._retriever.search(
            authz, question, model_ids=list(schemas), k=self._docs * len(schemas)
        )
        relevant = {ref for d in docs for ref in d.referenced_objects}
        blocks = [
            _model_block(authz.allowed[d].name, authz.allowed[d].domain, d, s, relevant)
            for d, s in schemas.items()
        ]
        return PlannerContext("\n\n".join(blocks), schemas)


def _model_block(
    name: str, domain: str | None, dataset_id: str, schema: NormalizedSchema, relevant: set[str]
) -> str:
    visible = [o for o in schema.objects if not o.is_hidden]
    measures = [o for o in visible if o.kind == "measure"]
    columns = [o for o in visible if o.kind == "column"]
    if len(columns) > SMALL_MODEL_COLUMNS:
        columns = [
            c for c in columns if c.key in relevant or (c.data_type or "").lower() in _DATE_TYPES
        ]
    measures.sort(key=lambda m: (m.key not in relevant, m.name))
    columns.sort(key=lambda c: (c.key not in relevant, c.table, c.name))

    lines = [f'<model id="{dataset_id}" name="{name}" domain="{domain or ""}">', "Measures:"]
    lines += [_line(m) for m in measures[:MAX_MEASURES]] or ["  (none)"]
    lines.append("Columns:")
    lines += [_line(c) for c in columns[:MAX_COLUMNS]] or ["  (none)"]
    if schema.ai_instructions:
        text = "\n".join(schema.ai_instructions)[:MAX_INSTRUCTION_CHARS]
        lines += ["AI instructions from the model author:", text]
    if schema.verified_answers:
        lines.append("Verified answers (preferred when they match):")
        for answer in schema.verified_answers:
            fields = ", ".join(answer.fields) or "-"
            lines.append(f"  - {answer.title}: {'; '.join(answer.phrases)} | fields: {fields}")
    lines.append("</model>")
    return "\n".join(lines)


def focused(
    authz: AuthorizedContext, schemas: dict[str, NormalizedSchema], selection: Selection
) -> PlannerContext:
    """Round trip 2 context: the selected tables in full (every measure and column), the
    relationships between them, and what the user's words mean (from round trip 1)."""
    blocks: list[str] = []
    kept: dict[str, NormalizedSchema] = {}
    for dataset_id, tables in selection.tables.items():
        schema = schemas.get(dataset_id)
        if schema is None:
            continue
        kept[dataset_id] = schema
        model = authz.allowed[dataset_id]
        visible = [
            o for o in schema.objects if not o.is_hidden and o.kind != "table" and o.table in tables
        ]
        measures = sorted((o for o in visible if o.kind == "measure"), key=lambda o: o.key)
        columns = sorted((o for o in visible if o.kind == "column"), key=lambda o: o.key)
        lines = [
            f'<model id="{dataset_id}" name="{model.name}" domain="{model.domain or ""}">',
            "Tables: " + ", ".join(sorted(tables)),
            "Measures:",
        ]
        lines += [_line(m) for m in measures[:MAX_FOCUSED_OBJECTS]] or ["  (none)"]
        lines.append("Columns:")
        lines += [_line(c) for c in columns[: MAX_FOCUSED_OBJECTS - len(measures)]] or ["  (none)"]
        relationships = [
            r for r in schema.relationships if r.from_table in tables and r.to_table in tables
        ]
        if relationships:
            lines.append("Relationships:")
            lines += [f"  {r.describe()}" for r in relationships]
        meanings = [(w, f) for w, f in selection.mappings if f.split("[", 1)[0] in tables]
        if meanings:
            lines.append("What the user's words refer to:")
            lines += [f'  "{word}" -> {field_ref}' for word, field_ref in meanings]
        if schema.ai_instructions:
            text = "\n".join(schema.ai_instructions)[:MAX_INSTRUCTION_CHARS]
            lines += ["AI instructions from the model author:", text]
        if schema.verified_answers:
            lines.append("Verified answers (preferred when they match):")
            for answer in schema.verified_answers:
                fields = ", ".join(answer.fields) or "-"
                lines.append(f"  - {answer.title}: {'; '.join(answer.phrases)} | fields: {fields}")
        lines.append("</model>")
        blocks.append("\n".join(lines))
    return PlannerContext("\n\n".join(blocks), kept)


def _line(obj: SchemaObject) -> str:
    kind = f" ({obj.data_type})" if obj.data_type else ""
    description = f" - {obj.description}" if obj.description else ""
    return f"  {obj.table}[{obj.name}]{kind}{description}"
