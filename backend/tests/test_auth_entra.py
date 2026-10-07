"""Security scenarios 10 (no token), 13 (foreign tenant) and 14 (expired / wrong audience),
plus the remaining token-validation rules."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient

from app.core.config import POWERBI_DESKTOP_CLIENT_ID
from tests.conftest import APP_ID_URI, CLIENT_ID, OTHER_TENANT_ID, TENANT_ID, TokenFactory

SESSION = "/api/v1/session"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def assert_error(
    client: AsyncClient, headers: dict[str, str], status: int, code: str
) -> None:
    response = await client.get(SESSION, headers=headers)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


# --- valid tokens -------------------------------------------------------------------------------


async def test_valid_v1_token_returns_the_signed_in_user(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    response = await client.get(SESSION, headers=bearer(make_token()))

    assert response.status_code == 200
    body = response.json()
    assert body["user"] == {
        "object_id": "user-a-oid",
        "tenant_id": TENANT_ID,
        "username": "user.a@contoso.com",
        "display_name": "User A",
    }
    assert body["auth_provider"] == "entra"


async def test_valid_v2_token_is_accepted(client: AsyncClient, make_token: TokenFactory) -> None:
    token = make_token(
        ver="2.0",
        iss=f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
        aud=CLIENT_ID,
        appid=None,
        azp=POWERBI_DESKTOP_CLIENT_ID,
        upn=None,
        preferred_username="user.a@contoso.com",
    )

    response = await client.get(SESSION, headers=bearer(token))

    assert response.status_code == 200
    assert response.json()["user"]["username"] == "user.a@contoso.com"


async def test_response_never_contains_the_token(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    token = make_token()

    response = await client.get(SESSION, headers=bearer(token))

    assert token not in response.text


# --- scenario 10: no / malformed credentials ----------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": ""}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}],
)
@pytest.mark.scenario(10)
async def test_missing_token_is_rejected(client: AsyncClient, headers: dict[str, str]) -> None:
    response = await client.get(SESSION, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_token"
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_garbage_token_is_rejected(client: AsyncClient) -> None:
    await assert_error(client, bearer("not-a-jwt"), 401, "invalid_token")


async def test_oversized_token_is_rejected(client: AsyncClient) -> None:
    await assert_error(client, bearer("a" * 20_000), 401, "invalid_token")


# --- scenario 14: expired / not yet valid / wrong audience --------------------------------------


@pytest.mark.scenario(14)
async def test_expired_token_is_rejected(client: AsyncClient, make_token: TokenFactory) -> None:
    past = int(time.time()) - 7200
    token = make_token(iat=past, nbf=past, exp=past + 600)

    await assert_error(client, bearer(token), 401, "token_expired")


async def test_token_not_yet_valid_is_rejected(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    future = int(time.time()) + 3600
    await assert_error(client, bearer(make_token(nbf=future)), 401, "invalid_token")


@pytest.mark.parametrize(
    "audience",
    ["https://analysis.windows.net/powerbi/api", "https://evil.example.com", None],
)
@pytest.mark.scenario(14)
async def test_wrong_or_missing_audience_is_rejected(
    client: AsyncClient, make_token: TokenFactory, audience: str | None
) -> None:
    await assert_error(client, bearer(make_token(aud=audience)), 401, "invalid_token")


# --- scenario 13: tenant allow-list and issuer --------------------------------------------------


@pytest.mark.scenario(13)
async def test_token_from_other_tenant_is_forbidden(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    token = make_token(tid=OTHER_TENANT_ID, iss=f"https://sts.windows.net/{OTHER_TENANT_ID}/")

    await assert_error(client, bearer(token), 403, "tenant_not_allowed")


async def test_issuer_from_other_tenant_is_rejected(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    token = make_token(iss=f"https://sts.windows.net/{OTHER_TENANT_ID}/")

    await assert_error(client, bearer(token), 401, "invalid_token")


async def test_v1_issuer_on_v2_token_is_rejected(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    await assert_error(client, bearer(make_token(ver="2.0")), 401, "invalid_token")


@pytest.mark.parametrize("claim", ["oid", "tid"])
async def test_token_without_user_identity_is_rejected(
    client: AsyncClient, make_token: TokenFactory, claim: str
) -> None:
    await assert_error(client, bearer(make_token(**{claim: None})), 401, "invalid_token")


# --- signature and algorithm --------------------------------------------------------------------


async def test_token_signed_by_another_key_is_rejected(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    await assert_error(client, bearer(make_token(key=attacker_key)), 401, "invalid_token")


async def test_unknown_key_id_is_rejected(client: AsyncClient, make_token: TokenFactory) -> None:
    await assert_error(client, bearer(make_token(kid="unknown")), 401, "invalid_token")


async def test_hs256_token_is_rejected(client: AsyncClient) -> None:
    # Classic alg-confusion attempt: HMAC "signed" with a public value.
    token = jwt.encode(
        {"aud": APP_ID_URI, "tid": TENANT_ID, "oid": "x"},
        "public-value-used-as-secret-0123456789",
        algorithm="HS256",
        headers={"kid": "test-key-1"},
    )

    await assert_error(client, bearer(token), 401, "invalid_token")


async def test_unsigned_token_is_rejected(client: AsyncClient) -> None:
    token = jwt.encode({"aud": APP_ID_URI, "tid": TENANT_ID}, key="", algorithm="none")

    await assert_error(client, bearer(token), 401, "invalid_token")


async def test_tampered_payload_is_rejected(client: AsyncClient, make_token: TokenFactory) -> None:
    header, _, signature = make_token().split(".")
    forged_payload = make_token(oid="admin").split(".")[1]

    await assert_error(
        client, bearer(f"{header}.{forged_payload}.{signature}"), 401, "invalid_token"
    )


# --- requesting client app and scope ------------------------------------------------------------


async def test_token_requested_by_other_app_is_forbidden(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    token = make_token(appid="33333333-3333-3333-3333-333333333333")

    await assert_error(client, bearer(token), 403, "client_not_allowed")


@pytest.mark.parametrize("scope", [None, "", "User.Read", "other_CV_ForPBI"])
async def test_token_without_required_scope_is_forbidden(
    client: AsyncClient, make_token: TokenFactory, scope: str | None
) -> None:
    await assert_error(client, bearer(make_token(scp=scope)), 403, "insufficient_scope")
