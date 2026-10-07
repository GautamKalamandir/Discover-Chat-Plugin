"""Scenarios #8 and #15, the invariant checker, logging hygiene (Q18) and production guards."""

import logging
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.events import TableEvent, TokenEvent
from app.agent.orchestrator import Agent
from app.auth.entra import EntraAuthProvider
from app.auth.jwks import StaticKeySource
from app.auth.models import RequestContext
from app.auth.token_broker import TokenBroker
from app.authz.messages import GENERIC_DENIAL
from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.core.logging import QUIET_LOGGERS, RedactingFormatter, configure_logging, redact
from app.main import create_app
from app.powerbi.base import GatewayCapability, PowerBIGateway, QueryResult
from app.powerbi.errors import DaxQueryError
from app.powerbi.service import PowerBIService
from tests.conftest import KID
from tests.security.hostile import HostileLLM, RecordingGateway, check_invariants, plan
from tests.test_agent_pipeline import (
    GOLD_STEP,
    SETTINGS,
    Env,
    ScriptedLLM,
    WrappedGateway,
    build_env,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def env(committed_sessionmaker: async_sessionmaker[AsyncSession]) -> Env:
    return await build_env(committed_sessionmaker)


def text_of(events: list[Any]) -> str:
    return "".join(e.text for e in events if isinstance(e, TokenEvent))


# --- scenario 8: indirect reference to a restricted domain ---------------------------------------


@pytest.mark.scenario(8)
async def test_indirect_reference_to_a_restricted_domain_is_refused(env: Env) -> None:
    llm = HostileLLM("forbidden_model")
    gateway = RecordingGateway()

    events = await env.run(env.agent(llm, gateway), "user-a", "Which department performs best?")

    assert text_of(events) == GENERIC_DENIAL
    assert gateway.calls == []
    assert "Budget Variance" not in llm.inputs[0]  # never offered to the model


# --- scenario 15: RLS — each user's own token, no shared results ---------------------------------


class PerUserBroker(TokenBroker):
    async def get_token(self, ctx: RequestContext, scope: str | None = None) -> str:
        return f"pbi-token-{ctx.user.object_id}"


class RlsGateway(PowerBIGateway):
    """Power BI applying row-level security: rows depend on whose token is used."""

    name = "rls"
    capabilities = frozenset({GatewayCapability.EXECUTE})

    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def execute_dax(
        self, user_token: str, dataset_id: str, dax: str, *, max_rows: int, user_key: str
    ) -> QueryResult:
        self.tokens.append(user_token)
        value = 1200.5 if user_token.endswith("user-a") else 75.25
        return QueryResult(["[Total Net Sales]"], [{"[Total Net Sales]": value}], False, self.name)


@pytest.mark.scenario(15)
async def test_same_question_two_users_each_gets_their_own_rls_result(env: Env) -> None:
    gateway = RlsGateway()
    tables: dict[str, Any] = {}
    for user in ("user-a", "user-b"):
        powerbi = PowerBIService(gateway, None, PerUserBroker(), env.authz_service, SETTINGS)
        llm = ScriptedLLM([plan(GOLD_STEP), "Done."])
        agent = Agent(
            llm, env.retriever, env.user_schemas, powerbi, env.authz_service, env.sm, SETTINGS
        )
        events = await env.run(agent, user, "What are GOLD sales?")
        tables[user] = next(e for e in events if isinstance(e, TableEvent)).rows

    assert gateway.tokens == ["pbi-token-user-a", "pbi-token-user-b"]  # no shared cache
    assert tables["user-a"] != tables["user-b"]


# --- the invariant checker must actually catch violations ----------------------------------------


def test_invariant_checker_flags_real_leaks() -> None:
    leaked = [TokenEvent("Budget Variance is 5")]

    verdict = check_invariants(
        "What are sales?",
        leaked,
        ["context with Budget Variance"],
        [("finance-ds", "EVALUATE INFO.TABLES()")],
        {"sales-ds"},
        ["Budget Variance"],
    )

    assert len(verdict.violations) == 4  # wrong model, INFO query, shown to user, sent to LLM


def test_invariant_checker_accepts_echo_of_the_users_own_words() -> None:
    verdict = check_invariants(
        "compare with Budget Variance",
        [TokenEvent("Budget Variance isn't available")],
        ["Budget Variance"],
        [],
        {"sales-ds"},
        ["Budget Variance"],
    )

    assert verdict.violations == []


# --- logging hygiene (Q18) -----------------------------------------------------------------------


def test_redaction_masks_tokens_and_secrets() -> None:
    jwt_like = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
    text = redact(
        f"auth Bearer abcdefghijklmnop token={jwt_like} client_secret=s3cr3tValue "
        "key gsk_ABCDEFGHIJKLMNOPQRSTUV"
    )

    for secret in (jwt_like, "abcdefghijklmnop", "s3cr3tValue", "gsk_ABCDEFGHIJKLMNOPQRSTUV"):
        assert secret not in text
    assert "[REDACTED]" in text


def test_formatter_redacts_exceptions_too() -> None:
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, None)
    try:
        raise RuntimeError("Bearer abcdefghijklmnopqrstuvwxyz rejected")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()

    assert "abcdefghijklmnopqrstuvwxyz" not in RedactingFormatter("%(message)s").format(record)


def test_chatty_libraries_are_kept_quiet_even_at_debug() -> None:
    configure_logging("DEBUG")

    for name in QUIET_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
    configure_logging("INFO")


CANARIES = [
    "CANARY-QUESTION-71",
    "CANARY-TERM-72",
    "CANARY-DAXERR-73",
    "CANARY-ROW-74",
    "CANARY-LLM-75",
    "987654321",
]


async def test_no_user_content_reaches_the_logs(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    q = "CANARY-QUESTION-71 GOLD sales?"
    # 1. unresolved terms (the user's words)
    await env.run(
        env.agent(ScriptedLLM([plan(status="cannot_answer", unresolved_terms=["CANARY-TERM-72"])])),
        "user-a",
        q,
    )
    # 2. Power BI DAX error text echoing a value, repaired then exhausted
    failing = WrappedGateway(failures=[DaxQueryError("Value 'CANARY-DAXERR-73' not found")] * 3)
    fix = '{"dax": "EVALUATE ROW(\\"x\\", [Total Net Sales])"}'
    await env.run(env.agent(ScriptedLLM([plan(GOLD_STEP), fix, fix]), failing), "user-a", q)
    # 3. data rows + an ungrounded number in the answer
    rows = WrappedGateway(rows=[{"Product[LOB]": "CANARY-ROW-74", "[Total Net Sales]": 1.5}])
    await env.run(
        env.agent(ScriptedLLM([plan(GOLD_STEP), "Sales were 987654321."]), rows), "user-a", q
    )
    # 4. LLM returns invalid JSON echoing content
    await env.run(env.agent(ScriptedLLM(['{"bad": "CANARY-LLM-75"}'] * 2)), "user-a", q)

    for canary in CANARIES:
        assert canary not in caplog.text, f"{canary} leaked into logs"


async def test_tokens_never_reach_the_logs(
    caplog: pytest.LogCaptureFixture, entra_settings: Settings, signing_key: Any, make_token: Any
) -> None:
    caplog.set_level(logging.DEBUG)
    provider = EntraAuthProvider(
        entra_settings, key_source=StaticKeySource({KID: signing_key.public_key()})
    )
    app = create_app(entra_settings, auth_provider=provider, llm=ScriptedLLM([]))
    good, expired = make_token(), make_token(exp=1, nbf=0, iat=0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        for token in (good, expired, good + "tampered"):
            await client.get("/api/v1/session", headers={"Authorization": f"Bearer {token}"})

    for token in (good, expired):
        assert token not in caplog.text
        assert token.split(".")[2] not in caplog.text  # not even the signature part


# --- production guards ---------------------------------------------------------------------------


def prod(entra_settings: Settings, **overrides: Any) -> Settings:
    values = {
        **entra_settings.model_dump(),
        "environment": "prod",
        "log_level": "INFO",
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"powerbi_gateway": "dev_synthetic"}, "dev_synthetic"),
        ({"powerbi_fallback_gateway": "dev_synthetic"}, "dev_synthetic"),
        ({"cors_allowed_origins": ["*"]}, "CORS"),
        ({"dev_model_access": {"u": ["m"]}}, "DEV_"),
        ({"log_level": "DEBUG"}, "LOG_LEVEL"),
    ],
)
def test_unsafe_production_configuration_refuses_to_start(
    entra_settings: Settings, overrides: dict[str, Any], problem: str
) -> None:
    with pytest.raises(ConfigurationError, match=problem):
        create_app(prod(entra_settings, **overrides), llm=ScriptedLLM([]))


async def test_production_hides_api_docs_and_sets_csp(
    entra_settings: Settings, signing_key: Any
) -> None:
    provider = EntraAuthProvider(
        entra_settings, key_source=StaticKeySource({KID: signing_key.public_key()})
    )
    app = create_app(prod(entra_settings), auth_provider=provider, llm=ScriptedLLM([]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        docs = await client.get("/docs")
        schema = await client.get("/openapi.json")
        health = await client.get("/api/v1/health")

    assert (docs.status_code, schema.status_code) == (404, 404)
    assert health.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"


_ = uuid  # keep stdlib import grouping stable for readers
