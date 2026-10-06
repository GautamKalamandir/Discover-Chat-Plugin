"""Uniform error model.

Every error leaves the API as `{"error": {"code", "message", "correlation_id"}}` so the visual can
react to a stable `code` without parsing messages. Internal details are logged, never returned.
"""

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_context import get_correlation_id

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    # 400 / 404 / 405 / 413 / 422
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    VALIDATION_ERROR = "validation_error"
    QUERY_REJECTED = "query_rejected"
    QUERY_FAILED = "query_failed"
    POWERBI_THROTTLED = "powerbi_throttled"
    SESSION_NOT_FOUND = "session_not_found"
    MODEL_NOT_FOUND = "model_not_found"
    # 401 — the visual should acquire a fresh token and retry once
    MISSING_TOKEN = "missing_token"
    INVALID_TOKEN = "invalid_token"
    TOKEN_EXPIRED = "token_expired"
    INTERACTION_REQUIRED = "interaction_required"
    # 403 — retrying with a new token will not help
    TENANT_NOT_ALLOWED = "tenant_not_allowed"
    CLIENT_NOT_ALLOWED = "client_not_allowed"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    CONSENT_REQUIRED = "consent_required"
    MODEL_ACCESS_DENIED = "model_access_denied"
    NEEDS_BUILD_PERMISSION = "needs_build_permission"
    # 5xx
    TOKEN_EXCHANGE_FAILED = "token_exchange_failed"
    SERVICE_MISCONFIGURED = "service_misconfigured"
    ACCESS_CHECK_UNAVAILABLE = "access_check_unavailable"
    POWERBI_UNAVAILABLE = "powerbi_unavailable"
    POWERBI_TIMEOUT = "powerbi_timeout"
    INTERNAL_ERROR = "internal_error"


class AppError(Exception):
    """An error that is safe to show to the client (`message` must not contain internals)."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status_code: int,
        *,
        log_detail: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.log_detail = log_detail
        self.headers = headers


class ConfigurationError(RuntimeError):
    """Settings are missing or invalid for the selected providers; raised at startup."""


class ProviderNotAvailableError(RuntimeError):
    """The provider selected in `.env` has no registered implementation yet."""

    def __init__(self, kind: str, name: str, available: list[str]) -> None:
        super().__init__(
            f"{kind} provider '{name}' is not available. "
            f"Registered providers: {available or 'none yet'}."
        )


_HTTP_STATUS_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
}


def error_response(
    code: ErrorCode, message: str, status_code: int, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "correlation_id": get_correlation_id()}}
    return JSONResponse(body, status_code=status_code, headers=headers)


async def _app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(AppError, exc)
    logger.warning("%s (%s): %s", exc.code, exc.status_code, exc.log_detail or exc.message)
    return error_response(exc.code, exc.message, exc.status_code, exc.headers)


async def _http_error_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, exc)
    code = _HTTP_STATUS_CODES.get(exc.status_code, ErrorCode.BAD_REQUEST)
    return error_response(code, str(exc.detail), exc.status_code, exc.headers)


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return error_response(ErrorCode.VALIDATION_ERROR, "Request validation failed.", 422)


async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error", exc_info=exc)
    return error_response(ErrorCode.INTERNAL_ERROR, "An unexpected error occurred.", 500)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
