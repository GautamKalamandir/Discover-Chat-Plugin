"""Chat completions over the OpenAI API format — used for both Groq (OpenAI-compatible endpoint)
and OpenAI, so switching provider is a `.env` change only (ADR 0002)."""

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import openai
from openai import AsyncOpenAI

from app.core.errors import AppError, ErrorCode
from app.llm.base import ChatMessage, LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key: str,
        base_url: str | None,
        temperature: float,
        max_output_tokens: int,
        timeout_seconds: float,
        max_retries: int,
        client: Any = None,
    ) -> None:
        self.name = name
        self.model = model
        self._temperature = temperature
        self._max_tokens = max_output_tokens
        self._client: Any = client or AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_seconds, max_retries=max_retries
        )

    async def complete(
        self, messages: Sequence[ChatMessage], *, json_mode: bool = False
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=_to_wire(messages),
                temperature=self._temperature,
                max_completion_tokens=self._max_tokens,
                **kwargs,
            )
        except openai.APIError as exc:
            raise _unavailable(self.name, exc) from exc
        usage = getattr(response, "usage", None)
        logger.info(
            "LLM %s/%s tokens in=%s out=%s",
            self.name,
            self.model,
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
        )
        return LLMResponse(
            content=response.choices[0].message.content or "",
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )

    async def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:
        try:
            chunks = await self._client.chat.completions.create(
                model=self.model,
                messages=_to_wire(messages),
                temperature=self._temperature,
                max_completion_tokens=self._max_tokens,
                stream=True,
            )
            async for chunk in chunks:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except openai.APIError as exc:
            raise _unavailable(self.name, exc) from exc

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


def _to_wire(messages: Sequence[ChatMessage]) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


def _unavailable(provider: str, exc: Exception) -> AppError:
    status = getattr(exc, "status_code", None)
    return AppError(
        ErrorCode.LLM_UNAVAILABLE,
        "The assistant is temporarily unavailable. Please try again in a moment.",
        503,
        log_detail=f"{provider} API error {status}: {type(exc).__name__}",
    )
