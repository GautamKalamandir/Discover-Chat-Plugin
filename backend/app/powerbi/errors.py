"""Gateway-level failures. `PowerBIService` turns them into user-safe `AppError`s.

Classification drives behaviour:
- GatewayUnavailableError  -> try the fallback gateway (endpoint down, disabled, contract mismatch)
- PowerBIAccessDeniedError -> re-check access live; revoke or report missing Build permission
- PowerBIThrottledError / PowerBITimeoutError -> retry with backoff, then give up
- DaxQueryError            -> no fallback (same DAX fails everywhere); the agent repairs it
"""

from app.core.errors import AppError, ErrorCode


class PowerBIError(Exception):
    pass


class GatewayUnavailableError(PowerBIError):
    pass


class CapabilityNotSupportedError(PowerBIError):
    pass


class PowerBIAccessDeniedError(PowerBIError):
    pass


class PowerBIThrottledError(PowerBIError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PowerBITimeoutError(PowerBIError):
    pass


class DaxQueryError(AppError):
    """The query itself failed. `dax_error` is for the agent's repair loop, never shown to users."""

    def __init__(self, dax_error: str) -> None:
        super().__init__(
            ErrorCode.QUERY_FAILED,
            "I couldn't run the query for that question. Try rephrasing it.",
            422,
            # Power BI's text can echo filter values (user data): never logged (Q18).
            log_detail=f"DAX query failed (error text withheld, {len(dax_error)} chars)",
        )
        self.dax_error = dax_error
