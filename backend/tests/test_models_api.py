"""HTTP end to end: dev auth -> G1/G2 -> /models endpoints, against the real test database."""

import time
from collections.abc import AsyncIterator

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.dev import DEV_AUDIENCE, DEV_ISSUER
from app.core.config import AuthProviderName, Environment, Settings
from app.main import create_app
from tests.authz_helpers import seed_registry
from tests.conftest import DEV_SECRET

pytestmark = pytest.mark.integration


def token(oid: str) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": DEV_ISSUER, "aud": DEV_AUDIENCE, "exp": now + 600, "oid": oid, "tid": "t1"}
    return {"Authorization": f"Bearer {jwt.encode(claims, DEV_SECRET, algorithm='HS256')}"}


@pytest.fixture
async def client(
    committed_sessionmaker: async_sessionmaker[AsyncSession], test_database_url: str
) -> AsyncIterator[AsyncClient]:
    await seed_registry(committed_sessionmaker)
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        auth_provider=AuthProviderName.DEV,
        dev_auth_secret=DEV_SECRET,
        database_url=test_database_url,
        cleanup_scheduler_enabled=False,
        dev_model_access={"user-a": ["sales-ds", "hr-ds", "inventory-ds"]},
    )
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.db_engine.dispose()


async def test_accessible_models_lists_only_the_users_models(client: AsyncClient) -> None:
    response = await client.get("/api/v1/models/accessible", headers=token("user-a"))

    assert response.status_code == 200
    assert response.json() == {
        "models": [
            {"id": "hr-ds", "name": "HR", "domain": "HR"},
            {"id": "inventory-ds", "name": "Inventory", "domain": "Inventory"},
            {"id": "sales-ds", "name": "Sales", "domain": "Sales"},
        ]
    }


async def test_user_without_grants_sees_no_models(client: AsyncClient) -> None:
    response = await client.get("/api/v1/models/accessible", headers=token("user-z"))

    assert response.json() == {"models": []}


async def test_allowed_model_is_returned(client: AsyncClient) -> None:
    response = await client.get("/api/v1/models/sales-ds", headers=token("user-a"))

    assert response.json() == {"id": "sales-ds", "name": "Sales", "domain": "Sales"}


async def test_forbidden_disabled_and_unknown_models_are_indistinguishable(
    client: AsyncClient,
) -> None:
    bodies = []
    for model_id in ("finance-ds", "draft-ds", "no-such-model"):
        response = await client.get(
            f"/api/v1/models/{model_id}",
            headers={**token("user-a"), "X-Correlation-ID": "same"},
        )
        assert response.status_code == 404
        bodies.append(response.json())

    assert bodies[0] == bodies[1] == bodies[2]
    assert "finance" not in str(bodies[0]).lower()


async def test_models_endpoints_require_sign_in(client: AsyncClient) -> None:
    for path in ("/api/v1/models/accessible", "/api/v1/models/sales-ds"):
        response = await client.get(path)
        assert response.status_code == 401
