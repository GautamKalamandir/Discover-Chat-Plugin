"""Fallback path: `POST /v1.0/myorg/datasets/{id}/executeQueries` with the user's token.

Requires the tenant setting "Dataset Execute Queries REST API" and Read + Build on the model.
Limits: one query/table per call, 100k rows / 1M values / 15 MB, 120 queries/min/user.
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import Settings
from app.powerbi.base import GatewayCapability, PowerBIGateway, QueryResult
from app.powerbi.errors import (
    CapabilityNotSupportedError,
    DaxQueryError,
    GatewayUnavailableError,
    PowerBIAccessDeniedError,
    PowerBIThrottledError,
    PowerBITimeoutError,
)
from app.powerbi.rate_limit import PerUserRateLimiter
from app.powerbi.results import columns_of, require_rows, rows_from_json

logger = logging.getLogger(__name__)

_DENIED = {401, 403, 404}


class PowerBiRestGateway(PowerBIGateway):
    name = "rest"
    capabilities = frozenset({GatewayCapability.EXECUTE, GatewayCapability.REFRESH_INFO})
    requires_build_permission = True

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: PerUserRateLimiter | None = None,
    ) -> None:
        self._base_url = settings.powerbi_api_base_url.rstrip("/")
        self._timeout = settings.powerbi_query_timeout_seconds
        self._client = http_client  # created on first use: building one loads the CA bundle
        self._limiter = rate_limiter or PerUserRateLimiter(settings.powerbi_rest_queries_per_minute)

    async def execute_dax(
        self, user_token: str, dataset_id: str, dax: str, *, max_rows: int, user_key: str
    ) -> QueryResult:
        await self._limiter.acquire(user_key)
        try:
            response = await self._http().post(
                f"{self._base_url}/datasets/{dataset_id}/executeQueries",
                headers={"Authorization": f"Bearer {user_token}"},
                json={"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}},
            )
        except httpx.TimeoutException as exc:
            raise PowerBITimeoutError("executeQueries timed out") from exc
        except httpx.HTTPError as exc:
            raise GatewayUnavailableError(f"executeQueries transport error: {exc!r}") from exc

        status = response.status_code
        if status in _DENIED:
            raise PowerBIAccessDeniedError(f"executeQueries HTTP {status}")
        if status == 429:
            raise PowerBIThrottledError("executeQueries throttled", _retry_after(response))
        if status == 400:
            raise DaxQueryError(_error_message(_json(response)) or "Bad request")
        if status != 200:
            raise GatewayUnavailableError(f"executeQueries HTTP {status}")

        body = _json(response)
        result = (body.get("results") or [{}])[0] if isinstance(body, dict) else {}
        if isinstance(result, dict) and result.get("error"):
            raise DaxQueryError(_error_message(result) or "Query failed")
        rows = require_rows(rows_from_json(body), "executeQueries")
        # Power BI reports its own row-limit truncation as an error alongside partial rows.
        truncated = bool(_error_message(body)) or len(rows) > max_rows
        return QueryResult(
            columns=columns_of(rows), rows=rows[:max_rows], truncated=truncated, gateway=self.name
        )

    async def last_refresh(self, user_token: str, dataset_id: str) -> datetime | None:
        """`GET datasets/{id}/refreshes`: the newest completed refresh (ADR 0015).

        Viewers may not be allowed to read refresh history. A refusal here only means "not
        available": it must never be taken for lost access to the model's data."""
        try:
            response = await self._http().get(
                f"{self._base_url}/datasets/{dataset_id}/refreshes",
                headers={"Authorization": f"Bearer {user_token}"},
                params={"$top": "5"},
            )
        except httpx.TimeoutException as exc:
            raise PowerBITimeoutError("refreshes timed out") from exc
        except httpx.HTTPError as exc:
            raise GatewayUnavailableError(f"refreshes transport error: {exc!r}") from exc

        status = response.status_code
        if status in _DENIED:
            raise CapabilityNotSupportedError(f"refreshes HTTP {status}")
        if status == 429:
            raise PowerBIThrottledError("refreshes throttled", _retry_after(response))
        if status != 200:
            raise GatewayUnavailableError(f"refreshes HTTP {status}")
        body = _json(response)
        refreshes = body.get("value") if isinstance(body, dict) else None
        for refresh in refreshes if isinstance(refreshes, list) else []:
            if isinstance(refresh, dict) and refresh.get("status") == "Completed":
                return _parse_time(refresh.get("endTime"))
        return None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


def _error_message(body: Any) -> str | None:
    """Pulls the most specific message from Power BI's nested error shapes."""
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    pbi_error = error.get("pbi.error")
    details = pbi_error.get("details") if isinstance(pbi_error, dict) else None
    if isinstance(details, list):
        for detail in details:
            value = detail.get("detail", {}).get("value") if isinstance(detail, dict) else None
            if value:
                # Power BI marks names as <oii>…</oii> (spike S4); the repair loop wants plain text.
                return re.sub(r"</?oii>", "", str(value))
    message = error.get("message") or error.get("code")
    return str(message) if message else None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None
