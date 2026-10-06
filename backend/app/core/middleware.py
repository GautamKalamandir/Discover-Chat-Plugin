"""Pure ASGI middleware (no BaseHTTPMiddleware) so streaming responses (SSE) pass through intact."""

import re
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import ErrorCode, error_response
from app.core.request_context import set_correlation_id

CORRELATION_HEADER = "x-correlation-id"
_VALID_CORRELATION_ID = re.compile(r"^[A-Za-z0-9\-_.]{1,64}$")


class CorrelationIdMiddleware:
    """Accepts a well-formed inbound `X-Correlation-ID` or generates one; echoes it back."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = dict(scope["headers"]).get(CORRELATION_HEADER.encode(), b"").decode("latin-1")
        correlation_id = inbound if _VALID_CORRELATION_ID.match(inbound) else uuid.uuid4().hex
        set_correlation_id(correlation_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[CORRELATION_HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            set_correlation_id(None)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Cache-Control", "no-store")
                if self.hsts:
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """Rejects request bodies above `max_bytes` (declared Content-Length or streamed size)."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLargeError:
            await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(
            ErrorCode.PAYLOAD_TOO_LARGE, "Request body is too large.", status_code=413
        )
        await response(scope, receive, send)


class _BodyTooLargeError(Exception):
    pass
