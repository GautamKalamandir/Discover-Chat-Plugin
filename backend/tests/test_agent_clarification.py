"""Agent-led clarification with options (ADR 0014), whole turns against the test database.

The live case: "sales of kismaba" with stores stored as "STR - KOSAMBA". Here the same shape uses
Product[Category], stored with a prefix ("CAT - BANGLE"), and the misspelt word "bangel".
"""

import json
import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest

from app.agent.events import AgentEvent, ClarificationEvent, DoneEvent, TableEvent
from app.agent.models import ClarificationChoice
from app.agent.orchestrator import STALE_CHOICE, Agent
from app.agent.resolution import similarity
from app.auth.token_broker import UnavailableTokenBroker
from app.authz.messages import GENERIC_DENIAL
from app.core.config import Settings
from app.db.models import ChatSession
from app.llm.base import ChatMessage
from app.powerbi.base import GatewayCapability, GatewayPayload, QueryResult
from app.powerbi.service import PowerBIService
from tests.authz_helpers import request_ctx
from tests.test_agent_conversation_and_change import change
from tests.test_agent_pipeline import (
    GOLD_STEP,
    SETTINGS,
    Env,
    ScriptedLLM,
    WrappedGateway,
    build_env,
    plan,
    text_of,
)

pytestmark = pytest.mark.integration

STORED = ["CAT - BANGLE", "CAT - BANGLE SET", "CAT - CHAIN"]


def step(value: str) -> dict[str, Any]:
    return {
        "model_id": "sales-ds",
        "label": "Category net sales",
        "measures": ["Sales[Total Net Sales]"],
        "filters": [{"column": "Product[Category]", "op": "=", "values": [value]}],
    }


def option(label: str, value: str) -> dict[str, Any]:
    return {"label": label, "steps": [step(value)]}


class CategoryGateway(WrappedGateway):
    """dev_synthetic whose ValueSearch knows the stored categories (Fabric IQ-like scores);
    other scoped columns find the word exactly, like the dev gateway."""

    def __init__(self, *, value_search: bool = True) -> None:
        super().__init__()
        self.searches: list[list[str]] = []
        if not value_search:
            self.capabilities = frozenset({GatewayCapability.EXECUTE})

    async def search_values(
        self,
        user_token: str,
        dataset_id: str,
        terms: list[str],
        *,
        scope: list[dict[str, Any]] | None = None,
    ) -> Any:
        self.searches.append(terms)
        other = [
            (s["tableName"], c)
            for s in scope or []
            for c in s.get("columnNames") or []
            if (s["tableName"], c) != ("Product", "Category")
        ]
        results: dict[str, list[dict[str, Any]]] = {}
        for term in terms:
            results[term] = [
                {"Table": "Product", "Column": "Category", "Value": v, "Score": similarity(term, v)}
                for v in STORED
                if similarity(term, v) >= 0.6
            ] + [{"Table": t, "Column": c, "Value": term, "Score": 1.0} for t, c in other]
        return GatewayPayload({"Results": results}, self.name)

    async def execute_dax(
        self, user_token: str, dataset_id: str, dax: str, *, max_rows: int, user_key: str
    ) -> QueryResult:
        if dax.startswith("EVALUATE TOPN(") and "VALUES('Product'[Category])" in dax:
            self.queries.append(dax)
            rows = [{"Product[Category]": v} for v in STORED]
            return QueryResult(["Product[Category]"], rows, False, self.name)
        return await super().execute_dax(
            user_token, dataset_id, dax, max_rows=max_rows, user_key=user_key
        )


@pytest.fixture
async def env(committed_sessionmaker: Any) -> Env:
    return await build_env(committed_sessionmaker)


def agent_with(env: Env, llm: ScriptedLLM, gateway: WrappedGateway, **overrides: Any) -> Agent:
    settings = Settings(_env_file=None, environment="test", powerbi_max_retries=0, **overrides)
    powerbi = PowerBIService(gateway, None, UnavailableTokenBroker(), env.authz_service, settings)
    return Agent(
        llm,
        env.retriever,
        env.user_schemas,
        powerbi,
        env.authz_service,
        env.sm,
        settings,
        today=lambda: date(2026, 10, 6),
    )


async def click(
    env: Env, agent: Agent, chat: ChatSession, label: str, asked: ClarificationEvent, index: int
) -> list[AgentEvent]:
    authz = await env.authz_service.build_context(request_ctx("user-a"))
    choice = ClarificationChoice(
        clarification_id=asked.id or uuid.uuid4(), option_id=uuid.UUID(asked.options[index]["id"])
    )
    return [
        e async for e in agent.run(authz, chat, label, primary_model_id="sales-ds", choice=choice)
    ]


def clarification(events: list[AgentEvent]) -> ClarificationEvent:
    return next(e for e in events if isinstance(e, ClarificationEvent))


def answer_echoing_question(messages: Sequence[ChatMessage]) -> str:
    question = messages[-1].content.split("<untrusted_data>\n", 1)[1].split("\n", 1)[0]
    return f"Answer to: {question}"


# --- the agent asks; a click runs the validated option ------------------------------------------


async def test_misspelt_value_agent_asks_with_options_and_a_click_runs_without_planning(
    env: Env,
) -> None:
    gateway = CategoryGateway()
    asks = plan(
        status="clarify",
        clarification="Which category did you mean?",
        options=[
            option("Bangle", "CAT - BANGLE"),
            option("Bangle set", "CAT - BANGLE SET"),
            option("Invented", "CAT - INVENTED"),  # not stored: never offered
        ],
    )
    first = ScriptedLLM([plan(step("bangel")), asks])
    chat = await env.chat("user-a")

    events = await env.run(env.agent(first, gateway), "user-a", "net sales of bangel", chat)

    asked = clarification(events)
    assert asked.question == "Which category did you mean?" and asked.id is not None
    assert [o["label"] for o in asked.options] == ["Bangle", "Bangle set"]
    evidence = first.calls[1][-1].content
    assert '"bangel" (used for Product[Category]): not stored as written.' in evidence
    assert "'Product'[Category] = \"CAT - BANGLE\"" in evidence
    assert await env.count("query_executions") == 0

    second = ScriptedLLM([answer_echoing_question])  # only the answer: no planning call
    events = await click(env, env.agent(second, gateway), chat, "Bangle", asked, 0)

    assert len(second.calls) == 1
    assert any("TREATAS({\"CAT - BANGLE\"}, 'Product'[Category])" in q for q in gateway.queries)
    assert text_of(events).startswith("Answer to: net sales of bangel (Bangle)")
    assert isinstance(events[-1], DoneEvent) and events[-1].message_id is not None

    # The clarification is answered: clicking it again is refused.
    again = await click(env, env.agent(ScriptedLLM([]), gateway), chat, "Bangle", asked, 1)
    assert text_of(again) == STALE_CHOICE


async def test_agent_proceeds_on_its_own_and_its_assumption_reaches_the_answer(env: Env) -> None:
    gateway = CategoryGateway()
    chosen = plan(step("CAT - BANGLE"), assumptions=['"bangel" read as CAT - BANGLE'])
    llm = ScriptedLLM([plan(step("bangel")), chosen, "Bangle sales are in the table."])

    events = await env.run(env.agent(llm, gateway), "user-a", "net sales of bangel")

    assert not any(isinstance(e, ClarificationEvent) for e in events)
    assert any("TREATAS({\"CAT - BANGLE\"}, 'Product'[Category])" in q for q in gateway.queries)
    facts = llm.calls[2][-1].content.split("FACTS", 1)[1]
    assert "assumptions" in facts and "read as CAT - BANGLE" in facts


async def test_value_stored_with_other_case_runs_with_no_extra_llm_call(env: Env) -> None:
    gateway = CategoryGateway()
    llm = ScriptedLLM([plan(step("cat - chain")), "Chain sales are in the table."])

    await env.run(env.agent(llm, gateway), "user-a", "net sales of cat - chain")

    assert len(llm.calls) == 2  # plan + answer
    assert any("TREATAS({\"CAT - CHAIN\"}, 'Product'[Category])" in q for q in gateway.queries)


async def test_without_value_search_the_agent_gets_evidence_from_the_columns_values(
    env: Env,
) -> None:
    gateway = CategoryGateway(value_search=False)
    llm = ScriptedLLM([plan(step("bangel")), plan(step("CAT - BANGLE")), "Done."])

    await env.run(env.agent(llm, gateway), "user-a", "net sales of bangel")

    assert gateway.searches == []
    assert "VALUES('Product'[Category])" in gateway.queries[0]
    assert "'Product'[Category] = \"CAT - BANGLE\"" in llm.calls[1][-1].content
    assert any("TREATAS({\"CAT - BANGLE\"}, 'Product'[Category])" in q for q in gateway.queries)


# --- safety -------------------------------------------------------------------------------------


async def test_forged_choice_is_refused_and_runs_nothing(env: Env) -> None:
    gateway = CategoryGateway()
    chat = await env.chat("user-a")
    forged = ClarificationEvent("?", id=uuid.uuid4(), options=[{"id": str(uuid.uuid4())}])

    events = await click(env, env.agent(ScriptedLLM([]), gateway), chat, "x", forged, 0)

    assert text_of(events) == STALE_CHOICE
    assert gateway.queries == [] and await env.count("query_executions") == 0


async def test_choice_from_another_conversation_is_refused(env: Env) -> None:
    gateway = CategoryGateway()
    asks = plan(
        status="clarify", clarification="Which?", options=[option("Bangle", "CAT - BANGLE")]
    )
    chat = await env.chat("user-a")
    events = await env.run(env.agent(ScriptedLLM([asks]), gateway), "user-a", "bangle?", chat)
    other_chat = await env.chat("user-a")

    refused = await click(env, env.agent(ScriptedLLM([]), gateway), other_chat, "Bangle",
                          clarification(events), 0)  # fmt: skip

    assert text_of(refused) == STALE_CHOICE


@pytest.mark.scenario(7)
async def test_option_naming_a_forbidden_model_denies_the_whole_request(env: Env) -> None:
    finance = {"model_id": "finance-ds", "label": "Budget", "measures": ["GL[Budget Variance]"]}
    asks = plan(
        status="clarify",
        clarification="Which one?",
        options=[option("Bangle", "CAT - BANGLE"), {"label": "Budget", "steps": [finance]}],
    )

    events = await env.run(env.agent(ScriptedLLM([asks]), CategoryGateway()), "user-a", "bangle?")

    assert text_of(events) == GENERIC_DENIAL
    assert not any(isinstance(e, ClarificationEvent) for e in events)
    assert await env.count("audit_events", "WHERE outcome = 'deny'") == 1


async def test_options_that_cant_run_are_replaced_once_then_asked_as_plain_text(env: Env) -> None:
    unknown = {"column": "Customer[Secret]", "function": "sum"}
    hidden = {
        "label": "Credit",
        "steps": [{**step("x"), "filters": [], "measures": [], "aggregations": [unknown]}],
    }
    llm = ScriptedLLM(
        [
            plan(status="clarify", clarification="Which?", options=[hidden]),
            plan(status="clarify", clarification="Which category exactly?"),
        ]
    )

    events = await env.run(env.agent(llm, CategoryGateway()), "user-a", "show it")

    assert "option 1: unknown column Customer[Secret]" in llm.calls[1][-1].content
    asked = clarification(events)
    assert asked.question == "Which category exactly?" and asked.options == []


# --- conversation -------------------------------------------------------------------------------


async def test_typed_reply_continues_the_question_and_sees_the_options(env: Env) -> None:
    gateway = CategoryGateway()
    period = {**GOLD_STEP, "time": None}
    asks = plan(
        status="clarify",
        clarification="Which period does 'till now' cover?",
        options=[{"label": "This financial year", "steps": [GOLD_STEP]},
                 {"label": "All time", "steps": [period]}],
    )  # fmt: skip
    chat = await env.chat("user-a")
    await env.run(env.agent(ScriptedLLM([asks]), gateway), "user-a", "GOLD sales till now", chat)

    reply = ScriptedLLM(
        [plan(period, answers_previous_clarification=True), answer_echoing_question]
    )
    events = await env.run(env.agent(reply, gateway), "user-a", "all time please", chat)

    previous = reply.calls[0][-1].content.split("PREVIOUS TURN", 1)[1]
    assert "options_offered" in previous and "All time" in previous
    assert '"id"' not in previous  # stored option ids and plans stay on the server
    assert text_of(events).startswith("Answer to: GOLD sales till now (all time please)")


async def test_clarification_limit_makes_the_agent_proceed(env: Env) -> None:
    gateway = CategoryGateway()
    asks = plan(status="clarify", clarification="Which period?")
    chat = await env.chat("user-a")
    agent = agent_with(env, ScriptedLLM([asks]), gateway, agent_max_clarifications=1)
    await env.run(agent, "user-a", "GOLD sales till now", chat)

    asks_again = plan(status="clarify", clarification="Calendar or financial year?",
                      answers_previous_clarification=True)  # fmt: skip
    llm = ScriptedLLM([asks_again, plan(GOLD_STEP), "GOLD sales are in the table."])
    events = await env.run(agent_with(env, llm, gateway, agent_max_clarifications=1),
                           "user-a", "the year", chat)  # fmt: skip

    assert "may not ask the user another question" in llm.calls[1][-1].content
    assert not any(isinstance(e, ClarificationEvent) for e in events)
    assert any(isinstance(e, TableEvent) for e in events)


async def test_running_period_is_asked_with_two_options_and_a_click_explains(env: Env) -> None:
    gateway = CategoryGateway()
    pending = change(drivers=["Product[Category]"]).model_dump(mode="json")
    chat = await env.chat("user-a")

    events = await env.run(env.agent(ScriptedLLM([plan(change=pending)]), gateway), "user-a",
                           "why is gold selling low in 2026-27 vs 2025-26?", chat)  # fmt: skip

    asked = clarification(events)
    assert [o["label"] for o in asked.options] == [
        "Same dates: 01 Apr 2025 to 06 Oct 2025 vs 01 Apr 2026 to 06 Oct 2026",
        "01 Apr 2025 to 31 Mar 2026 vs 01 Apr 2026 to 06 Oct 2026 so far",
    ]
    answer = ScriptedLLM(["Not grounded: 12345678.9"])  # -> template answer
    events = await click(env, env.agent(answer, gateway), chat, "Same dates", asked, 0)

    tables = [e for e in events if isinstance(e, TableEvent)]
    assert len(tables) == 2 and tables[0].columns[-2:] == ["[Change]", "[Change %]"]
    assert len(answer.calls) == 1 and "went from" in text_of(events)


def test_option_labels_and_events_serialise_for_the_stream() -> None:
    event = ClarificationEvent("Which?", id=uuid.uuid4(), options=[{"id": "1", "label": "A"}])

    data = json.loads(json.dumps(event.__dict__, default=str))

    assert data["options"] == [{"id": "1", "label": "A"}] and data["allow_free_text"] is True


def test_settings_for_clarification_fall_back_to_defaults() -> None:
    settings = Settings(_env_file=None, agent_max_clarifications="x", agent_max_options="0")

    assert (settings.agent_max_clarifications, settings.agent_max_options) == (3, 5)
    assert SETTINGS.value_fallback_max_values == 1000
