"""Per-call limits for LLM requests (ADR 0016 amendment 1: free-tier mode).

A caller that knows how long an answer should be sets it for the calls it makes:

    with call_limits(max_output_tokens=900, reasoning_effort="low"):
        await llm.complete_json(messages, Schema)

Providers read the limits of the current task. Groq counts a request's input plus its answer room
against the per-minute limit, so a smaller room lets a request fit; reasoning models (gpt-oss)
count their reasoning as output, so a lower effort costs fewer tokens. Calls without limits keep
LLM_MAX_OUTPUT_TOKENS and the model's default effort (chat is unchanged).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

ReasoningEffort = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class CallLimits:
    max_output_tokens: int
    reasoning_effort: ReasoningEffort | None = None


_limits: ContextVar[CallLimits | None] = ContextVar("llm_call_limits", default=None)


@contextmanager
def call_limits(
    max_output_tokens: int, reasoning_effort: ReasoningEffort | None = None
) -> Iterator[None]:
    token = _limits.set(CallLimits(max_output_tokens, reasoning_effort))
    try:
        yield
    finally:
        _limits.reset(token)


def current_limits() -> CallLimits | None:
    return _limits.get()
