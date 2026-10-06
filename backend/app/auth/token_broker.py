"""Exchanges the user's inbound token for a delegated Power BI token (OAuth 2.0 On-Behalf-Of).

The resulting token *is the user*: Power BI enforces workspace/item permissions, RLS and OLS on it.
Tokens are cached per user + scope and never logged.
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.auth.models import RequestContext
from app.core.config import AuthProviderName, Settings
from app.core.errors import AppError, ConfigurationError, ErrorCode

logger = logging.getLogger(__name__)

# Refresh cached tokens this many seconds before they expire.
EXPIRY_SKEW_SECONDS = 300
MAX_CACHED_TOKENS = 10_000

# AADSTS codes: consent missing / user or admin must interact (MFA, Conditional Access, etc.).
_CONSENT_CODES = {65001, 65004}
_INTERACTION_CODES = {50076, 50079, 50158, 53003, 50105}


class OboClient(Protocol):
    """The subset of `msal.ConfidentialClientApplication` used here."""

    def acquire_token_on_behalf_of(
        self, user_assertion: str, scopes: list[str], **kwargs: Any
    ) -> dict[str, Any]: ...


class TokenBroker(ABC):
    """Delegated tokens for the signed-in user.

    `scope=None` means the Power BI REST scope. Fabric IQ MCP needs the Fabric API scope instead:
    the same Entra resource (Power BI Service), so the same consent, but a different audience.
    """

    @abstractmethod
    async def get_token(self, ctx: RequestContext, scope: str | None = None) -> str: ...

    async def get_powerbi_token(self, ctx: RequestContext) -> str:
        return await self.get_token(ctx)


@dataclass(frozen=True)
class _CachedToken:
    access_token: str
    expires_at: float


class OboTokenBroker(TokenBroker):
    def __init__(
        self, settings: Settings, client_factory: Callable[[str], OboClient] | None = None
    ) -> None:
        self._default_scope = settings.powerbi_scope
        self._client_factory = client_factory or (lambda tid: _build_msal_client(settings, tid))
        # One MSAL client per tenant: the exchange must happen in the user's own tenant.
        self._clients: dict[str, OboClient] = {}
        self._cache: dict[str, _CachedToken] = {}
        self._lock = asyncio.Lock()

    async def get_token(self, ctx: RequestContext, scope: str | None = None) -> str:
        scopes = [scope or self._default_scope]
        cache_key = f"{ctx.user.key}|{scopes[0]}"
        cached = self._cache.get(cache_key)
        if cached and cached.expires_at - EXPIRY_SKEW_SECONDS > time.time():
            return cached.access_token

        # MSAL is synchronous (requests); keep the event loop free.
        client = self._clients.get(ctx.user.tenant_id)
        if client is None:
            client = self._clients.setdefault(
                ctx.user.tenant_id, self._client_factory(ctx.user.tenant_id)
            )
        result = await asyncio.to_thread(
            client.acquire_token_on_behalf_of,
            user_assertion=ctx.access_token,
            scopes=scopes,
        )
        if "access_token" not in result:
            raise _map_obo_error(result)

        expires_in = int(result.get("expires_in", 3600))
        async with self._lock:
            if len(self._cache) >= MAX_CACHED_TOKENS:
                self._evict_expired()
            self._cache[cache_key] = _CachedToken(result["access_token"], time.time() + expires_in)
        return str(result["access_token"])

    def _evict_expired(self) -> None:
        now = time.time()
        self._cache = {k: v for k, v in self._cache.items() if v.expires_at > now}
        if len(self._cache) >= MAX_CACHED_TOKENS:
            self._cache.clear()


class UnavailableTokenBroker(TokenBroker):
    """Used with AUTH_PROVIDER=dev: there is no Entra token to exchange."""

    async def get_token(self, ctx: RequestContext, scope: str | None = None) -> str:
        raise AppError(
            ErrorCode.SERVICE_MISCONFIGURED,
            "Power BI access is not available in local development mode.",
            503,
            log_detail="Token exchange requested with AUTH_PROVIDER=dev",
        )


def _map_obo_error(result: dict[str, Any]) -> AppError:
    codes = set(result.get("error_codes") or [])
    # Only the error code is logged: descriptions can echo request details.
    detail = f"OBO failed: error={result.get('error')!r} codes={sorted(codes)}"
    if codes & _CONSENT_CODES:
        return AppError(
            ErrorCode.CONSENT_REQUIRED,
            "Your organization's administrator must approve the chatbot's access to Power BI.",
            403,
            log_detail=detail,
        )
    if codes & _INTERACTION_CODES or result.get("error") == "interaction_required":
        return AppError(
            ErrorCode.INTERACTION_REQUIRED,
            "Additional sign-in verification is required. Please sign in to Power BI again.",
            401,
            log_detail=detail,
        )
    return AppError(
        ErrorCode.TOKEN_EXCHANGE_FAILED,
        "Could not obtain Power BI access for your account.",
        502,
        log_detail=detail,
    )


def _build_msal_client(settings: Settings, tenant_id: str) -> OboClient:
    import msal

    if not settings.entra_client_id:
        raise ConfigurationError("OBO requires ENTRA_CLIENT_ID")

    credential: str | dict[str, str]
    if settings.entra_client_certificate_path and settings.entra_client_certificate_thumbprint:
        with open(settings.entra_client_certificate_path, encoding="utf-8") as pem:
            credential = {
                "private_key": pem.read(),
                "thumbprint": settings.entra_client_certificate_thumbprint,
            }
    elif settings.entra_client_secret:
        credential = settings.entra_client_secret.get_secret_value()
    else:
        raise ConfigurationError(
            "OBO requires ENTRA_CLIENT_CERTIFICATE_PATH + ENTRA_CLIENT_CERTIFICATE_THUMBPRINT "
            "or ENTRA_CLIENT_SECRET"
        )

    authority = f"{settings.entra_authority_host.rstrip('/')}/{tenant_id}"
    client: OboClient = msal.ConfidentialClientApplication(
        client_id=settings.entra_client_id,
        client_credential=credential,
        authority=authority,
    )
    return client


def create_token_broker(settings: Settings) -> TokenBroker:
    if settings.auth_provider is AuthProviderName.DEV:
        return UnavailableTokenBroker()
    return OboTokenBroker(settings)
