"""Local-development authentication, used until the Entra app registration exists.

Accepts HS256 tokens signed with DEV_AUTH_SECRET (mint them with scripts/mint_dev_token.py).
Refuses to start outside ENVIRONMENT=local|test.
"""

import time
from typing import Any

import jwt

from app.auth.base import AuthProvider
from app.auth.models import AuthenticatedUser
from app.core.config import Environment, Settings
from app.core.errors import AppError, ConfigurationError, ErrorCode

DEV_ISSUER = "discover-chatbot-dev"
DEV_AUDIENCE = "discover-chatbot-api"
MIN_SECRET_LENGTH = 32


class DevAuthProvider(AuthProvider):
    name = "dev"

    def __init__(self, settings: Settings) -> None:
        if settings.environment not in (Environment.LOCAL, Environment.TEST):
            raise ConfigurationError(
                f"AUTH_PROVIDER=dev is not allowed in ENVIRONMENT={settings.environment}"
            )
        secret = settings.dev_auth_secret.get_secret_value() if settings.dev_auth_secret else ""
        if len(secret) < MIN_SECRET_LENGTH:
            raise ConfigurationError(
                f"AUTH_PROVIDER=dev requires DEV_AUTH_SECRET of at least {MIN_SECRET_LENGTH} chars"
            )
        self._secret = secret
        self._leeway = settings.jwt_leeway_seconds

    async def authenticate(self, token: str) -> AuthenticatedUser:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=self._secret,
                algorithms=["HS256"],
                audience=DEV_AUDIENCE,
                issuer=DEV_ISSUER,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud", "oid", "tid"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AppError(ErrorCode.TOKEN_EXPIRED, "The access token has expired.", 401) from exc
        except jwt.PyJWTError as exc:
            raise AppError(
                ErrorCode.INVALID_TOKEN, "The access token is not valid.", 401, log_detail=str(exc)
            ) from exc

        return AuthenticatedUser(
            object_id=str(claims["oid"]),
            tenant_id=str(claims["tid"]).lower(),
            username=claims.get("upn"),
            display_name=claims.get("name"),
            scopes=frozenset(str(claims.get("scp", "")).split()),
            client_app_id=DEV_ISSUER,
        )


def mint_dev_token(
    settings: Settings,
    oid: str,
    *,
    tid: str = "dev-tenant",
    name: str | None = None,
    upn: str | None = None,
    minutes: int = 60,
) -> str:
    """A token DevAuthProvider accepts. Only for AUTH_PROVIDER=dev (local development)."""
    DevAuthProvider(settings)  # same guards: dev provider, local/test environment, strong secret
    assert settings.dev_auth_secret is not None  # noqa: S101 - checked by DevAuthProvider
    now = int(time.time())
    claims = {
        "iss": DEV_ISSUER,
        "aud": DEV_AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + minutes * 60,
        "oid": oid,
        "tid": tid,
        "upn": upn or f"{oid}@dev.local",
        "name": name or oid,
    }
    return jwt.encode(claims, settings.dev_auth_secret.get_secret_value(), algorithm="HS256")
