"""Gate G3: every agent tool that touches a model checks it against the server-built context.

Usage (Phase 8):

    @requires_model_access("model_id")
    async def execute_query(authz: AuthorizedContext, *, model_id: str, dax: str) -> ...:

The check runs before the tool body, so a hallucinated or injected model id never reaches
Power BI. Models the agent routes to must first be approved via
`AuthorizationService.assert_allowed`, which is what extends the context.
"""

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

from app.authz import messages
from app.authz.models import AuthorizedContext

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


def requires_model_access(
    param: str = "model_id",
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        signature = inspect.signature(fn)
        if param not in signature.parameters:
            raise TypeError(f"{fn.__qualname__} has no parameter {param!r}")

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            bound = signature.bind(*args, **kwargs)
            authz = next(
                (v for v in bound.arguments.values() if isinstance(v, AuthorizedContext)), None
            )
            if authz is None:
                raise TypeError(f"{fn.__qualname__} must be called with an AuthorizedContext")

            value: Any = bound.arguments.get(param)
            requested = [value] if isinstance(value, str) else list(value or [])
            denied = [d for d in requested if not isinstance(d, str) or not authz.is_allowed(d)]
            if not requested or denied:
                logger.warning(
                    "Tool %s refused for models %s (user %s)",
                    fn.__qualname__,
                    denied or requested,
                    authz.request.user.key,
                )
                raise messages.access_denied(f"G3 refused {fn.__qualname__}: {denied}")
            return await fn(*args, **kwargs)

        return wrapper

    return decorator
