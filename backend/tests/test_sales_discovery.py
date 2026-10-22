"""Which of the user's models feed the /sales-summary tables (ADR 0016 §2, design §3): real
database, real authorization, fixture schemas (OLS for user-b), admin pins, and an LLM that answers
with scripted choices.

user-a may use Sales and HR (not Finance); user-b only Sales, with Customer[CreditLimit] hidden.
"""

import json
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import pytest
from sqlalchemy import update

from app.authz.models import ModelSummary
from app.core.errors import AppError, ErrorCode
from app.db.models import SemanticModel
from app.llm.base import ChatMessage, LLMProvider, LLMResponse
from app.llm.budget import current_limits
from app.reports.sales.discovery import (
    BATCH_CHARS,
    FIELDS_OUTPUT_TOKENS,
    MAX_MODEL_CHARS,
    MODELS_PER_CALL,
    TABLES_OUTPUT_TOKENS,
    SalesDiscovery,
    _batches,
    fillable,
    model_block,
    table_catalog,
)
from app.reports.sales.prompts import load_compact_prompt
from app.semantic.normalizer import normalize
from tests.authz_helpers import request_ctx
from tests.test_agent_pipeline import Env, build_env

pytestmark = pytest.mark.integration

SALES = {
    "dataset_id": "sales-ds",
    "is_sales": True,
    "lenses": ["lob_growth", "subcategory_loss", "price_range"],
    "date_column": "Sales[InvoiceDate]",
    "columns": {"lob": "Product[LOB]", "subcategory": "Product[Category]"},
    "measures": {"sales_value": "Sales[Total Net Sales]"},
    "reason": "Invoice-level sales.",
}
HR = {"dataset_id": "hr-ds", "is_sales": False, "reason": "Headcount, not sales."}
Reply = Callable[[Sequence[ChatMessage]], Any]


# Step 1 (ADR 0016 amendment 1): Sales is sales data (its three tables matter), HR isn't.
TABLES = {
    "models": [
        {"dataset_id": "sales-ds", "is_sales": True, "tables": ["Sales", "product", "Customer"]},
        {"dataset_id": "hr-ds", "is_sales": False, "tables": []},
    ]
}


class DiscoveryLLM(LLMProvider):
    """Answers step 1 (tables) with `tables`, and each step-2 call (fields) with the next
    scripted reply."""

    name = "scripted"
    model = "scripted"

    def __init__(self, *replies: Reply | dict[str, Any], tables: Any = None) -> None:
        self.replies = list(replies)
        self.tables = tables or TABLES
        self.calls: list[list[ChatMessage]] = []  # step 2 only
        self.table_calls: list[list[ChatMessage]] = []

    async def complete(
        self, messages: Sequence[ChatMessage], *, json_mode: bool = False
    ) -> LLMResponse:
        if "TASK: pick the tables" in messages[0].content:
            self.table_calls.append(list(messages))
            return LLMResponse(json.dumps(self.tables))
        assert "TASK: find which of the reader's data sources hold SALES" in messages[0].content
        self.calls.append(list(messages))
        reply = self.replies.pop(0)
        answer = reply(messages) if callable(reply) else reply
        return LLMResponse(json.dumps(answer))

    def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:  # pragma: no cover
        raise NotImplementedError


class FailingLLM(DiscoveryLLM):
    async def complete(
        self, messages: Sequence[ChatMessage], *, json_mode: bool = False
    ) -> LLMResponse:
        raise AppError(ErrorCode.LLM_UNAVAILABLE, "down", 503)


def answer(*models: dict[str, Any]) -> dict[str, Any]:
    return {"models": list(models)}


@pytest.fixture
async def env(committed_sessionmaker: Any) -> Env:
    return await build_env(committed_sessionmaker)


async def discover(env: Env, llm: DiscoveryLLM, oid: str = "user-a") -> Any:
    authz = await env.authz_service.build_context(request_ctx(oid))
    discovery = SalesDiscovery(
        llm,
        env.user_schemas,
        env.sm,
        tables_prompt=load_compact_prompt("sales_discovery_tables"),
        fields_prompt=load_compact_prompt("sales_discovery"),
    )
    return await discovery.discover(authz)


async def pin(env: Env, dataset_id: str, pins: dict[str, bool]) -> None:
    async with env.sm() as session, session.begin():
        await session.execute(
            update(SemanticModel)
            .where(SemanticModel.pbi_dataset_id == dataset_id)
            .values(report_lenses=pins)
        )


# --- the agent decides, the server checks -------------------------------------------------------


async def test_sales_models_and_their_tables_come_from_the_agent(env: Env) -> None:
    llm = DiscoveryLLM(answer(SALES, HR))

    found = await discover(env, llm)

    [sales] = found.models
    assert sales.model.dataset_id == "sales-ds"
    assert sales.lenses == ("lob_growth", "subcategory_loss")  # no price column: price_range out
    assert sales.date_column == "Sales[InvoiceDate]"
    assert sales.columns == {"lob": "Product[LOB]", "subcategory": "Product[Category]"}
    assert sales.measures == {"sales_value": "Sales[Total Net Sales]"}
    assert [m.dataset_id for m in found.skipped] == ["hr-ds"]  # not sales: left out
    assert found.unreadable == []
    assert len(llm.calls) == 1  # every field valid: no second round


async def test_the_agent_sees_only_allowed_models_and_visible_fields(env: Env) -> None:
    llm = DiscoveryLLM(answer(SALES))

    await discover(env, llm, oid="user-b")

    prompt = llm.calls[0][1].content
    assert '<model id="sales-ds"' in prompt
    assert "hr-ds" not in prompt and "finance-ds" not in prompt  # not allowed for user-b
    assert "CreditLimit" not in prompt  # hidden from user-b by object-level security
    assert "SalesKey" not in prompt  # a hidden technical column isn't advertised
    assert (
        "TABLE Sales\n  measures: Total Net Sales [Revenue after returns and discounts]\n"
        "  columns: InvoiceDate (date)"
    ) in prompt


@pytest.mark.scenario(16)
async def test_fields_the_user_cannot_see_are_refused_then_dropped(env: Env) -> None:
    # user-b can't see Customer[CreditLimit]; the agent insists on it as the price column.
    hidden = {**SALES, "columns": {**SALES["columns"], "price": "Customer[CreditLimit]"}}
    llm = DiscoveryLLM(answer(hidden), answer(hidden))

    found = await discover(env, llm, oid="user-b")

    assert "price column Customer[CreditLimit] is not listed" in llm.calls[1][-1].content
    [sales] = found.models
    assert "price" not in sales.columns
    assert "price_range" not in sales.lenses


async def test_invalid_fields_are_replanned_once(env: Env) -> None:
    wrong = {
        **SALES,
        "date_column": "Product[LOB]",
        "measures": {"sales_value": "Sales[Made Up]"},
    }
    llm = DiscoveryLLM(answer(wrong, HR), answer(SALES, HR))

    found = await discover(env, llm)

    feedback = llm.calls[1][-1].content
    assert "date_column Product[LOB] is not a listed date column" in feedback
    assert "sales_value measure Sales[Made Up] is not listed" in feedback
    assert found.models[0].lenses == ("lob_growth", "subcategory_loss")


async def test_a_column_the_user_can_see_is_kept_for_its_role(env: Env) -> None:
    # user-a can see Customer[CreditLimit], so it may serve as the price column.
    priced = {**SALES, "columns": {**SALES["columns"], "price": "Customer[CreditLimit]"}}

    found = await discover(env, DiscoveryLLM(answer(priced, HR)))

    assert found.models[0].lenses == ("lob_growth", "subcategory_loss", "price_range")


async def test_answers_about_models_outside_the_batch_are_ignored(env: Env) -> None:
    finance = {**SALES, "dataset_id": "finance-ds"}  # not allowed for user-a

    found = await discover(env, DiscoveryLLM(answer(finance, HR)))

    assert found.models == []
    assert {m.dataset_id for m in found.skipped} == {"sales-ds", "hr-ds"}


async def test_lower_is_better_keeps_only_listed_measures(env: Env) -> None:
    choice = {**SALES, "lower_is_better": ["Sales[Total Net Sales]", "Sales[Returns]"]}

    found = await discover(env, DiscoveryLLM(answer(choice, HR)))

    sales = found.models[0]
    assert sales.lower_is_better == frozenset({"Sales[Total Net Sales]"})
    assert not sales.higher_is_better("Sales[Total Net Sales]")


# --- admin pins ---------------------------------------------------------------------------------


async def test_admin_pins_add_and_remove_tables(env: Env) -> None:
    await pin(env, "sales-ds", {"subcategory_loss": False})
    await pin(env, "hr-ds", {"lob_growth": True})  # the agent says HR isn't sales

    found = await discover(env, DiscoveryLLM(answer(SALES, HR)))

    [sales] = found.models
    assert sales.lenses == ("lob_growth",)  # subcategory_loss switched off by the admin
    # HR has no fields for the pinned table, so it can't fill it and stays out (logged).
    assert [m.dataset_id for m in found.skipped] == ["hr-ds"]


async def test_a_pinned_table_the_model_cannot_fill_is_noted(env: Env) -> None:
    await pin(env, "sales-ds", {"stock_turn": True})  # no stock measure in Sales

    found = await discover(env, DiscoveryLLM(answer(SALES, HR)))

    sales = found.models[0]
    assert "stock_turn" not in sales.lenses
    assert sales.notes == ("stock_turn is pinned but this data source has no fields for it",)


async def test_a_pin_makes_a_model_count_even_when_the_agent_disagrees(env: Env) -> None:
    await pin(env, "sales-ds", {"lob_growth": True})
    not_sales = {**SALES, "is_sales": False, "lenses": []}

    found = await discover(env, DiscoveryLLM(answer(not_sales, HR)))

    assert [(m.model.dataset_id, m.lenses) for m in found.models] == [("sales-ds", ("lob_growth",))]


# --- failures -----------------------------------------------------------------------------------


async def test_models_are_unreadable_when_the_agent_is_unavailable(env: Env) -> None:
    found = await discover(env, FailingLLM())

    assert found.models == [] and found.skipped == []
    assert [m.dataset_id for m in found.unreadable] == ["hr-ds", "sales-ds"]


def test_what_each_table_needs() -> None:
    date, sales = "T[Date]", {"sales_value": "T[Sales]"}
    assert fillable("lob_growth", date, {"lob": "T[LOB]"}, sales)
    assert not fillable("lob_growth", None, {"lob": "T[LOB]"}, sales)  # no date column
    assert not fillable("lob_growth", date, {}, sales)
    assert fillable("subcategory_loss", date, {"subcategory": "T[Sub]"}, {"sales_qty": "T[Q]"})
    assert fillable("stock_turn", date, {}, {"stock_turn": "T[Turn]"})  # its own measure
    assert not fillable("stock_turn", None, {}, {"stock_turn": "T[Turn]"})  # periods need a date
    assert fillable("stock_turn", date, {}, {**sales, "stock_value": "T[Stock]"})
    assert not fillable("stock_turn", date, {}, sales)
    assert fillable("price_range", date, {"price_band": "T[Band]"}, sales)
    assert fillable("price_range", date, {"price": "T[MRP]"}, sales)
    assert not fillable("price_range", date, {"lob": "T[LOB]"}, sales)
    assert MODELS_PER_CALL == 10


# --- large models (a live model had 26 tables and 928 columns) ----------------------------------


def big_schema(tables: int, columns: int) -> Any:
    return normalize(
        {
            "tables": [
                {
                    "name": f"T{t:02d} {'SALES MASTER' if t == tables - 1 else 'OTHER'}",
                    "columns": [
                        {"name": "Bill Date", "dataType": "DateTime"},
                        *(
                            {"name": f"Some fairly long column name {c}", "dataType": "Double"}
                            for c in range(columns)
                        ),
                    ],
                    "measures": [{"name": f"Measure {t}"}],
                }
                for t in range(tables)
            ]
        }
    )


def test_every_table_of_a_large_model_is_listed() -> None:
    schema = big_schema(30, 60)
    model = ModelSummary("big-ds", "Big", None, uuid.uuid4())

    block = model_block(model, schema)

    assert len(block) <= MAX_MODEL_CHARS + len("\n</model>")
    assert all(f"TABLE T{t:02d}" in block for t in range(30))
    assert "TABLE T29 SALES MASTER" in block  # the last table alphabetically is still there
    assert block.count("Bill Date (date)") == 30  # date columns always stay
    assert "(and " in block and "Measure 29" in block


def test_large_models_are_sent_in_smaller_batches() -> None:
    schema = big_schema(30, 60)
    models = [ModelSummary(f"m{i}", f"M{i}", None, uuid.uuid4()) for i in range(5)]

    batches = _batches(models, lambda m: len(model_block(m, schema)))

    assert [len(b) for b in batches] == [1, 1, 1, 1, 1]  # each listing is near the call limit


# --- free-tier mode: two small steps (ADR 0016 amendment 1) -------------------------------------


async def test_step_two_lists_only_the_tables_picked_in_step_one(env: Env) -> None:
    tables = {
        "models": [
            {"dataset_id": "sales-ds", "is_sales": True, "tables": ["sales", "PRODUCT"]},
            {"dataset_id": "hr-ds", "is_sales": False},
        ]
    }
    llm = DiscoveryLLM(answer(SALES), tables=tables)

    found = await discover(env, llm)

    catalog = llm.table_calls[0][1].content
    assert "TABLE Customer" in catalog and '<model id="hr-ds"' in catalog  # step 1: everything
    fields = llm.calls[0][1].content
    assert "TABLE Sales" in fields and "TABLE Product" in fields  # names matched in any case
    assert "TABLE Customer" not in fields and "hr-ds" not in fields  # not picked / not sales
    assert found.models[0].tables == ("Sales", "Product")
    assert [m.dataset_id for m in found.skipped] == ["hr-ds"]


async def test_every_discovery_call_asks_for_a_short_answer(env: Env) -> None:
    seen: list[Any] = []

    class Recording(DiscoveryLLM):
        async def complete(
            self, messages: Sequence[ChatMessage], *, json_mode: bool = False
        ) -> LLMResponse:
            seen.append(current_limits())
            return await super().complete(messages, json_mode=json_mode)

    await discover(env, Recording(answer(SALES, HR)))

    assert [(lim.max_output_tokens, lim.reasoning_effort) for lim in seen] == [
        (TABLES_OUTPUT_TOKENS, "low"),
        (FIELDS_OUTPUT_TOKENS, "low"),
    ]


def test_the_catalog_lists_every_table_with_a_sample_of_its_columns() -> None:
    schema = big_schema(30, 60)
    model = ModelSummary("big-ds", "Big", None, uuid.uuid4())

    catalog = table_catalog(model, schema)

    assert all(f"TABLE T{t:02d}" in catalog for t in range(30))
    assert (
        "TABLE T00 OTHER (61 columns) | measures: Measure 0 | columns: Bill Date (date)," in catalog
    )
    # Too wide for 10 columns a table: the sample shrinks to 5, every table stays.
    assert "... (56 more)" in catalog and len(catalog) < BATCH_CHARS
