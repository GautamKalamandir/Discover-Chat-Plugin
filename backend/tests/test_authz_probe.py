import asyncio

import httpx
import pytest

from app.auth.models import RequestContext
from app.auth.token_broker import TokenBroker
from app.authz.models import ProbeOutcome
from app.authz.probe import DevAccessProbe, PowerBiRestAccessProbe
from app.core.config import AuthProviderName, Environment, Settings
from tests.authz_helpers import request_ctx


class StaticBroker(TokenBroker):
    async def get_token(self, ctx: RequestContext, scope: str | None = None) -> str:
        return "pbi-user-token"


def rest_probe(handler: httpx.MockTransport, concurrency: int = 8) -> PowerBiRestAccessProbe:
    settings = Settings(_env_file=None, authz_probe_concurrency=concurrency)
    return PowerBiRestAccessProbe(settings, StaticBroker(), httpx.AsyncClient(transport=handler))


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, ProbeOutcome.ALLOWED),
        (400, ProbeOutcome.DENIED),  # id rejected by Power BI (live: non-GUID registry ids)
        (401, ProbeOutcome.DENIED),
        (403, ProbeOutcome.DENIED),
        (404, ProbeOutcome.DENIED),
        (429, ProbeOutcome.UNKNOWN),
        (500, ProbeOutcome.UNKNOWN),
        (503, ProbeOutcome.UNKNOWN),
    ],
)
async def test_power_bi_status_maps_to_outcome(status: int, outcome: ProbeOutcome) -> None:
    probe = rest_probe(httpx.MockTransport(lambda _: httpx.Response(status)))

    assert await probe.check(request_ctx(), ["ds-1"]) == {"ds-1": outcome}


async def test_network_failure_is_unknown() -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    probe = rest_probe(httpx.MockTransport(fail))

    assert await probe.check(request_ctx(), ["ds-1"]) == {"ds-1": ProbeOutcome.UNKNOWN}


async def test_probe_calls_get_dataset_with_the_users_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    await rest_probe(httpx.MockTransport(handler)).check(request_ctx(), ["abc-123"])

    assert str(seen[0].url) == "https://api.powerbi.com/v1.0/myorg/datasets/abc-123"
    assert seen[0].headers["authorization"] == "Bearer pbi-user-token"


async def test_parallel_checks_respect_the_concurrency_cap() -> None:
    in_flight = peak = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200)

    probe = rest_probe(httpx.MockTransport(handler), concurrency=3)
    result = await probe.check(request_ctx(), [f"ds-{i}" for i in range(12)])

    assert len(result) == 12
    assert peak == 3


async def test_dev_probe_uses_the_local_access_map() -> None:
    settings = Settings(
        _env_file=None,
        environment=Environment.LOCAL,
        auth_provider=AuthProviderName.DEV,
        dev_model_access={"user-a": ["sales-ds"]},
    )

    result = await DevAccessProbe(settings).check(request_ctx("user-a"), ["sales-ds", "hr-ds"])

    assert result == {"sales-ds": ProbeOutcome.ALLOWED, "hr-ds": ProbeOutcome.DENIED}
