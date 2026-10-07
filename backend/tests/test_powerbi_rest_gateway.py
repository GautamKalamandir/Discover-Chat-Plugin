import json
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.powerbi.errors import (
    DaxQueryError,
    GatewayUnavailableError,
    PowerBIAccessDeniedError,
    PowerBIThrottledError,
    PowerBITimeoutError,
)
from app.powerbi.rate_limit import PerUserRateLimiter
from app.powerbi.rest import PowerBiRestGateway

DAX = "EVALUATE VALUES(Product[LOB])"


def gateway(handler: Any, limiter: PerUserRateLimiter | None = None) -> PowerBiRestGateway:
    return PowerBiRestGateway(
        Settings(_env_file=None),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        limiter,
    )


def ok(rows: list[dict[str, Any]], **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={"results": [{"tables": [{"rows": rows}]}], **extra})


async def run(handler: Any, max_rows: int = 100) -> Any:
    return await gateway(handler).execute_dax("tok", "ds-1", DAX, max_rows=max_rows, user_key="u")


async def test_request_follows_the_documented_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok([{"Product[LOB]": "GOLD"}])

    result = await run(handler)

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.powerbi.com/v1.0/myorg/datasets/ds-1/executeQueries"
    assert request.headers["authorization"] == "Bearer tok"
    assert json.loads(request.content) == {
        "queries": [{"query": DAX}],
        "serializerSettings": {"includeNulls": True},
    }
    assert (result.rows, result.columns, result.truncated) == (
        [{"Product[LOB]": "GOLD"}],
        ["Product[LOB]"],
        False,
    )


@pytest.mark.scenario(18)
async def test_rows_beyond_max_rows_are_cut_and_flagged() -> None:
    result = await run(lambda _: ok([{"x": i} for i in range(5)]), max_rows=3)

    assert (result.row_count, result.truncated) == (3, True)


async def test_power_bi_row_limit_error_flags_truncation() -> None:
    body = {"error": {"code": "RowLimit", "message": "More than 100000 rows in a query result"}}

    result = await run(lambda _: ok([{"x": 1}], **body))

    assert result.truncated is True


async def test_query_error_inside_a_200_response() -> None:
    body = {"results": [{"error": {"code": "DAX", "message": "Column 'Foo' cannot be found"}}]}

    with pytest.raises(DaxQueryError) as excinfo:
        await run(lambda _: httpx.Response(200, json=body))

    assert "Column 'Foo'" in excinfo.value.dax_error


async def test_http_400_nested_dax_error_detail_is_extracted() -> None:
    body = {
        "error": {
            "code": "DatasetExecuteQueriesError",
            "pbi.error": {"details": [{"detail": {"value": "Query (1, 10) syntax error"}}]},
        }
    }

    with pytest.raises(DaxQueryError) as excinfo:
        await run(lambda _: httpx.Response(400, json=body))

    assert excinfo.value.dax_error == "Query (1, 10) syntax error"


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_refusals_are_access_denied(status: int) -> None:
    with pytest.raises(PowerBIAccessDeniedError):
        await run(lambda _: httpx.Response(status))


async def test_429_carries_retry_after() -> None:
    with pytest.raises(PowerBIThrottledError) as excinfo:
        await run(lambda _: httpx.Response(429, headers={"Retry-After": "7"}))

    assert excinfo.value.retry_after == 7


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_errors_mean_unavailable(status: int) -> None:
    with pytest.raises(GatewayUnavailableError):
        await run(lambda _: httpx.Response(status))


async def test_timeout_and_connection_errors() -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    def refused(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(PowerBITimeoutError):
        await run(timeout)
    with pytest.raises(GatewayUnavailableError):
        await run(refused)


@pytest.mark.scenario(19)
async def test_per_user_limit_stops_the_121st_query_per_minute() -> None:
    now = [0.0]
    limiter = PerUserRateLimiter(120, clock=lambda: now[0])
    sut = gateway(lambda _: ok([]), limiter)

    for _ in range(120):
        await sut.execute_dax("tok", "ds", DAX, max_rows=1, user_key="user-a")
    with pytest.raises(PowerBIThrottledError) as excinfo:
        await sut.execute_dax("tok", "ds", DAX, max_rows=1, user_key="user-a")
    await sut.execute_dax("tok", "ds", DAX, max_rows=1, user_key="user-b")  # other users unaffected

    assert excinfo.value.retry_after == pytest.approx(60)
    now[0] = 60.0
    await sut.execute_dax("tok", "ds", DAX, max_rows=1, user_key="user-a")  # window moved on
