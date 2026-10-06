"""FastAPI dependencies = per-route authorization middleware (gates G1 and G2)."""

from typing import Annotated

from fastapi import Depends, Request

from app.auth.dependencies import CurrentContext
from app.authz import messages
from app.authz.models import AuthorizedContext, ModelSummary
from app.authz.service import AuthorizationService
from app.core.errors import AppError, ErrorCode


def get_authorization_service(request: Request) -> AuthorizationService:
    service: AuthorizationService = request.app.state.authz_service
    return service


AuthzService = Annotated[AuthorizationService, Depends(get_authorization_service)]


async def get_authorized_context(ctx: CurrentContext, service: AuthzService) -> AuthorizedContext:
    """G1: runs before every protected handler; resolves the user's allowed models."""
    return await service.build_context(ctx)


AuthorizedDep = Annotated[AuthorizedContext, Depends(get_authorized_context)]


async def require_model_access(
    model_id: str, authz: AuthorizedDep, service: AuthzService
) -> ModelSummary:
    """G2 for routes with `{model_id}` in the path.

    "Doesn't exist", "not enabled" and "not allowed" all return the same 404, so the route can't
    be used to discover which models exist.
    """
    try:
        authz = await service.assert_allowed(authz, [model_id])
    except AppError as exc:
        if exc.code is ErrorCode.MODEL_ACCESS_DENIED:
            raise messages.model_not_found(exc.log_detail or "denied") from exc
        raise
    return authz.allowed[model_id]


AuthorizedModel = Annotated[ModelSummary, Depends(require_model_access)]
