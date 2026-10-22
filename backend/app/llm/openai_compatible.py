"""Chat completions over the OpenAI API format — used for both Groq (OpenAI-compatible endpoint)
and OpenAI, so switching provider is a `.env` change only (ADR 0002)."""

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import openai
from openai import AsyncOpenAI

from app.core.errors import AppError, ErrorCode
from app.llm.base import ChatMessage, LLMProvider, LLMResponse
from app.llm.budget import current_limits
from app.llm.pacer import TokenPacer, estimate_tokens

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
        tokens_per_minute: int = 0,
        client: Any = None,
    ) -> None:
        self.name = name
        self.model = model
        self._temperature = temperature
        self._max_tokens = max_output_tokens
        # Only reasoning models take `reasoning_effort` (others reject the parameter).
        self._reasoning = "gpt-oss" in model or model.startswith(("o1", "o3", "o4", "gpt-5"))
        self._pacer = TokenPacer(tokens_per_minute) if tokens_per_minute > 0 else None
        self._client: Any = client or AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_seconds, max_retries=max_retries
        )

    async def complete(
        self, messages: Sequence[ChatMessage], *, json_mode: bool = False
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        max_tokens = self._max_tokens
        limits = current_limits()
        if limits is not None:  # set by callers that know how long the answer should be
            max_tokens = min(max_tokens, limits.max_output_tokens)
            if limits.reasoning_effort and self._reasoning:
                kwargs["reasoning_effort"] = limits.reasoning_effort
        reservation = None
        if self._pacer is not None:
            estimate = estimate_tokens([m.content for m in messages]) + max_tokens
            reservation = await self._pacer.reserve(estimate)
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=_to_wire(messages),
                temperature=self._temperature,
                max_completion_tokens=max_tokens,
                **kwargs,
            )
        except openai.APIError as exc:
            raise _unavailable(self.name, exc) from exc
        usage = getattr(response, "usage", None)
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) or 0
        if reservation is not None and self._pacer is not None and prompt is not None:
            # Cached input doesn't count towards the provider's limits.
            self._pacer.settle(reservation, prompt - cached + (completion or 0))
        logger.info(
            "LLM %s/%s tokens in=%s cached=%s out=%s",
            self.name,
            self.model,
            prompt,
            cached,
            completion,
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
    hint = " (request larger than the tokens-per-minute limit)" if status == 413 else ""
    return AppError(
        ErrorCode.LLM_UNAVAILABLE,
        "The assistant is temporarily unavailable. Please try again in a moment.",
        503,
        log_detail=f"{provider} API error {status}: {type(exc).__name__}{hint}",
    )
