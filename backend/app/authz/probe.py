"""Asks Power BI — as the user — whether they can access given semantic models."""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence

import httpx

from app.auth.models import RequestContext
from app.auth.token_broker import TokenBroker
from app.authz.models import ProbeOutcome
from app.core.config import AuthProviderName, Settings

logger = logging.getLogger(__name__)

# 400: Power BI rejects the id itself (e.g. not a dataset GUID); it can never be accessible, so it's
# a denial (cached) rather than "unknown" (re-asked on every request). Fail-closed either way.
_DENIED_STATUSES = {400, 401, 403, 404}


class ModelAccessProbe(ABC):
    source: str

    @abstractmethod
    async def check(
        self, ctx: RequestContext, dataset_ids: Sequence[str]
    ) -> dict[str, ProbeOutcome]: ...

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""


class PowerBiRestAccessProbe(ModelAccessProbe):
    """`GET /v1.0/myorg/datasets/{id}` with the user's delegated token.

    Works for models in any workspace "provided the caller has the required permissions", which
    includes models shared with the user directly (no workspace role needed).
    """

    source = "powerbi_rest"

    def __init__(
        self,
        settings: Settings,
        token_broker: TokenBroker,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = settings.powerbi_api_base_url.rstrip("/")
        self._broker = token_broker
        self._concurrency = settings.authz_probe_concurrency
        self._timeout = settings.authz_probe_timeout_seconds
        self._client = http_client  # created on first use: building one loads the CA bundle

    async def check(
        self, ctx: RequestContext, dataset_ids: Sequence[str]
    ) -> dict[str, ProbeOutcome]:
        if not dataset_ids:
            return {}
        token = await self._broker.get_powerbi_token(ctx)
        semaphore = asyncio.Semaphore(self._concurrency)

        async def one(dataset_id: str) -> tuple[str, ProbeOutcome]:
            async with semaphore:
                return dataset_id, await self._check_one(token, dataset_id)

        return dict(await asyncio.gather(*(one(d) for d in dataset_ids)))

    async def _check_one(self, token: str, dataset_id: str) -> ProbeOutcome:
        try:
            response = await self._http().get(
                f"{self._base_url}/datasets/{dataset_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            logger.warning("Access probe for %s failed: %s", dataset_id, type(exc).__name__)
            return ProbeOutcome.UNKNOWN
        if response.status_code == 200:
            return ProbeOutcome.ALLOWED
        if response.status_code in _DENIED_STATUSES:
            return ProbeOutcome.DENIED
        logger.warning("Access probe for %s returned HTTP %d", dataset_id, response.status_code)
        return ProbeOutcome.UNKNOWN

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


class DevAccessProbe(ModelAccessProbe):
    """AUTH_PROVIDER=dev only: answers from DEV_MODEL_ACCESS instead of Power BI."""

    source = "dev"

    def __init__(self, settings: Settings) -> None:
        self._access = {oid: set(ids) for oid, ids in settings.dev_model_access.items()}

    async def check(
        self, ctx: RequestContext, dataset_ids: Sequence[str]
    ) -> dict[str, ProbeOutcome]:
        granted = self._access.get(ctx.user.object_id, set())
        return {
            d: ProbeOutcome.ALLOWED if d in granted else ProbeOutcome.DENIED for d in dataset_ids
        }


def create_access_probe(settings: Settings, token_broker: TokenBroker) -> ModelAccessProbe:
    if settings.auth_provider is AuthProviderName.DEV:
        return DevAccessProbe(settings)
    return PowerBiRestAccessProbe(settings, token_broker)
