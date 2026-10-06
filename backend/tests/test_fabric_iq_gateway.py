"""FabricIqMcpGateway against an in-process MCP server speaking Fabric IQ's tool contract."""

import pytest

from app.core.config import Settings
from app.powerbi.errors import (
    DaxQueryError,
    GatewayUnavailableError,
    PowerBIAccessDeniedError,
    PowerBIThrottledError,
    PowerBITimeoutError,
)
from app.powerbi.fabric_iq import FabricIqMcpGateway
from tests.fake_fabric_iq import ROWS, FakeFabricIq, running

SETTINGS = Settings(_env_file=None, fabric_iq_mcp_url="http://fabric-iq.test/mcp")
DAX = 'EVALUATE SUMMARIZECOLUMNS(Product[LOB], "Net Sales", [Net Sales])'


async def execute(fake: FakeFabricIq, max_rows: int = 250) -> tuple[FabricIqMcpGateway, object]:
    error: Exception | None = None
    async with running(fake) as factory:
        gateway = FabricIqMcpGateway(SETTINGS, http_client_factory=factory)
        try:
            result = await gateway.execute_dax(
                "user-pbi-token", "sales-model-guid", DAX, max_rows=max_rows, user_key="t:u"
            )
        except Exception as exc:  # re-raised outside the fake server's task group
            error = exc
    if error is not None:
        raise error
    return gateway, result


async def test_execute_query_sends_fabric_iq_arguments_and_parses_rows() -> None:
    fake = FakeFabricIq()

    _, result = await execute(fake, max_rows=100)

    assert fake.calls == [
        ("ExecuteQuery", {"artifactId": "sales-model-guid", "daxQueries": [DAX], "maxRows": 100})
    ]
    assert result.rows == ROWS  # type: ignore[attr-defined]
    assert result.columns == ["Product[LOB]", "[Net Sales]"]  # type: ignore[attr-defined]
    assert result.gateway == "fabric_iq_mcp"  # type: ignore[attr-defined]


async def test_every_request_carries_the_users_token_and_pinned_variant() -> None:
    fake = FakeFabricIq()

    await execute(fake)

    assert fake.requests  # initialize, tools/list, tools/call, ...
    for headers in fake.requests:
        assert headers["authorization"] == "Bearer user-pbi-token"
        assert headers["x-variants"] == "Fabric.Routing.FabricIQ.V1"


async def test_csv_resource_is_used_for_the_full_result() -> None:
    _, result = await execute(FakeFabricIq(execute_mode="csv"))

    assert result.rows == [  # type: ignore[attr-defined]
        {"Product[LOB]": "GOLD", "[Net Sales]": "1200.5"},
        {"Product[LOB]": "SILVER", "[Net Sales]": "800"},
    ]


async def test_result_at_the_row_limit_is_flagged_as_possibly_truncated() -> None:
    _, result = await execute(FakeFabricIq(), max_rows=2)

    assert result.truncated is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("Query (1, 10) The syntax for 'bad' is incorrect.", DaxQueryError),
        (
            "Unauthorized: the user does not have permission on this artifact",
            PowerBIAccessDeniedError,
        ),
        ("Request was throttled, try again shortly", PowerBIThrottledError),
        ("The query timed out while the model was loading", PowerBITimeoutError),
    ],
)
async def test_tool_errors_are_classified(message: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        await execute(FakeFabricIq(execute_mode=f"error:{message}"))


async def test_dax_error_text_is_kept_for_the_repair_loop() -> None:
    with pytest.raises(DaxQueryError) as excinfo:
        await execute(FakeFabricIq(execute_mode="error:Column 'Foo' cannot be found"))

    assert "Column 'Foo' cannot be found" in excinfo.value.dax_error
    assert "Foo" not in excinfo.value.message  # the user-facing text stays generic


@pytest.mark.parametrize("status", [401, 403, 404, 503])
async def test_endpoint_refusal_means_unavailable_not_revoked(status: int) -> None:
    # Tenant setting off / unsupported region / outage: fall back, never revoke the user's model.
    with pytest.raises(GatewayUnavailableError):
        await execute(FakeFabricIq(endpoint_status=status))


async def test_contract_mismatch_makes_the_gateway_unavailable() -> None:
    with pytest.raises(GatewayUnavailableError, match="ExecuteQuery"):
        await execute(FakeFabricIq(omit_tools={"ExecuteQuery"}))


async def test_contract_is_checked_once_per_gateway() -> None:
    fake = FakeFabricIq()
    async with running(fake) as factory:
        gateway = FabricIqMcpGateway(SETTINGS, http_client_factory=factory)
        for _ in range(3):
            await gateway.execute_dax("t", "m", DAX, max_rows=10, user_key="k")

    bodies = [r for r in fake.requests if r.get("content-type", "").startswith("application/json")]
    assert len(bodies) > 3  # sanity: requests were made
    assert gateway._contract_checked is True


async def test_schema_and_value_search_use_artifact_id() -> None:
    fake = FakeFabricIq()
    async with running(fake) as factory:
        gateway = FabricIqMcpGateway(SETTINGS, http_client_factory=factory)
        schema = await gateway.get_schema("t", "sales-model-guid")
        values = await gateway.search_values("t", "sales-model-guid", ["gold"])

    assert schema.data == {"tables": [{"name": "Product", "columns": ["LOB"]}]}
    assert values.data == [{"column": "Product[LOB]", "value": "GOLD"}]
    assert (
        "ValueSearch",
        {"artifactId": "sales-model-guid", "searchTerms": ["gold"]},
    ) in fake.calls
