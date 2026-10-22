from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from pydantic import BaseModel

from app.core.errors import AppError
from app.llm.base import ChatMessage, extract_json
from app.llm.budget import call_limits
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.llm.pacer import TokenPacer, estimate_tokens


class Answer(BaseModel):
    value: int


class FakeCompletions:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = replies
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if kwargs.get("stream"):
            return _stream(reply)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )


async def _stream(parts: list[str]) -> AsyncIterator[Any]:
    for part in parts:
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part))])


def provider(replies: list[Any]) -> tuple[OpenAICompatibleProvider, FakeCompletions]:
    completions = FakeCompletions(replies)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    llm = OpenAICompatibleProvider(
        name="groq",
        model="openai/gpt-oss-120b",
        api_key="k",
        base_url="https://x",
        temperature=0.0,
        max_output_tokens=100,
        timeout_seconds=5,
        max_retries=0,
        client=client,
    )
    return llm, completions


MSG = [ChatMessage("user", "Return JSON")]


async def test_complete_sends_model_and_json_mode() -> None:
    llm, completions = provider(['{"value": 1}'])

    assert (await llm.complete_json(MSG, Answer)).value == 1
    call = completions.calls[0]
    assert call["model"] == "openai/gpt-oss-120b"
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"] == [{"role": "user", "content": "Return JSON"}]


async def test_invalid_json_gets_one_corrective_retry() -> None:
    llm, completions = provider(['{"value": "many"}', '{"value": 2}'])

    assert (await llm.complete_json(MSG, Answer)).value == 2
    retry_messages = completions.calls[1]["messages"]
    assert retry_messages[-1]["role"] == "user" and "invalid" in retry_messages[-1]["content"]


async def test_two_invalid_answers_raise_a_user_safe_error() -> None:
    llm, _ = provider(["not json", "still not json"])

    with pytest.raises(AppError) as excinfo:
        await llm.complete_json(MSG, Answer)

    assert excinfo.value.code == "llm_output_invalid"


async def test_api_errors_become_llm_unavailable() -> None:
    error = openai.APIConnectionError(request=httpx2.Request("POST", "https://x"))
    llm, _ = provider([error])

    with pytest.raises(AppError) as excinfo:
        await llm.complete(MSG)

    assert (excinfo.value.code, excinfo.value.status_code) == ("llm_unavailable", 503)


async def test_stream_yields_text_deltas() -> None:
    llm, _ = provider([["Hel", "lo"]])

    assert [part async for part in llm.stream(MSG)] == ["Hel", "lo"]


@pytest.mark.parametrize(
    "text", ['{"value": 3}', '```json\n{"value": 3}\n```', 'Sure! {"value": 3} Done.']
)
def test_extract_json_tolerates_wrapping(text: str) -> None:
    assert extract_json(text) == '{"value": 3}'


# --- free-tier mode: per-call limits and pacing (ADR 0016 amendment 1) --------------------------


async def test_callers_can_shorten_the_answer_and_lower_the_reasoning_effort() -> None:
    llm, completions = provider(['{"value": 1}', '{"value": 2}'])

    with call_limits(40, "low"):
        await llm.complete_json(MSG, Answer)
    await llm.complete_json(MSG, Answer)

    limited, normal = completions.calls
    assert (limited["max_completion_tokens"], limited["reasoning_effort"]) == (40, "low")
    assert normal["max_completion_tokens"] == 100 and "reasoning_effort" not in normal


async def test_a_limit_never_raises_the_configured_answer_room() -> None:
    llm, completions = provider(['{"value": 1}'])

    with call_limits(5000):
        await llm.complete_json(MSG, Answer)

    assert completions.calls[0]["max_completion_tokens"] == 100


async def test_models_without_reasoning_never_get_a_reasoning_effort() -> None:
    completions = FakeCompletions(['{"value": 1}'])
    llm = OpenAICompatibleProvider(
        name="groq", model="llama-3.3-70b-versatile", api_key="k", base_url=None,
        temperature=0.0, max_output_tokens=100, timeout_seconds=5, max_retries=0,
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )  # fmt: skip

    with call_limits(40, "low"):
        await llm.complete_json(MSG, Answer)

    assert "reasoning_effort" not in completions.calls[0]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


async def test_the_pacer_waits_for_room_in_the_last_minute() -> None:
    clock = Clock()
    pacer = TokenPacer(8000, clock=clock, sleep=clock.sleep)

    first = await pacer.reserve(5000)
    clock.now = 20.0
    await pacer.reserve(2500)  # 7,500 in the minute: fits
    clock.now = 30.0
    await pacer.reserve(3000)  # would be 10,500: waits until the first call is a minute old

    assert clock.waits == [30.0] and clock.now == 60.0
    pacer.settle(first, 1200)  # what the provider counted replaces the estimate
    assert first.tokens == 1200


async def test_a_paced_provider_books_what_the_answer_really_used() -> None:
    completions = FakeCompletions(['{"value": 1}'])
    llm = OpenAICompatibleProvider(
        name="groq", model="openai/gpt-oss-120b", api_key="k", base_url=None,
        temperature=0.0, max_output_tokens=100, timeout_seconds=5, max_retries=0,
        tokens_per_minute=8000,
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )  # fmt: skip

    await llm.complete_json(MSG, Answer)

    assert llm._pacer is not None
    assert [r.tokens for r in llm._pacer._window] == [15]  # 10 in + 5 out, not the estimate


def test_estimates_count_about_three_characters_a_token() -> None:
    assert estimate_tokens(["x" * 300, "y" * 30]) == 110 + 16
