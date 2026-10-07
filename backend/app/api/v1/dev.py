"""Local-development sign-in for the visual before the Entra app registration exists (ADR 0010).

Mounted ONLY when AUTH_PROVIDER=dev and ENVIRONMENT=local|test (see app.main). Anyone who can reach
a dev backend can mint a token for any dev user, which is the point of local development and the
reason it never exists anywhere else.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.auth.dev import mint_dev_token

TOKEN_MINUTES = 60

router = APIRouter(prefix="/dev", tags=["dev"])


class DevTokenRequest(BaseModel):
    oid: str = Field(pattern=r"^[A-Za-z0-9_.@-]{1,64}$")
    name: str | None = Field(default=None, max_length=100)


class DevTokenResponse(BaseModel):
    access_token: str
    expires_in: int


@router.post("/token")
async def dev_token(body: DevTokenRequest, request: Request) -> DevTokenResponse:
    token = mint_dev_token(
        request.app.state.settings, body.oid, name=body.name, minutes=TOKEN_MINUTES
    )
    return DevTokenResponse(access_token=token, expires_in=TOKEN_MINUTES * 60)
