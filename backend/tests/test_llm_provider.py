from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from pydantic import BaseModel

from app.core.errors import AppError
from app.llm.base import ChatMessage, extract_json
from app.llm.openai_compatible import OpenAICompatibleProvider


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
