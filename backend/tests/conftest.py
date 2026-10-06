import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.entra import EntraAuthProvider
from app.auth.jwks import StaticKeySource
from app.core.config import POWERBI_WFE_CLIENT_ID, AuthProviderName, Environment, Settings
from app.main import create_app

TENANT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT_ID = "99999999-9999-9999-9999-999999999999"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
APP_ID_URI = "https://chatbot-api.contoso.com"
SCOPE = "discoverChatBot_CV_ForPBI"
KID = "test-key-1"
DEV_SECRET = "x" * 40

TokenFactory = Callable[..., str]


@pytest.fixture(scope="session")
def signing_key() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def entra_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        auth_provider=AuthProviderName.ENTRA,
        entra_client_id=CLIENT_ID,
        entra_app_id_uri=APP_ID_URI,
        entra_allowed_tenant_ids=[TENANT_ID],
        entra_required_scope=SCOPE,
        entra_client_secret="not-used-in-tests",
    )


@pytest.fixture
def make_token(signing_key: RSAPrivateKey) -> TokenFactory:
    """Builds an Entra-shaped v1 access token; override or drop (value None) any claim."""

    def _make(*, key: Any = None, kid: str = KID, alg: str = "RS256", **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "ver": "1.0",
            "iss": f"https://sts.windows.net/{TENANT_ID}/",
            "aud": APP_ID_URI,
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "tid": TENANT_ID,
            "oid": "user-a-oid",
            "upn": "user.a@contoso.com",
            "name": "User A",
            "scp": SCOPE,
            "appid": POWERBI_WFE_CLIENT_ID,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, key or signing_key, algorithm=alg, headers={"kid": kid})

    return _make


@pytest.fixture
def entra_app(entra_settings: Settings, signing_key: RSAPrivateKey) -> FastAPI:
    provider = EntraAuthProvider(
        entra_settings, key_source=StaticKeySource({KID: signing_key.public_key()})
    )
    return create_app(entra_settings, auth_provider=provider)


@pytest.fixture
async def client(entra_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=entra_app), base_url="http://test") as c:
        yield c
