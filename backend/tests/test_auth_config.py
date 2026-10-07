import time

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.dev import DEV_AUDIENCE, DEV_ISSUER, DevAuthProvider
from app.auth.entra import EntraAuthProvider
from app.core.config import AuthProviderName, Environment, Settings
from app.core.errors import ConfigurationError
from app.main import create_app
from tests.conftest import DEV_SECRET


def dev_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": Environment.LOCAL,
        "auth_provider": AuthProviderName.DEV,
        "dev_auth_secret": DEV_SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


# --- fail fast on misconfiguration --------------------------------------------------------------


def test_entra_provider_refuses_to_start_without_configuration() -> None:
    settings = Settings(_env_file=None, auth_provider=AuthProviderName.ENTRA)

    with pytest.raises(ConfigurationError) as excinfo:
        EntraAuthProvider(settings)

    for name in ("ENTRA_CLIENT_ID", "ENTRA_APP_ID_URI", "ENTRA_ALLOWED_TENANT_IDS"):
        assert name in str(excinfo.value)


def test_app_refuses_to_start_when_auth_is_misconfigured() -> None:
    with pytest.raises(ConfigurationError):
        create_app(Settings(_env_file=None, auth_provider=AuthProviderName.ENTRA))


@pytest.mark.parametrize("environment", [Environment.DEV, Environment.PROD])
def test_dev_provider_is_refused_outside_local(environment: Environment) -> None:
    with pytest.raises(ConfigurationError, match="not allowed"):
        DevAuthProvider(dev_settings(environment=environment))


def test_dev_provider_requires_a_strong_secret() -> None:
    with pytest.raises(ConfigurationError, match="DEV_AUTH_SECRET"):
        DevAuthProvider(dev_settings(dev_auth_secret="short"))


# --- dev provider end to end --------------------------------------------------------------------


def dev_token(secret: str = DEV_SECRET, **overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": DEV_ISSUER,
        "aud": DEV_AUDIENCE,
        "nbf": now,
        "exp": now + 600,
        "oid": "dev-user",
        "tid": "dev-tenant",
        "name": "Dev User",
    }
    claims.update(overrides)
    return jwt.encode(claims, secret, algorithm="HS256")


async def test_dev_token_authenticates_in_local_mode() -> None:
    app = create_app(dev_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/session", headers={"Authorization": f"Bearer {dev_token()}"}
        )

    assert response.status_code == 200
    assert response.json()["user"]["object_id"] == "dev-user"
    assert response.json()["auth_provider"] == "dev"


@pytest.mark.parametrize(
    ("token", "code"),
    [
        (dev_token(secret="y" * 40), "invalid_token"),
        (dev_token(exp=int(time.time()) - 600), "token_expired"),
        (dev_token(aud="someone-else"), "invalid_token"),
        (dev_token(oid=None), "invalid_token"),
    ],
)
async def test_bad_dev_tokens_are_rejected(token: str, code: str) -> None:
    app = create_app(dev_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/session", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == code


async def test_dev_token_endpoint_issues_working_tokens_in_local_dev() -> None:
    app = create_app(dev_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        issued = await client.post("/api/v1/dev/token", json={"oid": "user-a", "name": "User A"})
        token = issued.json()["access_token"]
        session = await client.get("/api/v1/session", headers={"Authorization": f"Bearer {token}"})

    assert issued.status_code == 200 and issued.json()["expires_in"] == 3600
    assert session.json()["user"]["object_id"] == "user-a"


@pytest.mark.parametrize("oid", ["", "x" * 65, "bad id!", "../etc"])
async def test_dev_token_endpoint_validates_the_user_id(oid: str) -> None:
    app = create_app(dev_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/dev/token", json={"oid": oid})

    assert response.status_code == 422


async def test_dev_token_endpoint_does_not_exist_with_entra_auth(client: AsyncClient) -> None:
    # `client` is the Entra-configured app from conftest.
    response = await client.post("/api/v1/dev/token", json={"oid": "user-a"})

    assert response.status_code == 404
