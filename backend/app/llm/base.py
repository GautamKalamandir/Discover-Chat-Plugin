"""LLM provider contract (ADR 0002 / 0008).

The agent is a fixed pipeline (Q13): the LLM never calls tools. It returns validated structured
output (`complete_json`) or plain text (`complete`). Providers are selected only via `.env`.
"""

import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.errors import AppError, ErrorCode

logger = logging.getLogger(__name__)

Role = Literal["system", "user", "assistant"]
T = TypeVar("T", bound=BaseModel)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class LLMResponse:
    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def complete(
        self, messages: Sequence[ChatMessage], *, json_mode: bool = False
    ) -> LLMResponse: ...

    @abstractmethod
    def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]: ...

    async def complete_json(self, messages: Sequence[ChatMessage], schema: type[T]) -> T:
        """JSON output validated against `schema`; one corrective retry, then a typed error."""
        conversation = list(messages)
        last_error = ""
        for attempt in range(2):
            response = await self.complete(conversation, json_mode=True)
            try:
                return schema.model_validate_json(extract_json(response.content))
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)[:1500]
                logger.warning("LLM returned invalid %s (attempt %d)", schema.__name__, attempt + 1)
                conversation += [
                    ChatMessage("assistant", response.content[:4000]),
                    ChatMessage(
                        "user",
                        "That JSON was invalid for the required schema:\n"
                        f"{last_error}\nReturn only the corrected JSON object.",
                    ),
                ]
        raise AppError(
            ErrorCode.LLM_OUTPUT_INVALID,
            "I couldn't work out how to answer that. Please try rephrasing your question.",
            502,
            log_detail=f"{schema.__name__} validation failed twice: {last_error[:300]}",
        )

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""


def extract_json(text: str) -> str:
    """The JSON object in a response, tolerating code fences or stray prose around it."""
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped
    match = _JSON_BLOCK.search(stripped)
    if not match:
        raise ValueError("no JSON object in response")
    candidate = match.group(0)
    json.loads(candidate)  # raises ValueError if not JSON
    return candidate


class UnconfiguredLLM(LLMProvider):
    """Stands in when no API key is configured outside production; every call explains why."""

    name = "unconfigured"
    model = "-"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def _error(self) -> AppError:
        return AppError(
            ErrorCode.LLM_UNAVAILABLE,
            "The assistant isn't configured yet. Please contact the administrator.",
            503,
            log_detail=self._reason,
        )

    async def complete(
        self, messages: Sequence[ChatMessage], *, json_mode: bool = False
    ) -> LLMResponse:
        raise self._error()

    async def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:
        raise self._error()
        yield ""  # pragma: no cover - makes this an async generator
