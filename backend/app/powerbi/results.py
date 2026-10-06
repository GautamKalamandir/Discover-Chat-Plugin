"""Turns Power BI query responses into rows.

The REST shape is documented (`results[0].tables[0].rows`). Fabric IQ's ExecuteQuery output is
not documented beyond "tabular results" plus an embedded CSV resource for large results, so the
parser accepts the plausible shapes and fails loudly otherwise. Confirm with spike S3.
"""

import csv
import io
import json
from typing import Any

from app.powerbi.errors import GatewayUnavailableError

Rows = list[dict[str, Any]]


def rows_from_json(payload: Any) -> Rows | None:
    """Accepts REST-style `{"results":[{"tables":[{"rows":[...]}]}]}`, `{"tables":[...]}`,
    `{"rows":[...]}` or a bare list of row objects. Returns None when nothing matches."""
    if isinstance(payload, list) and all(isinstance(r, dict) for r in payload):
        return list(payload)
    if not isinstance(payload, dict):
        return None
    # MCP servers wrap plain-string tool results as {"result": "<json text>"}.
    if set(payload) == {"result"} and isinstance(payload["result"], str):
        return rows_from_json(parse_json_text(payload["result"]))
    if isinstance(payload.get("rows"), list):
        return rows_from_json(payload["rows"])
    for key in ("results", "tables"):
        items = payload.get(key)
        if isinstance(items, list) and items:
            return rows_from_json(items[0])
    return None


def rows_from_csv(text: str) -> Rows:
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


def columns_of(rows: Rows) -> list[str]:
    columns: dict[str, None] = {}
    for row in rows:
        columns.update(dict.fromkeys(row))
    return list(columns)


def parse_json_text(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def require_rows(rows: Rows | None, source: str) -> Rows:
    if rows is None:
        raise GatewayUnavailableError(f"Unrecognised result format from {source}")
    return rows
