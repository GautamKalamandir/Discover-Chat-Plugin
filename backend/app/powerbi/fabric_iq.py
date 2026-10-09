"""Primary path: Microsoft's Fabric IQ MCP server, called as the signed-in user.

- Endpoint: FABRIC_IQ_MCP_URL (Streamable HTTP). Delegated tokens only; RLS/OLS enforced.
- Tool contract pinned with the `X-Variants` header; tools checked via `tools/list` once.
- Tools used: ExecuteQuery(artifactId, daxQueries[1..4], maxRows<=1000),
  GetSemanticModelSchema(artifactId), ValueSearch(artifactId, searchTerms[]).
- Token scope: `https://api.fabric.microsoft.com/.default`, as advertised by the endpoint's
  OAuth protected-resource metadata. This is not the Power BI REST scope; the broker issues both
  through On-Behalf-Of.

Endpoint-level failures (HTTP errors, tenant setting off, unsupported region, contract mismatch)
raise GatewayUnavailableError so the service can fall back to REST. Only a tool reporting that
the *user* is unauthorized raises PowerBIAccessDeniedError.
"""

import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, EmbeddedResource, TextContent, TextResourceContents

from app.core.config import Settings
from app.powerbi.base import GatewayCapability, GatewayPayload, PowerBIGateway, QueryResult
from app.powerbi.errors import (
    DaxQueryError,
    GatewayUnavailableError,
    PowerBIAccessDeniedError,
    PowerBIError,
    PowerBIThrottledError,
    PowerBITimeoutError,
)
from app.powerbi.results import (
    columns_of,
    parse_json_text,
    require_rows,
    rows_from_csv,
    rows_from_json,
)

logger = logging.getLogger(__name__)

EXECUTE_QUERY = "ExecuteQuery"
GET_SCHEMA = "GetSemanticModelSchema"
VALUE_SEARCH = "ValueSearch"
REQUIRED_TOOLS = frozenset({EXECUTE_QUERY, GET_SCHEMA, VALUE_SEARCH})

# Tool error text -> classification. Fabric IQ documents the categories (invalid DAX,
# unauthorized, timeout, throttled) but not exact wording; verify in spike S3.
# Spike S3 (2026-10-08): a failing DAX query is NOT a tool error. It comes back as a normal
# text result: "DAX query syntax error: DAX query execution failed: Query (1, 18) ...".
_DAX_ERROR_PREFIXES = ("dax query syntax error", "dax query execution failed")
_ACCESS_WORDS = ("unauthorized", "forbidden", "permission", "access denied", "not authorized")
_THROTTLE_WORDS = ("throttl", "too many requests", "rate limit", "429")
_TIMEOUT_WORDS = ("timeout", "timed out")

HttpClientFactory = Callable[[dict[str, str], float], httpx2.AsyncClient]


def _default_http_client(headers: dict[str, str], timeout_seconds: float) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(timeout_seconds))


class FabricIqMcpGateway(PowerBIGateway):
    name = "fabric_iq_mcp"
    capabilities = frozenset(
        {GatewayCapability.EXECUTE, GatewayCapability.SCHEMA, GatewayCapability.VALUE_SEARCH}
    )

    def __init__(
        self, settings: Settings, http_client_factory: HttpClientFactory | None = None
    ) -> None:
        self._url = settings.fabric_iq_mcp_url
        self._variant = settings.fabric_iq_tool_variant
        self._timeout = float(settings.powerbi_query_timeout_seconds)
        self._http_client_factory = http_client_factory or _default_http_client
        self.token_scope = settings.fabric_iq_token_scope
        self._contract_checked = False

    # --- capabilities ---------------------------------------------------------------------------

    async def execute_dax(
        self, user_token: str, dataset_id: str, dax: str, *, max_rows: int, user_key: str
    ) -> QueryResult:
        started = time.perf_counter()
        result = await self._call(
            user_token,
            EXECUTE_QUERY,
            {"artifactId": dataset_id, "daxQueries": [dax], "maxRows": max_rows},
        )
        if (dax_error := _dax_error_text(result)) is not None:
            raise DaxQueryError(dax_error)
        rows = _canonical_columns(
            require_rows(_rows_from_result(result), "Fabric IQ ExecuteQuery"), dax
        )
        return QueryResult(
            columns=columns_of(rows),
            rows=rows[:max_rows],
            truncated=len(rows) >= max_rows,
            gateway=self.name,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    async def get_schema(self, user_token: str, dataset_id: str) -> GatewayPayload:
        result = await self._call(user_token, GET_SCHEMA, {"artifactId": dataset_id})
        return GatewayPayload(data=_payload_from_result(result), gateway=self.name)

    async def search_values(
        self, user_token: str, dataset_id: str, terms: list[str]
    ) -> GatewayPayload:
        result = await self._call(
            user_token, VALUE_SEARCH, {"artifactId": dataset_id, "searchTerms": terms}
        )
        return GatewayPayload(data=_payload_from_result(result), gateway=self.name)

    # --- diagnostics (Phase 2 spikes) -----------------------------------------------------------

    async def list_tools(self, user_token: str) -> list[dict[str, Any]]:
        """The live tool contract: names, descriptions and input schemas."""
        try:
            async with self._session(user_token) as session:
                tools = (await session.list_tools()).tools
        except Exception as exc:
            raise _classify_transport_error(exc) from exc
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]

    async def call_raw(
        self, user_token: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """One tool call, described as returned (content types, structured content, error flag)
        plus how our classifier would read an error. Used to capture the real formats."""
        try:
            async with self._session(user_token) as session:
                result = await session.call_tool(
                    tool, arguments, read_timeout_seconds=self._timeout
                )
        except Exception as exc:
            raise _classify_transport_error(exc) from exc
        if not isinstance(result, CallToolResult):
            return {"unexpected_result_type": type(result).__name__}
        content = []
        for item in result.content:
            entry: dict[str, Any] = {"type": type(item).__name__}
            if isinstance(item, TextContent):
                entry["text"] = item.text
            elif isinstance(item, EmbeddedResource):
                resource = item.resource
                entry["mime_type"] = getattr(resource, "mime_type", None)
                entry["uri"] = str(getattr(resource, "uri", ""))
                entry["text"] = getattr(resource, "text", None)
            content.append(entry)
        described: dict[str, Any] = {
            "is_error": bool(result.is_error),
            "content": content,
            "structured_content": result.structured_content,
        }
        if result.is_error:
            described["classified_as"] = type(_classify_tool_error(_text_of(result))).__name__
        elif _dax_error_text(result) is not None:
            described["classified_as"] = DaxQueryError.__name__
        return described

    # --- MCP plumbing ---------------------------------------------------------------------------

    def _session(self, user_token: str) -> AbstractAsyncContextManager[ClientSession]:
        headers = {"Authorization": f"Bearer {user_token}", "X-Variants": self._variant}

        @asynccontextmanager
        async def open_session() -> AsyncIterator[ClientSession]:
            async with (
                self._http_client_factory(headers, self._timeout) as http,
                streamable_http_client(self._url, http_client=http) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                yield session

        return open_session()

    async def _call(self, user_token: str, tool: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            async with self._session(user_token) as session:
                if not self._contract_checked:
                    await self._check_contract(session)
                result = await session.call_tool(
                    tool, arguments, read_timeout_seconds=self._timeout
                )
        except Exception as exc:
            # The transport runs in a task group, so errors (including our own) arrive wrapped
            # in an ExceptionGroup.
            leaf = _first_leaf(exc)
            if isinstance(leaf, PowerBIError | DaxQueryError):
                raise leaf from exc
            raise _classify_transport_error(exc) from exc

        if not isinstance(result, CallToolResult):
            raise GatewayUnavailableError(f"Unexpected MCP result type {type(result).__name__}")
        if result.is_error:
            raise _classify_tool_error(_text_of(result))
        return result

    async def _check_contract(self, session: ClientSession) -> None:
        tools = {tool.name for tool in (await session.list_tools()).tools}
        missing = REQUIRED_TOOLS - tools
        if missing:
            logger.error("Fabric IQ tool contract mismatch; missing %s", sorted(missing))
            raise GatewayUnavailableError(f"Fabric IQ tools missing: {sorted(missing)}")
        self._contract_checked = True


# --- result handling ------------------------------------------------------------------------


def _text_of(result: CallToolResult) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


def _csv_resource(result: CallToolResult) -> str | None:
    for content in result.content:
        if isinstance(content, EmbeddedResource) and isinstance(
            content.resource, TextResourceContents
        ):
            if "csv" in (content.resource.mime_type or "").lower():
                return content.resource.text
    return None


_QUALIFIED = re.compile(r"^'?(?P<table>[^'\[\]]+?)'?\[(?P<name>[^\]]+)\]$")
_DOTTED = re.compile(r"^(?P<table>[^.\[\]]+)\.(?P<name>[^\[\]]+)$")
_DAX_ALIAS = re.compile(r'"((?:[^"]|"")+)"\s*,')


def _canonical_columns(rows: list[dict[str, Any]], dax: str) -> list[dict[str, Any]]:
    """Column names as the REST API spells them, which the rest of the agent relies on:
    group-by columns `Table[Column]`, calculated values `[Alias]`. Fabric IQ's own spelling is
    mapped onto that (aliases are recognised from the query that was sent)."""
    aliases = {m.group(1).replace('""', '"') for m in _DAX_ALIAS.finditer(dax)}

    def canonical(name: str) -> str:
        if name.startswith("[") and name.endswith("]"):
            return name
        if name in aliases:
            return f"[{name}]"
        if match := _QUALIFIED.match(name):
            return f"{match.group('table')}[{match.group('name')}]"
        if match := _DOTTED.match(name):
            return f"{match.group('table')}[{match.group('name')}]"
        return name

    mapping = {name: canonical(name) for row in rows[:1] for name in row}
    if all(k == v for k, v in mapping.items()):
        return rows
    return [{mapping.get(k, k): v for k, v in row.items()} for row in rows]


def _dax_error_text(result: CallToolResult) -> str | None:
    """A DAX failure reported as an ordinary text result (spike S3)."""
    text = _text_of(result).strip()
    return text if text.lower().startswith(_DAX_ERROR_PREFIXES) else None


def _structured(result: CallToolResult) -> Any:
    """Structured content that carries data. Spike S3: Fabric IQ puts only an
    `artifact_citation` (model name and link) there; the payload itself is the text."""
    structured = result.structured_content
    if not structured or set(structured) == {"artifact_citation"}:
        return None
    # Plain-string tool results arrive wrapped as {"result": "<json text>"}.
    if set(structured) == {"result"} and isinstance(structured["result"], str):
        parsed = parse_json_text(structured["result"])
        return parsed if parsed is not None else structured["result"]
    return structured


def _rows_from_result(result: CallToolResult) -> list[dict[str, Any]] | None:
    # The CSV resource carries the complete result; inline content may be a preview.
    csv_text = _csv_resource(result)
    if csv_text is not None:
        return rows_from_csv(csv_text)
    structured = _structured(result)
    if structured is not None:
        rows = rows_from_json(structured)
        if rows is not None:
            return rows
    for content in result.content:
        if isinstance(content, TextContent):
            rows = rows_from_json(parse_json_text(content.text))
            if rows is not None:
                return rows
    return None


def _payload_from_result(result: CallToolResult) -> Any:
    # Spike S3: the schema / value-search payload is JSON text; structured content is a citation.
    text = _text_of(result)
    parsed = parse_json_text(text)
    if parsed is not None:
        return parsed
    structured = _structured(result)
    return structured if structured is not None else text


def _classify_tool_error(message: str) -> PowerBIError | DaxQueryError:
    lowered = message.lower()
    if any(word in lowered for word in _ACCESS_WORDS):
        return PowerBIAccessDeniedError(message[:300])
    if any(word in lowered for word in _THROTTLE_WORDS):
        return PowerBIThrottledError(message[:300])
    if any(word in lowered for word in _TIMEOUT_WORDS):
        return PowerBITimeoutError(message[:300])
    return DaxQueryError(message)


def _classify_transport_error(exc: BaseException) -> PowerBIError:
    leaf = _first_leaf(exc)
    if isinstance(leaf, httpx2.TimeoutException):
        return PowerBITimeoutError(f"Fabric IQ timeout: {leaf!r}")
    status = getattr(getattr(leaf, "response", None), "status_code", None)
    if status == 429:
        return PowerBIThrottledError("Fabric IQ HTTP 429")
    # 401/403 here means the endpoint refused the caller (tenant setting, consent, region),
    # not that the user lacks access to a model: fall back instead of revoking.
    return GatewayUnavailableError(f"Fabric IQ unavailable: {type(leaf).__name__} {status or ''}")


def _first_leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc
