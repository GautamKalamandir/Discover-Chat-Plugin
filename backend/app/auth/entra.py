"""Validation of Entra ID access tokens issued to the visual by the Power BI Authentication API.

Checks, in order: header (RS256 + kid) → signature → exp/nbf → audience → tenant allow-list →
issuer matches token version and tenant → required claims → requesting client app → scope.
"""

import logging
from typing import Any

import jwt

from app.auth.base import AuthProvider
from app.auth.jwks import JwksKeySource, SigningKeySource
from app.auth.models import AuthenticatedUser
from app.core.config import Environment, Settings
from app.core.errors import AppError, ConfigurationError, ErrorCode

logger = logging.getLogger(__name__)

_INVALID = "The access token is not valid."


def accepted_scopes(settings: Settings) -> frozenset[str]:
    """The visual's scope. Outside production also its developer-visual variant: `pbiviz start`
    serves the visual as `<guid>_DEBUG`, so Power BI asks Entra for `<guid>_DEBUG_CV_ForPBI`."""
    scope = settings.entra_required_scope or ""
    scopes = {scope}
    if settings.environment is not Environment.PROD and scope.endswith("_CV_ForPBI"):
        scopes.add(scope.removesuffix("_CV_ForPBI") + "_DEBUG_CV_ForPBI")
    return frozenset(scopes)


def _unauthorized(code: ErrorCode, detail: str, message: str = _INVALID) -> AppError:
    return AppError(
        code,
        message,
        401,
        log_detail=detail,
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


def _forbidden(code: ErrorCode, message: str, detail: str) -> AppError:
    return AppError(code, message, 403, log_detail=detail)


class EntraAuthProvider(AuthProvider):
    name = "entra"

    def __init__(self, settings: Settings, key_source: SigningKeySource | None = None) -> None:
        client_id = settings.entra_client_id
        app_id_uri = settings.entra_app_id_uri
        missing = [
            name
            for name, value in (
                ("ENTRA_CLIENT_ID", settings.entra_client_id),
                ("ENTRA_APP_ID_URI", settings.entra_app_id_uri),
                ("ENTRA_ALLOWED_TENANT_IDS", settings.entra_allowed_tenant_ids),
                ("ENTRA_REQUIRED_SCOPE", settings.entra_required_scope),
            )
            if not value
        ]
        if missing or not client_id or not app_id_uri:
            raise ConfigurationError(
                f"AUTH_PROVIDER=entra requires: {', '.join(missing)} (see docs/entra-setup.md)"
            )

        self._audiences = [app_id_uri.rstrip("/"), client_id]
        self._allowed_tenants = {t.lower() for t in settings.entra_allowed_tenant_ids}
        self._allowed_clients = {c.lower() for c in settings.entra_allowed_client_app_ids}
        self._accepted_scopes = accepted_scopes(settings)
        self._authority = settings.entra_authority_host.rstrip("/")
        self._leeway = settings.jwt_leeway_seconds
        self._keys = key_source or JwksKeySource(settings.entra_jwks_url)

    async def authenticate(self, token: str) -> AuthenticatedUser:
        claims = await self._decode(token)
        return self._authorize_claims(claims)

    async def _decode(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise _unauthorized(ErrorCode.INVALID_TOKEN, f"Malformed token header: {exc}") from exc

        if header.get("alg") != "RS256":
            raise _unauthorized(ErrorCode.INVALID_TOKEN, f"Rejected alg {header.get('alg')!r}")
        kid = header.get("kid")
        if not isinstance(kid, str):
            raise _unauthorized(ErrorCode.INVALID_TOKEN, "Token header has no kid")

        try:
            key = await self._keys.get_signing_key(kid)
        except KeyError as exc:
            raise _unauthorized(ErrorCode.INVALID_TOKEN, f"Unknown signing key {kid!r}") from exc

        try:
            return jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self._audiences,
                leeway=self._leeway,
                options={"require": ["exp", "nbf", "iss", "aud"], "verify_iss": False},
            )
        except jwt.ExpiredSignatureError as exc:
            raise _unauthorized(
                ErrorCode.TOKEN_EXPIRED, "Token expired", "The access token has expired."
            ) from exc
        except jwt.PyJWTError as exc:
            raise _unauthorized(ErrorCode.INVALID_TOKEN, f"Token rejected: {exc}") from exc

    def _authorize_claims(self, claims: dict[str, Any]) -> AuthenticatedUser:
        tenant_id = str(claims.get("tid", "")).lower()
        object_id = claims.get("oid")
        if not tenant_id or not isinstance(object_id, str) or not object_id:
            raise _unauthorized(ErrorCode.INVALID_TOKEN, "Token lacks tid/oid (not a user token)")

        # Issuer is checked only after the tenant is known to be allowed, against that tenant.
        if tenant_id not in self._allowed_tenants:
            raise _forbidden(
                ErrorCode.TENANT_NOT_ALLOWED,
                "Your organization is not enabled for this chatbot.",
                f"Tenant {tenant_id} not in allow-list",
            )
        expected_issuer = (
            f"{self._authority}/{tenant_id}/v2.0"
            if claims.get("ver") == "2.0"
            else f"https://sts.windows.net/{tenant_id}/"
        )
        if str(claims.get("iss", "")).lower() != expected_issuer.lower():
            raise _unauthorized(
                ErrorCode.INVALID_TOKEN,
                f"Issuer {claims.get('iss')!r} does not match {expected_issuer!r}",
            )

        client_app_id = claims.get("azp") or claims.get("appid")
        if not isinstance(client_app_id, str) or client_app_id.lower() not in self._allowed_clients:
            raise _forbidden(
                ErrorCode.CLIENT_NOT_ALLOWED,
                "This application is not allowed to call the chatbot.",
                f"Client app {client_app_id!r} not allowed",
            )

        scopes = frozenset(str(claims.get("scp", "")).split())
        if not scopes & self._accepted_scopes:
            raise _forbidden(
                ErrorCode.INSUFFICIENT_SCOPE,
                "The access token does not grant access to the chatbot.",
                f"Required scope {sorted(self._accepted_scopes)!r} missing from {sorted(scopes)}",
            )

        return AuthenticatedUser(
            object_id=object_id,
            tenant_id=tenant_id,
            username=claims.get("upn") or claims.get("preferred_username"),
            display_name=claims.get("name"),
            scopes=scopes,
            client_app_id=client_app_id,
        )

    async def aclose(self) -> None:
        await self._keys.aclose()
