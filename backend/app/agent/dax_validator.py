"""Validates DAX against the user's own visible schema before it reaches Power BI (L5).

Applied to every query, including template-built ones (defence in depth) and especially to
LLM-written or LLM-repaired DAX:
- read-only query shape (existing ensure_read_only_query: DEFINE/EVALUATE, no DMV/INFO.*)
- exactly one EVALUATE
- every Table[Column] / Table[Measure] reference is in the user's visible schema (OLS)
- every bare [Name] is a visible measure or a name defined inside the query
"""

import re

from app.core.errors import AppError, ErrorCode
from app.powerbi.dax_guard import ensure_read_only_query
from app.semantic.normalizer import NormalizedSchema, object_key

_STRING = re.compile(r'"(?:[^"]|"")*"')
# Unquoted table names touch the bracket (Sales[Amount]); "ORDER BY [x]" must not read as table BY.
_QUALIFIED = re.compile(r"(?:'((?:[^']|'')+)'\s*|\b([A-Za-z_][A-Za-z0-9_]*))\[((?:[^\]]|\]\])+)\]")
_KEYWORDS = {"BY", "IN", "ORDER", "RETURN", "VAR", "DEFINE", "EVALUATE", "ASC", "DESC", "NOT"}
_BARE = re.compile(r"(?<![\w'\]])\[((?:[^\]]|\]\])+)\]")
_EVALUATE = re.compile(r"\bEVALUATE\b", re.IGNORECASE)
_DEFINED = re.compile(r"\b(?:MEASURE|COLUMN)\s+(?:'[^']+'|\w+)\s*\[([^\]]+)\]", re.IGNORECASE)
_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*|--[^\n]*", re.DOTALL)


class DaxValidationError(AppError):
    """`reason` (may name objects from LLM-written DAX) goes to the repair loop; logs get `kind`."""

    def __init__(self, reason: str, kind: str) -> None:
        super().__init__(
            ErrorCode.QUERY_REJECTED,
            "I couldn't run the query for that question. Try rephrasing it.",
            422,
            log_detail=f"DAX rejected: {kind}",
        )
        self.reason = reason
        self.kind = kind


def validate_dax(dax: str, schema: NormalizedSchema) -> None:
    try:
        ensure_read_only_query(dax)
    except AppError as exc:
        raise DaxValidationError(
            exc.log_detail or "not a read-only query", "not_read_only"
        ) from exc

    code = _COMMENTS.sub(" ", dax)
    aliases = {m.group(0)[1:-1].replace('""', '"') for m in _STRING.finditer(code)}
    code = _STRING.sub('""', code)  # string contents can't smuggle references
    if len(_EVALUATE.findall(code)) != 1:
        raise DaxValidationError("exactly one EVALUATE is required", "evaluate_count")

    defined = {m.group(1) for m in _DEFINED.finditer(code)}
    visible = schema.visible_keys
    measure_names = {o.name for o in schema.objects if o.kind == "measure"}

    unknown: list[str] = []
    for match in _QUALIFIED.finditer(code):
        table = (match.group(1) or match.group(2) or "").replace("''", "'")
        name = match.group(3).replace("]]", "]")
        if match.group(2) and match.group(2).upper() in _KEYWORDS:
            continue
        keys = {object_key("column", table, name), object_key("measure", table, name)}
        if not keys & visible and name not in defined:
            unknown.append(f"{table}[{name}]")
    stripped = _QUALIFIED.sub(" ", code)
    for match in _BARE.finditer(stripped):
        name = match.group(1).replace("]]", "]")
        if name not in measure_names and name not in aliases and name not in defined:
            unknown.append(f"[{name}]")
    if unknown:
        raise DaxValidationError(
            f"objects not in the user's schema: {sorted(set(unknown))}",
            f"unknown_objects:{len(set(unknown))}",
        )
