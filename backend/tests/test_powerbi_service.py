"""PowerBIService orchestration: G3, read-only check, fallback, retries, revocation, Build."""

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from app.auth.models import RequestContext
from app.auth.token_broker import TokenBroker
from app.authz.messages import GENERIC_DENIAL
from app.authz.models import AuthorizedContext, ModelSummary
from app.core.config import Settings
from app.core.errors import AppError
from app.powerbi.base import GatewayCapability, GatewayPayload, PowerBIGateway, QueryResult
from app.powerbi.errors import (
    DaxQueryError,
    GatewayUnavailableError,
    PowerBIAccessDeniedError,
    PowerBIThrottledError,
    PowerBITimeoutError,
)
from app.powerbi.service import PowerBIService
from tests.authz_helpers import request_ctx

DAX = 'EVALUATE ROW("x", 1)'


class ScriptedGateway(PowerBIGateway):
    """Raises/returns the scripted outcomes in order, one per call."""

    def __init__(
        self,
        name: str,
        outcomes: list[Exception | str],
        *,
        build: bool = False,
        capabilities: frozenset[GatewayCapability] = frozenset(GatewayCapability),
    ) -> None:
        self.name = name
        self.outcomes = outcomes
        self.requires_build_permission = build
        self.capabilities = capabilities
        self.calls: list[dict[str, Any]] = []

    async def execute_dax(
        self, user_token: str, dataset_id: str, dax: str, *, max_rows: int, user_key: str
    ) -> QueryResult:
        self.calls.append({"token": user_token, "dataset": dataset_id, "max_rows": max_rows})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return QueryResult(columns=["x"], rows=[{"x": outcome}], truncated=False, gateway=self.name)

    async def get_schema(self, user_token: str, dataset_id: str) -> GatewayPayload:
        self.calls.append({"schema": dataset_id})
        return GatewayPayload(data={"tables": []}, gateway=self.name)


class Broker(TokenBroker):
    calls = 0
    scopes: ClassVar[list[str | None]] = []

    async def get_token(self, ctx: RequestContext, scope: str | None = None) -> str:
        Broker.calls += 1
        Broker.scopes.append(scope)
        return "pbi-token"


class Access:
    def __init__(self, still_allowed: bool) -> None:
        self.still_allowed = still_allowed
        self.revoked: list[str] = []

    async def verify_live(self, authz: AuthorizedContext, dataset_id: str) -> bool:
        return self.still_allowed

    async def on_power_bi_denied(self, authz: AuthorizedContext, dataset_id: str) -> None:
        self.revoked.append(dataset_id)


def authz() -> AuthorizedContext:
    sales = ModelSummary("sales-ds", "Sales", "Sales", uuid.uuid4())
    return AuthorizedContext(request_ctx(), uuid.uuid4(), {"sales-ds": sales}, datetime.now(UTC))


def service(
    primary: PowerBIGateway,
    fallback: PowerBIGateway | None = None,
    access: Access | None = None,
    **settings: Any,
) -> tuple[PowerBIService, list[float]]:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    svc = PowerBIService(
        primary,
        fallback,
        Broker(),
        access or Access(still_allowed=True),
        Settings(_env_file=None, **settings),
        sleep=sleep,
    )
    return svc, sleeps


async def test_query_runs_on_primary_with_the_users_token() -> None:
    primary = ScriptedGateway("fabric_iq_mcp", ["ok"])
    svc, _ = service(primary)

    result = await svc.execute_query(authz(), model_id="sales-ds", dax=DAX)

    assert result.rows == [{"x": "ok"}]
    assert primary.calls == [{"token": "pbi-token", "dataset": "sales-ds", "max_rows": 250}]


@pytest.mark.scenario(18)
async def test_max_rows_is_capped_at_the_configured_limit() -> None:
    primary = ScriptedGateway("p", ["ok"])
    svc, _ = service(primary)

    await svc.execute_query(authz(), model_id="sales-ds", dax=DAX, max_rows=50_000)

    assert primary.calls[0]["max_rows"] == 1000


@pytest.mark.scenario(7)
async def test_g3_refuses_other_models_before_any_token_exchange() -> None:
    primary = ScriptedGateway("p", ["ok"])
    svc, _ = service(primary)
    Broker.calls = 0

    with pytest.raises(AppError) as excinfo:
        await svc.execute_query(authz(), model_id="finance-ds", dax=DAX)

    assert excinfo.value.message == GENERIC_DENIAL
    assert (Broker.calls, primary.calls) == (0, [])


@pytest.mark.parametrize(
    "dax", ["", "SELECT * FROM x", "EVALUATE $SYSTEM.TMSCHEMA_TABLES", "EVALUATE INFO.TABLES()"]
)
@pytest.mark.scenario(17)
async def test_non_query_dax_never_reaches_power_bi(dax: str) -> None:
    primary = ScriptedGateway("p", ["ok"])
    svc, _ = service(primary)

    with pytest.raises(AppError) as excinfo:
        await svc.execute_query(authz(), model_id="sales-ds", dax=dax)

    assert excinfo.value.code == "query_rejected"
    assert primary.calls == []


async def test_falls_back_when_primary_is_unavailable() -> None:
    primary = ScriptedGateway("fabric_iq_mcp", [GatewayUnavailableError("region")])
    fallback = ScriptedGateway("rest", ["from rest"], build=True)
    svc, _ = service(primary, fallback)

    result = await svc.execute_query(authz(), model_id="sales-ds", dax=DAX)

    assert result.gateway == "rest"


async def test_dax_errors_do_not_fall_back() -> None:
    primary = ScriptedGateway("fabric_iq_mcp", [DaxQueryError("Column 'Foo' not found")])
    fallback = ScriptedGateway("rest", ["unused"])
    svc, _ = service(primary, fallback)

    with pytest.raises(DaxQueryError):
        await svc.execute_query(authz(), model_id="sales-ds", dax=DAX)

    assert fallback.calls == []


async def test_throttling_is_retried_with_backoff_then_succeeds() -> None:
    primary = ScriptedGateway("p", [PowerBIThrottledError("busy", retry_after=3), "ok"])
    svc, sleeps = service(primary)

    result = await svc.execute_query(authz(), model_id="sales-ds", dax=DAX)

    assert result.rows == [{"x": "ok"}]
    assert sleeps == [3]


@pytest.mark.scenario(19)
async def test_persistent_throttling_becomes_a_friendly_429() -> None:
    primary = ScriptedGateway("p", [PowerBIThrottledError("busy")] * 3)
    svc, sleeps = service(primary, powerbi_max_retries=2)

    with pytest.raises(AppError) as excinfo:
        await svc.execute_query(authz(), model_id="sales-ds", dax=DAX)

    assert (excinfo.value.status_code, excinfo.value.code) == (429, "powerbi_throttled")
    assert len(sleeps) == 2
    assert all(s <= 10 for s in sleeps)


async def test_persistent_timeout_becomes_504() -> None:
    svc, _ = service(ScriptedGateway("p", [PowerBITimeoutError("slow")] * 3))

    with pytest.raises(AppError) as excinfo:
        await svc.execute_query(authz(), model_id="sales-ds", dax=DAX)

    assert excinfo.value.status_code == 504


async def test_all_gateways_unavailable_is_503() -> None:
    svc, _ = service(
        ScriptedGateway("p", [GatewayUnavailableError("x")]),
        ScriptedGateway("rest", [GatewayUnavailableError("y")]),
    )

    with pytest.raises(AppError) as excinfo:
        await svc.execute_query(authz(), model_id="sales-ds", dax=DAX)

    assert (excinfo.value.status_code, excinfo.value.code) == (503, "powerbi_unavailable")


@pytest.mark.scenario(5)
async def test_refusal_with_access_gone_revokes_and_denies_generically() -> None:
    access = Access(still_allowed=False)
    fallback = ScriptedGateway("rest", ["unused"])
    svc, _ = service(ScriptedGateway("p", [PowerBIAccessDeniedError("401")]), fallback, access)

    with pytest.raises(AppError) as excinfo:
        await svc.execute_query(authz(), model_id="sales-ds", dax=DAX)

    assert excinfo.value.message == GENERIC_DENIAL
    assert access.revoked == ["sales-ds"]
    assert fallback.calls == []


async def test_mcp_refusal_with_access_confirmed_tries_rest() -> None:
    primary = ScriptedGateway("fabric_iq_mcp", [PowerBIAccessDeniedError("odd")])
    fallback = ScriptedGateway("rest", ["ok"], build=True)
    access = Access(still_allowed=True)
    svc, _ = service(primary, fallback, access)

    result = await svc.execute_query(authz(), model_id="sales-ds", dax=DAX)

    assert result.gateway == "rest"
    assert access.revoked == []


@pytest.mark.scenario(3)
async def test_rest_refusal_with_access_confirmed_means_missing_build_permission() -> None:
    access = Access(still_allowed=True)
    svc, _ = service(
        ScriptedGateway("rest", [PowerBIAccessDeniedError("403")], build=True), access=access
    )

    with pytest.raises(AppError) as excinfo:
        await svc.execute_query(authz(), model_id="sales-ds", dax=DAX)

    assert (excinfo.value.status_code, excinfo.value.code) == (403, "needs_build_permission")
    assert "Sales" not in excinfo.value.message
    assert access.revoked == []


async def test_schema_skips_gateways_without_the_capability() -> None:
    rest = ScriptedGateway("rest", [], capabilities=frozenset({GatewayCapability.EXECUTE}))
    mcp = ScriptedGateway("fabric_iq_mcp", [])
    svc, _ = service(rest, mcp)

    payload = await svc.get_schema(authz(), model_id="sales-ds")

    assert payload.gateway == "fabric_iq_mcp"
    assert rest.calls == []


async def test_schema_unavailable_when_no_gateway_supports_it() -> None:
    rest = ScriptedGateway("rest", [], capabilities=frozenset({GatewayCapability.EXECUTE}))
    svc, _ = service(rest)

    with pytest.raises(AppError) as excinfo:
        await svc.get_schema(authz(), model_id="sales-ds")

    assert excinfo.value.status_code == 503


async def test_each_gateway_gets_a_token_for_its_own_audience() -> None:
    primary = ScriptedGateway("fabric_iq_mcp", [GatewayUnavailableError("down")])
    primary.token_scope = "https://api.fabric.microsoft.com/.default"
    fallback = ScriptedGateway("rest", ["ok"], build=True)
    svc, _ = service(primary, fallback)
    Broker.scopes = []

    await svc.execute_query(authz(), model_id="sales-ds", dax=DAX)

    assert Broker.scopes == ["https://api.fabric.microsoft.com/.default", None]
