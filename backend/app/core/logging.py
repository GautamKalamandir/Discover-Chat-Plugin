"""Logging setup (ADR 0011, Q18: logs carry ids, codes, counts and timings, never user content).

RedactingFormatter is defence in depth: no log statement may emit a token or secret in the first
place, but if one ever does (an exception message, a third-party library), it is masked on output.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime

from app.core.request_context import get_correlation_id

REDACTED = "[REDACTED]"
_SECRETS = [
    re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),  # JWTs
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)((?:client_secret|api_key|apikey|password|secret|token|authorization)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{4,}"
    ),
    re.compile(r"\b(?:gsk_|sk-)[A-Za-z0-9_-]{16,}"),  # Groq (gsk_…) / OpenAI (sk-…) API keys
]

# Third-party libraries that log request/response bodies at DEBUG (e.g. the OpenAI SDK logs full
# prompts). They stay at WARNING whatever LOG_LEVEL is, so user content never reaches the logs.
QUIET_LOGGERS = ("openai", "httpx", "httpcore", "httpx2", "mcp", "sse_starlette", "msal", "asyncio")


def redact(text: str) -> str:
    for pattern in _SECRETS:
        text = pattern.sub(
            lambda m: (m.group(1) if m.re.groups and m.group(1) else "") + REDACTED, text
        )
    return text


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload))


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CorrelationIdFilter())
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            RedactingFormatter(
                "%(asctime)s %(levelname)-8s [%(correlation_id)s] %(name)s: %(message)s"
            )
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
