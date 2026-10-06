"""Turns a GetSemanticModelSchema payload into model objects + "Prep data for AI" content.

Microsoft documents the content (tables, columns, measures, relationships, CustomInstructions,
VerifiedAnswers) but not the exact field names, so lookups are case-insensitive and accept the
plausible variants. Fixtures under tests/fixtures/ must be replaced by real captured payloads in
spike S3; unknown shapes yield an empty schema rather than guesses.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class SchemaObject:
    kind: str  # table | column | measure
    table: str
    name: str
    description: str | None = None
    data_type: str | None = None
    expression: str | None = None
    is_hidden: bool = False

    @property
    def key(self) -> str:
        return object_key(self.kind, self.table, self.name)


@dataclass(frozen=True)
class VerifiedAnswer:
    title: str
    phrases: tuple[str, ...]
    fields: tuple[str, ...]  # object keys referenced, when the payload names them


@dataclass(frozen=True)
class NormalizedSchema:
    objects: tuple[SchemaObject, ...]
    ai_instructions: tuple[str, ...] = ()
    verified_answers: tuple[VerifiedAnswer, ...] = ()
    schema_hash: str = ""
    visible_keys: frozenset[str] = field(default_factory=frozenset)


def object_key(kind: str, table: str, name: str) -> str:
    return f"table:{table}" if kind == "table" else f"{kind}:{table}[{name}]"


def normalize(payload: Any) -> NormalizedSchema:
    root = _unwrap(payload)
    objects: list[SchemaObject] = []
    for table in _list(root, "tables"):
        if not isinstance(table, dict):
            continue
        table_name = _str(table, "name")
        if not table_name:
            continue
        objects.append(
            SchemaObject(
                "table",
                table_name,
                table_name,
                _str(table, "description"),
                is_hidden=_bool(table, "isHidden"),
            )
        )
        for column in _list(table, "columns"):
            obj = _member("column", table_name, column)
            if obj:
                objects.append(obj)
        for measure in _list(table, "measures"):
            obj = _member("measure", table_name, measure)
            if obj:
                objects.append(obj)

    instructions = tuple(_instructions(root))
    answers = tuple(_verified_answers(root))
    canonical = json.dumps(
        {
            "objects": sorted((asdict(o) for o in objects), key=lambda o: json.dumps(o)),
            "instructions": instructions,
            "answers": [asdict(a) for a in answers],
        },
        sort_keys=True,
    )
    return NormalizedSchema(
        objects=tuple(objects),
        ai_instructions=instructions,
        verified_answers=answers,
        schema_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        visible_keys=frozenset(o.key for o in objects),
    )


# --- helpers ------------------------------------------------------------------------------------


def _unwrap(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, dict):
        return {}
    for wrapper in ("schema", "model", "semanticModel", "result"):
        inner = _get(payload, wrapper)
        if isinstance(inner, str):
            return _unwrap(inner)
        if isinstance(inner, dict) and _get(inner, "tables") is not None:
            return inner
    return payload


def _get(obj: dict[str, Any], name: str) -> Any:
    wanted = name.lower()
    for key, value in obj.items():
        if key.lower() == wanted:
            return value
    return None


def _list(obj: dict[str, Any], name: str) -> list[Any]:
    value = _get(obj, name)
    return value if isinstance(value, list) else []


def _str(obj: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = _get(obj, name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _bool(obj: dict[str, Any], name: str) -> bool:
    return _get(obj, name) is True


def _member(kind: str, table: str, raw: Any) -> SchemaObject | None:
    if isinstance(raw, str):
        return SchemaObject(kind, table, raw)
    if not isinstance(raw, dict):
        return None
    name = _str(raw, "name")
    if not name:
        return None
    return SchemaObject(
        kind,
        table,
        name,
        description=_str(raw, "description"),
        data_type=_str(raw, "dataType", "type"),
        expression=_str(raw, "expression") if kind == "measure" else None,
        is_hidden=_bool(raw, "isHidden"),
    )


def _instructions(root: dict[str, Any]) -> list[str]:
    value = _get(root, "CustomInstructions")
    if value is None:
        value = _get(root, "aiInstructions")
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []


def _verified_answers(root: dict[str, Any]) -> list[VerifiedAnswer]:
    answers = []
    for raw in _list(root, "VerifiedAnswers"):
        if not isinstance(raw, dict):
            continue
        title = _str(raw, "title", "name", "question") or ""
        phrases = _get(raw, "triggerPhrases") or _get(raw, "phrases") or []
        fields = []
        for f in _get(raw, "fields") or []:
            if isinstance(f, dict) and _str(f, "table") and _str(f, "name"):
                kind = (_str(f, "kind", "type") or "column").lower()
                kind = kind if kind in {"column", "measure"} else "column"
                fields.append(object_key(kind, _str(f, "table") or "", _str(f, "name") or ""))
        if title or phrases:
            answers.append(
                VerifiedAnswer(
                    title=title,
                    phrases=tuple(p for p in phrases if isinstance(p, str)),
                    fields=tuple(fields),
                )
            )
    return answers
