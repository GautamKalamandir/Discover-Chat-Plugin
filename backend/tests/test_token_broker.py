import logging
from typing import Any

import pytest

from app.auth.models import AuthenticatedUser, RequestContext
from app.auth.token_broker import OboTokenBroker, UnavailableTokenBroker
from app.core.config import Settings
from app.core.errors import AppError

POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"


class FakeOboClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, list[str]]] = []

    def acquire_token_on_behalf_of(
        self, user_assertion: str, scopes: list[str], **_: Any
    ) -> dict[str, Any]:
        self.calls.append((user_assertion, scopes))
        return self.responses.pop(0)


def ctx(oid: str = "user-a", tid: str = "tenant-1", token: str = "inbound-token") -> RequestContext:
    user = AuthenticatedUser(oid, tid, None, None, frozenset(), None)
    return RequestContext(user=user, correlation_id="c1", access_token=token)


def broker(client: FakeOboClient, tenants: list[str] | None = None) -> OboTokenBroker:
    def factory(tenant_id: str) -> FakeOboClient:
        if tenants is not None:
            tenants.append(tenant_id)
        return client

    return OboTokenBroker(Settings(_env_file=None), client_factory=factory)


async def test_exchanges_user_assertion_for_powerbi_token() -> None:
    client = FakeOboClient([{"access_token": "pbi-token", "expires_in": 3600}])

    token = await broker(client).get_powerbi_token(ctx())

    assert token == "pbi-token"
    assert client.calls == [("inbound-token", [POWERBI_SCOPE])]


async def test_token_is_cached_per_user() -> None:
    client = FakeOboClient(
        [
            {"access_token": "token-a", "expires_in": 3600},
            {"access_token": "token-b", "expires_in": 3600},
        ]
    )
    sut = broker(client)

    assert await sut.get_powerbi_token(ctx("user-a")) == "token-a"
    assert await sut.get_powerbi_token(ctx("user-a")) == "token-a"
    assert await sut.get_powerbi_token(ctx("user-b")) == "token-b"
    assert len(client.calls) == 2


async def test_nearly_expired_token_is_refreshed() -> None:
    client = FakeOboClient(
        [
            {"access_token": "old", "expires_in": 60},
            {"access_token": "new", "expires_in": 3600},
        ]
    )
    sut = broker(client)

    await sut.get_powerbi_token(ctx())

    assert await sut.get_powerbi_token(ctx()) == "new"


async def test_exchange_runs_in_the_users_own_tenant() -> None:
    client = FakeOboClient([{"access_token": "t", "expires_in": 3600}])
    tenants: list[str] = []

    await broker(client, tenants).get_powerbi_token(ctx(tid="tenant-xyz"))

    assert tenants == ["tenant-xyz"]


@pytest.mark.parametrize(
    ("response", "status", "code"),
    [
        ({"error": "invalid_grant", "error_codes": [65001]}, 403, "consent_required"),
        ({"error": "invalid_grant", "error_codes": [50076]}, 401, "interaction_required"),
        ({"error": "interaction_required", "error_codes": []}, 401, "interaction_required"),
        ({"error": "invalid_client", "error_codes": [7000215]}, 502, "token_exchange_failed"),
    ],
)
async def test_exchange_failures_map_to_typed_errors(
    response: dict[str, Any], status: int, code: str
) -> None:
    with pytest.raises(AppError) as excinfo:
        await broker(FakeOboClient([response])).get_powerbi_token(ctx())

    assert excinfo.value.status_code == status
    assert excinfo.value.code == code


async def test_tokens_never_appear_in_logs_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client = FakeOboClient([{"access_token": "secret-pbi-token", "expires_in": 3600}])
    context = ctx(token="secret-inbound-token")

    await broker(client).get_powerbi_token(context)

    assert "secret" not in caplog.text
    assert "secret-inbound-token" not in repr(context)


async def test_dev_mode_broker_explains_power_bi_is_unavailable() -> None:
    with pytest.raises(AppError) as excinfo:
        await UnavailableTokenBroker().get_powerbi_token(ctx())

    assert excinfo.value.status_code == 503
