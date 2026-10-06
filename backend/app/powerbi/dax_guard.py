"""Minimal read-only check applied to every query before it leaves the backend.

The full validator (object names against the user's schema, size limits, repair loop) is Phase 8.
DAX queries can only read data, but this keeps anything that isn't a query (DMV/INFO probing,
empty or oversized text) from reaching Power BI.
"""

import re

from app.core.errors import AppError, ErrorCode

MAX_DAX_LENGTH = 20_000
_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*|--[^\n]*", re.DOTALL)
_STARTS_LIKE_QUERY = re.compile(r"^\s*(DEFINE|EVALUATE)\b", re.IGNORECASE)
# DMVs and INFO functions expose model internals and are unsupported by the REST API anyway.
_FORBIDDEN = re.compile(r"\$SYSTEM\.|\bINFO\.[A-Z]+\s*\(", re.IGNORECASE)


def ensure_read_only_query(dax: str) -> str:
    stripped = _COMMENTS.sub(" ", dax).strip()
    if not stripped or len(dax) > MAX_DAX_LENGTH:
        raise _rejected("empty or oversized query")
    if not _STARTS_LIKE_QUERY.match(stripped):
        raise _rejected("query must start with DEFINE or EVALUATE")
    if _FORBIDDEN.search(stripped):
        raise _rejected("DMV / INFO functions are not allowed")
    return dax


def _rejected(reason: str) -> AppError:
    return AppError(
        ErrorCode.QUERY_REJECTED,
        "I couldn't run the query for that question. Try rephrasing it.",
        422,
        log_detail=f"DAX rejected: {reason}",
    )
