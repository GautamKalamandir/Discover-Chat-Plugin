from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.auth.dependencies import CurrentContext

router = APIRouter(tags=["session"])


class SessionUser(BaseModel):
    object_id: str
    tenant_id: str
    username: str | None
    display_name: str | None


class SessionResponse(BaseModel):
    user: SessionUser
    auth_provider: str
    correlation_id: str | None


@router.get("/session")
async def get_session(ctx: CurrentContext, request: Request) -> SessionResponse:
    """Who the backend believes is signed in. Shown in the chatbot header."""
    return SessionResponse(
        user=SessionUser(
            object_id=ctx.user.object_id,
            tenant_id=ctx.user.tenant_id,
            username=ctx.user.username,
            display_name=ctx.user.display_name,
        ),
        auth_provider=request.app.state.auth_provider.name,
        correlation_id=ctx.correlation_id,
    )
