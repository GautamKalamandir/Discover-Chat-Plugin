"""Two round trips for large models: pick tables + map words to fields, then plan (ADR 0013 §7)."""

import json
import uuid
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.context_builder import focused
from app.agent.orchestrator import Agent
from app.agent.table_selector import (
    Selection,
    TableSelection,
    catalog,
    needs_selection,
    validate_selection,
)
from app.auth.token_broker import UnavailableTokenBroker
from app.authz.models import AuthorizedContext, ModelSummary
from app.powerbi.service import PowerBIService
from app.semantic.normalizer import Relationship, normalize
from tests.authz_helpers import request_ctx
from tests.semantic_helpers import fixture
from tests.test_agent_pipeline import (
    GOLD_STEP,
    SETTINGS,
    Env,
    ScriptedLLM,
    WrappedGateway,
    build_env,
    grounded_answer,
    plan,
    text_of,
)

SALES = normalize(fixture("sales-ds"))
REAL_SHAPE = {
    "schema": {
        "Tables": [
            {"Name": "NET SALES MASTER",
             "Measures": [{"Name": "Total Net Sales", "Type": "Double"}],
             "Columns": [{"Name": "Location Name", "Type": "Text"},
                         {"Name": "ProductKey", "Type": "Text"}]},
            {"Name": "Dim_Product", "Columns": [{"Name": "ProductKey", "Type": "Text"},
                                                {"Name": "LOB", "Type": "Text"}]},
        ],
        "ActiveRelationships": [
            {"PK": "'Dim_Product'[ProductKey]", "FK": "'NET SALES MASTER'[ProductKey]",
             "UnidirectionalFilter": "'Dim_Product' filters 'NET SALES MASTER'"}
        ],
        "InactiveRelationships": [],
    }
}  # fmt: skip


def authz_with(*models: tuple[str, str]) -> AuthorizedContext:
    return AuthorizedContext(
        request=request_ctx("user-a"),
        user_id=uuid.uuid4(),
        allowed={d: ModelSummary(d, n, "Sales", uuid.uuid4()) for d, n in models},
        resolved_at=None,  # type: ignore[arg-type]
    )


def test_relationships_are_read_from_the_real_schema_shape() -> None:
    schema = normalize(REAL_SHAPE)

    assert schema.relationships == (
        Relationship("Dim_Product", "ProductKey", "NET SALES MASTER", "ProductKey", True),
    )


def test_catalog_lists_every_visible_table_by_name_only() -> None:
    text = catalog(authz_with(("kmjl", "KMJL Sales New")), {"kmjl": normalize(REAL_SHAPE)})

    assert "TABLE NET SALES MASTER | measures: Total Net Sales | columns: Location Name" in text
    assert "TABLE Dim_Product | columns: ProductKey, LOB" in text
    assert "Dim_Product[ProductKey] filters NET SALES MASTER[ProductKey]" in text


def test_hidden_objects_never_reach_the_catalog() -> None:
    text = catalog(authz_with(("sales-ds", "Sales")), {"sales-ds": SALES})

    assert "SalesKey" not in text  # isHidden in the fixture


def test_selection_keeps_only_real_visible_names() -> None:
    raw = TableSelection.model_validate(
        {
            "tables": [
                {"model_id": "sales-ds", "table": "Sales"},
                {"model_id": "sales-ds", "table": "Budget"},  # doesn't exist
                {"model_id": "finance-ds", "table": "Budget"},  # not in the user's context
            ],
            "mappings": [
                {"term": "line of business", "field": "Product[LOB]"},
                {"term": "store", "field": "Store[Name]"},  # invented
            ],
        }
    )

    selection = validate_selection(raw, {"sales-ds": SALES})

    assert selection == Selection(
        {"sales-ds": {"Sales", "Product"}}, [("line of business", "Product[LOB]")]
    )


def test_selection_with_nothing_valid_is_dropped() -> None:
    raw = TableSelection.model_validate({"tables": [{"model_id": "x", "table": "Nope"}]})

    assert validate_selection(raw, {"sales-ds": SALES}) is None


def test_focused_context_shows_selected_tables_in_full_with_meanings() -> None:
    schema = normalize(REAL_SHAPE)
    selection = Selection(
        {"kmjl": {"NET SALES MASTER", "Dim_Product"}},
        [("store", "NET SALES MASTER[Location Name]")],
    )

    context = focused(authz_with(("kmjl", "KMJL Sales New")), {"kmjl": schema}, selection)

    assert "NET SALES MASTER[Location Name] (Text)" in context.text
    assert "Dim_Product[LOB] (Text)" in context.text
    assert '"store" -> NET SALES MASTER[Location Name]' in context.text
    assert "Dim_Product[ProductKey] filters NET SALES MASTER[ProductKey]" in context.text
    assert set(context.schemas) == {"kmjl"}


def test_only_large_models_get_the_extra_round_trip() -> None:
    assert needs_selection({"sales-ds": SALES}, 80) is False
    assert needs_selection({"sales-ds": SALES}, 0) is True


# --- whole turn (test database) -----------------------------------------------------------------


@pytest.fixture
async def env(committed_sessionmaker: async_sessionmaker[AsyncSession]) -> Env:
    return await build_env(committed_sessionmaker)


@pytest.mark.integration
async def test_planner_gets_the_selected_tables_and_word_meanings(env: Env) -> None:
    settings = SETTINGS.model_copy(update={"agent_table_selection_min_columns": 0})
    gateway = WrappedGateway()
    powerbi = PowerBIService(gateway, None, UnavailableTokenBroker(), env.authz_service, settings)
    selection = {
        "tables": [{"model_id": "sales-ds", "table": "Sales"}],
        "mappings": [{"term": "line of business", "field": "Product[LOB]"}],
    }
    llm = ScriptedLLM([json.dumps(selection), plan(GOLD_STEP), grounded_answer])
    agent = Agent(
        llm, env.retriever, env.user_schemas, powerbi, env.authz_service, env.sm, settings,
        today=lambda: date(2026, 10, 6),
    )  # fmt: skip

    events = await env.run(agent, "user-a", "GOLD line of business sales this year")

    selector_input = llm.calls[0][-1].content
    assert "TABLE Customer" in selector_input and "TABLE Product" in selector_input
    planner_input = llm.calls[1][-1].content
    assert '"line of business" -> Product[LOB]' in planner_input
    assert "Customer[CreditLimit]" not in planner_input  # table not selected
    assert text_of(events).startswith("GOLD net sales this FY were")
