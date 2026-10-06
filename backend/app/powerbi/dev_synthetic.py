"""POWERBI_GATEWAY=dev_synthetic: fake, deterministic rows for local development (ADR 0008).

Refused unless ENVIRONMENT=local|test. Lets the whole agent pipeline run before Power BI access
exists. Answers built on it carry a "Development data" note. It reads the group-by columns and
"Alias" values from a SUMMARIZECOLUMNS query and makes up numbers for them.
"""

import hashlib
import re
from typing import Any

from app.core.config import Environment, Settings
from app.core.errors import ConfigurationError
from app.powerbi.base import GatewayCapability, GatewayPayload, PowerBIGateway, QueryResult

_STRING = re.compile(r'"((?:[^"]|"")*)"')
_COLUMN = re.compile(r"'((?:[^']|'')+)'\[((?:[^\]]|\]\])+)\]")
_NESTED = ("TREATAS", "FILTER", "CALCULATETABLE", "KEEPFILTERS", "ALL")
GROUPS = ("DEV-A", "DEV-B", "DEV-C")


class DevSyntheticGateway(PowerBIGateway):
    name = "dev_synthetic"
    capabilities = frozenset({GatewayCapability.EXECUTE, GatewayCapability.VALUE_SEARCH})
    requires_user_token = False

    def __init__(self, settings: Settings) -> None:
        if settings.environment not in (Environment.LOCAL, Environment.TEST):
            env = settings.environment
            raise ConfigurationError(f"POWERBI_GATEWAY=dev_synthetic is not allowed in {env=}")

    async def execute_dax(
        self, user_token: str, dataset_id: str, dax: str, *, max_rows: int, user_key: str
    ) -> QueryResult:
        groups = _group_columns(dax)
        filters = _filter_values(dax)
        aliases = [m.group(1).replace('""', '"') for m in _STRING.finditer(_strip_nested(dax))]
        aliases = [a for a in aliases if a] or ["Value"]
        keys = [[f"{t}[{c}]", *filters.get(f"{t}[{c}]", GROUPS)] for t, c in groups]

        rows: list[dict[str, Any]] = []
        combos: list[list[str]] = [[]]
        for key in keys:
            combos = [[*combo, value] for combo in combos for value in key[1:]]
        for combo in combos:
            row: dict[str, Any] = {key[0]: value for key, value in zip(keys, combo, strict=True)}
            for alias in aliases:
                row[f"[{alias}]"] = _number(dataset_id, alias, combo)
            rows.append(row)
        columns = [k[0] for k in keys] + [f"[{a}]" for a in aliases]
        return QueryResult(
            columns=columns, rows=rows[:max_rows], truncated=len(rows) > max_rows, gateway=self.name
        )

    async def search_values(
        self, user_token: str, dataset_id: str, terms: list[str]
    ) -> GatewayPayload:
        return GatewayPayload(data=[], gateway=self.name)


def _number(dataset_id: str, alias: str, combo: list[str]) -> float:
    digest = hashlib.sha256("|".join([dataset_id, alias, *combo]).encode()).hexdigest()
    return round(1000 + int(digest[:8], 16) % 99000 + int(digest[8:10], 16) / 100, 2)


def _strip_nested(dax: str) -> str:
    """Removes TREATAS(...)/FILTER(...) spans so only group-by columns and aliases remain."""
    out, i, upper = [], 0, dax.upper()
    while i < len(dax):
        hit = next((f for f in _NESTED if upper.startswith(f + "(", i)), None)
        if hit is None:
            out.append(dax[i])
            i += 1
            continue
        depth, j = 0, i + len(hit)
        while j < len(dax):
            depth += dax[j] == "("
            depth -= dax[j] == ")"
            j += 1
            if depth == 0:
                break
        i = j
    return "".join(out)


def _group_columns(dax: str) -> list[tuple[str, str]]:
    body = _strip_nested(dax).split("ORDER BY")[0]
    seen: list[tuple[str, str]] = []
    for match in _COLUMN.finditer(body):
        pair = (match.group(1).replace("''", "'"), match.group(2).replace("]]", "]"))
        if pair not in seen:
            seen.append(pair)
    return seen


def _filter_values(dax: str) -> dict[str, list[str]]:
    """TREATAS({"GOLD"}, 'Product'[LOB]) -> {"Product[LOB]": ["GOLD"]}."""
    found: dict[str, list[str]] = {}
    for match in re.finditer(r"TREATAS\(\{([^}]*)\},\s*" + _COLUMN.pattern + r"\)", dax):
        values = [v.replace('""', '"') for v in _STRING.findall(match.group(1))]
        if values:
            found[f"{match.group(2)}[{match.group(3)}]"] = values
    return found
