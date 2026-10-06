"""User-facing authorization texts (Q16: generic). Never include model names, ids or reasons."""

from app.core.errors import AppError, ErrorCode

GENERIC_DENIAL = (
    "I can't answer that because it needs data you don't have access to. "
    "I can help with questions about the data available to you."
)
MODEL_NOT_AVAILABLE = "This data source isn't available to you."
ACCESS_CHECK_UNAVAILABLE = (
    "I couldn't verify your data access with Power BI right now. Please try again in a moment."
)


def access_denied(log_detail: str) -> AppError:
    return AppError(ErrorCode.MODEL_ACCESS_DENIED, GENERIC_DENIAL, 403, log_detail=log_detail)


def model_not_found(log_detail: str) -> AppError:
    return AppError(ErrorCode.MODEL_NOT_FOUND, MODEL_NOT_AVAILABLE, 404, log_detail=log_detail)


def access_check_unavailable(log_detail: str) -> AppError:
    return AppError(
        ErrorCode.ACCESS_CHECK_UNAVAILABLE, ACCESS_CHECK_UNAVAILABLE, 503, log_detail=log_detail
    )
