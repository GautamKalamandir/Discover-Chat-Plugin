"""The only way the rest of the backend reaches Power BI (ADR 0006).

Per call: gate G3 (model must be in the AuthorizedContext) -> read-only DAX check -> user's
Power BI token (OBO) -> primary gateway, falling back when it is unavailable -> retries for
throttling/timeouts -> typed, user-safe errors. A refusal from Power BI is re-checked live:
revoked access is cached as denied (generic message); access without Build permission gets its
own message.
"""

import asyncio
import functools
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from app.auth.token_broker import TokenBroker
from app.authz import messages
from app.authz.guards import requires_model_access
from app.authz.models import AuthorizedContext
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.powerbi.base import GatewayCapability, GatewayPayload, PowerBIGateway, QueryResult
from app.powerbi.dax_guard import ensure_read_only_query
from app.powerbi.errors import (
    CapabilityNotSupportedError,
    GatewayUnavailableError,
    PowerBIAccessDeniedError,
    PowerBIThrottledError,
    PowerBITimeoutError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_BACKOFF_SECONDS = 10.0


class AccessFeedback(Protocol):
    """The part of AuthorizationService this layer needs."""

    async def verify_live(self, authz: AuthorizedContext, dataset_id: str) -> bool: ...

    async def on_power_bi_denied(self, authz: AuthorizedContext, dataset_id: str) -> None: ...


class PowerBIService:
    def __init__(
        self,
        primary: PowerBIGateway,
        fallback: PowerBIGateway | None,
        token_broker: TokenBroker,
        access: AccessFeedback,
        settings: Settings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._gateways = [g for g in (primary, fallback) if g is not None]
        self._broker = token_broker
        self._access = access
        self._max_retries = settings.powerbi_max_retries
        self._default_rows = settings.powerbi_default_max_rows
        self._max_rows = settings.powerbi_max_rows_limit
        self._sleep = sleep

    @requires_model_access("model_id")
    async def execute_query(
        self, authz: AuthorizedContext, *, model_id: str, dax: str, max_rows: int | None = None
    ) -> QueryResult:
        ensure_read_only_query(dax)
        rows = min(max_rows or self._default_rows, self._max_rows)
        return await self._run(
            authz,
            model_id,
            GatewayCapability.EXECUTE,
            lambda g, token: g.execute_dax(
                token, model_id, dax, max_rows=rows, user_key=authz.request.user.key
            ),
        )

    @requires_model_access("model_id")
    async def get_schema(self, authz: AuthorizedContext, *, model_id: str) -> GatewayPayload:
        return await self._run(
            authz, model_id, GatewayCapability.SCHEMA, lambda g, t: g.get_schema(t, model_id)
        )

    @requires_model_access("model_id")
    async def search_values(
        self, authz: AuthorizedContext, *, model_id: str, terms: list[str]
    ) -> GatewayPayload:
        return await self._run(
            authz,
            model_id,
            GatewayCapability.VALUE_SEARCH,
            lambda g, t: g.search_values(t, model_id, terms),
        )

    # --- orchestration -------------------------------------------------------------------------

    async def _run(
        self,
        authz: AuthorizedContext,
        model_id: str,
        capability: GatewayCapability,
        operation: Callable[[PowerBIGateway, str], Awaitable[T]],
    ) -> T:
        gateways = [g for g in self._gateways if capability in g.capabilities]
        if not gateways:
            raise _unavailable(f"No gateway supports {capability}")

        for index, gateway in enumerate(gateways):
            is_last = index == len(gateways) - 1
            # Each path has its own token audience (Fabric API vs Power BI REST); both cached.
            token = await self._broker.get_token(authz.request, gateway.token_scope)
            try:
                return await self._with_retries(functools.partial(operation, gateway, token))
            except (GatewayUnavailableError, CapabilityNotSupportedError) as exc:
                logger.warning("Gateway %s unavailable for %s: %s", gateway.name, capability, exc)
            except PowerBIAccessDeniedError as exc:
                logger.info(
                    "Gateway %s refused %s for %s: %s",
                    gateway.name,
                    model_id,
                    authz.request.user.key,
                    exc,
                )
                if not await self._access.verify_live(authz, model_id):
                    await self._access.on_power_bi_denied(authz, model_id)
                    raise messages.access_denied(f"Power BI refused {model_id}: revoked") from exc
                if is_last:
                    if gateway.requires_build_permission:
                        # The user can see the model but this path refuses to query it.
                        raise _needs_build(model_id) from exc
                    raise messages.access_denied(
                        f"{gateway.name} refused {model_id} although access is confirmed"
                    ) from exc
            except PowerBIThrottledError as exc:
                raise AppError(
                    ErrorCode.POWERBI_THROTTLED,
                    "Power BI is busy right now. Please try again in a moment.",
                    429,
                    log_detail=str(exc),
                ) from exc
            except PowerBITimeoutError as exc:
                raise AppError(
                    ErrorCode.POWERBI_TIMEOUT,
                    "Power BI took too long to answer. Try a narrower question.",
                    504,
                    log_detail=str(exc),
                ) from exc
        raise _unavailable(f"All gateways unavailable for {capability}")

    async def _with_retries(self, attempt: Callable[[], Awaitable[T]]) -> T:
        for retry in range(self._max_retries + 1):
            try:
                return await attempt()
            except (PowerBIThrottledError, PowerBITimeoutError) as exc:
                if retry == self._max_retries:
                    raise
                hint = exc.retry_after if isinstance(exc, PowerBIThrottledError) else None
                delay = min(hint or (2**retry + random.random()), MAX_BACKOFF_SECONDS)  # noqa: S311
                logger.info("Retrying Power BI call in %.1fs after %s", delay, type(exc).__name__)
                await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def aclose(self) -> None:
        for gateway in self._gateways:
            await gateway.aclose()


def _unavailable(detail: str) -> AppError:
    return AppError(
        ErrorCode.POWERBI_UNAVAILABLE,
        "I can't reach Power BI right now. Please try again later.",
        503,
        log_detail=detail,
    )


def _needs_build(model_id: str) -> AppError:
    return AppError(
        ErrorCode.NEEDS_BUILD_PERMISSION,
        "You can view this data in Power BI, but your permissions don't allow the chatbot to "
        "query it. Please ask your Power BI administrator for Build permission.",
        403,
        log_detail=f"Query refused but access confirmed for {model_id}: Build permission missing",
    )
