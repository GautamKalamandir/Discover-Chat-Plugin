"""Red-team suite (ADR 0011): zero unauthorized model access and zero restricted-name leaks, even
with a fully compromised LLM (deterministic), and with the real model (`-m network`)."""

import os
from collections.abc import AsyncIterator, Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.events import ClarificationEvent, DoneEvent, ErrorEvent, TableEvent, TokenEvent
from app.core.config import Settings
from app.llm.base import ChatMessage, LLMProvider, LLMResponse
from app.llm.factory import create_llm_provider
from tests.security.hostile import (
    RESTRICTED_FOR_USER_A,
    RESTRICTED_FOR_USER_B,
    HostileLLM,
    RecordingGateway,
    check_invariants,
)
from tests.security.redteam_cases import CASES, Case
from tests.test_agent_pipeline import GRANTS, Env, build_env

pytestmark = pytest.mark.integration


@pytest.fixture
async def env(committed_sessionmaker: async_sessionmaker[AsyncSession]) -> Env:
    return await build_env(committed_sessionmaker)


def restricted_for(user: str) -> Sequence[str]:
    return RESTRICTED_FOR_USER_B if user == "user-b" else RESTRICTED_FOR_USER_A


async def run_case(
    env: Env, case: Case, llm: LLMProvider, inputs: list[str], outputs: Sequence[str] = ()
) -> list[str]:
    gateway = RecordingGateway()
    events = await env.run(env.agent(llm, gateway), case.user, case.prompt)
    assert isinstance(events[-1], DoneEvent), f"{case.id}: turn did not finish"
    verdict = check_invariants(
        case.prompt,
        events,
        inputs,
        gateway.calls,
        GRANTS[case.user],
        restricted_for(case.user),
        outputs,
    )
    return [f"{case.id} ({case.category}): {v}" for v in verdict.violations]


@pytest.mark.scenario(7)
@pytest.mark.scenario(8)
@pytest.mark.scenario(12)
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_compromised_llm_cannot_breach_authorization(env: Env, case: Case) -> None:
    llm = HostileLLM(case.strategy)

    violations = await run_case(env, case, llm, llm.inputs, llm.outputs)

    assert violations == []


async def test_benign_control_still_answers(env: Env) -> None:
    gateway = RecordingGateway()
    events = await env.run(env.agent(HostileLLM("comply_and_dump"), gateway), "user-a", "GOLD?")

    assert any(isinstance(e, TableEvent) for e in events)
    assert any(isinstance(e, TokenEvent) for e in events)
    assert gateway.calls and gateway.calls[0][0] == "sales-ds"


class RecordingLLM(LLMProvider):
    """Real provider, with every prompt recorded for the leak invariant."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner, self.name, self.model = inner, inner.name, inner.model
        self.inputs: list[str] = []

    async def complete(
        self, messages: Sequence[ChatMessage], *, json_mode: bool = False
    ) -> LLMResponse:
        self.inputs.append("\n".join(m.content for m in messages))
        return await self.inner.complete(messages, json_mode=json_mode)

    def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:  # pragma: no cover
        raise NotImplementedError


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("GROQ_API_KEY"), reason="GROQ_API_KEY not set")
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_real_model_redteam(env: Env, case: Case) -> None:
    """Run with: GROQ_API_KEY=... uv run pytest -m network -k redteam"""
    inner = create_llm_provider(Settings(_env_file=None, groq_api_key=os.environ["GROQ_API_KEY"]))
    llm = RecordingLLM(inner)
    try:
        violations = await run_case(env, case, llm, llm.inputs)
    finally:
        await inner.aclose()

    assert violations == []


_ = (ClarificationEvent, ErrorEvent)  # event types appear in the invariant output
