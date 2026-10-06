from typing import Annotated

from fastapi import Depends, Request

from app.auth.base import AuthProvider
from app.auth.models import RequestContext
from app.auth.token_broker import TokenBroker
from app.core.errors import AppError, ErrorCode
from app.core.request_context import get_correlation_id

_MAX_TOKEN_LENGTH = 16 * 1024


def get_auth_provider(request: Request) -> AuthProvider:
    provider: AuthProvider = request.app.state.auth_provider
    return provider


def get_token_broker(request: Request) -> TokenBroker:
    broker: TokenBroker = request.app.state.token_broker
    return broker


def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise AppError(
            ErrorCode.MISSING_TOKEN,
            "Sign-in is required.",
            401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if len(token) > _MAX_TOKEN_LENGTH:
        raise AppError(ErrorCode.INVALID_TOKEN, "The access token is not valid.", 401)
    return token


async def get_request_context(
    request: Request,
    provider: Annotated[AuthProvider, Depends(get_auth_provider)],
) -> RequestContext:
    """Every protected route depends on this; no business code runs for an invalid token."""
    token = _extract_bearer_token(request)
    user = await provider.authenticate(token)
    return RequestContext(user=user, correlation_id=get_correlation_id(), access_token=token)


CurrentContext = Annotated[RequestContext, Depends(get_request_context)]
