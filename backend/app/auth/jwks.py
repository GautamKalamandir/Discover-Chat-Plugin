"""Signing keys for Entra-issued tokens, fetched from the JWKS endpoint and cached."""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
import jwt

logger = logging.getLogger(__name__)


class SigningKeySource(ABC):
    @abstractmethod
    async def get_signing_key(self, kid: str) -> Any:
        """Return the public key for `kid`, or raise `KeyError` if it is unknown."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""


class JwksKeySource(SigningKeySource):
    """Caches the JWKS document; refetches on TTL expiry or on an unknown `kid` (key rollover),
    but no more often than `min_refresh_interval` to avoid being driven by forged `kid` values."""

    def __init__(
        self,
        jwks_url: str,
        *,
        ttl_seconds: float = 24 * 3600,
        min_refresh_interval: float = 300,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._jwks_url = jwks_url
        self._ttl = ttl_seconds
        self._min_refresh_interval = min_refresh_interval
        self._client = http_client  # created on first use: building one loads the CA bundle
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def get_signing_key(self, kid: str) -> Any:
        if self._is_expired() or kid not in self._keys:
            await self._refresh(force=self._is_expired())
        try:
            return self._keys[kid]
        except KeyError:
            raise KeyError(kid) from None

    def _is_expired(self) -> bool:
        return self._fetched_at is None or time.monotonic() - self._fetched_at > self._ttl

    async def _refresh(self, *, force: bool) -> None:
        async with self._lock:
            now = time.monotonic()
            recently = (
                self._fetched_at is not None and now - self._fetched_at < self._min_refresh_interval
            )
            if recently and not force:
                return
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=10)
            response = await self._client.get(self._jwks_url)
            response.raise_for_status()
            keys: dict[str, Any] = {}
            for jwk in response.json().get("keys", []):
                if jwk.get("kid") and jwk.get("kty") == "RSA":
                    keys[jwk["kid"]] = jwt.PyJWK(jwk, algorithm="RS256").key
            self._keys = keys
            self._fetched_at = now
            logger.info("Loaded %d signing keys from JWKS", len(keys))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


class StaticKeySource(SigningKeySource):
    """Fixed keys; used in tests."""

    def __init__(self, keys: dict[str, Any]) -> None:
        self._keys = keys

    async def get_signing_key(self, kid: str) -> Any:
        return self._keys[kid]
