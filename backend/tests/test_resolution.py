"""Value evidence (ADR 0014): looked up as the user, never decided by the server."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.agent.models import QueryPlan
from app.agent.resolution import (
    Candidate,
    Evidence,
    TermEvidence,
    ValueEvidence,
    apply_stored_values,
    similarity,
    unknown_values,
)
from app.authz.models import AuthorizedContext, ModelSummary
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.powerbi.base import GatewayPayload, QueryResult
from app.powerbi.service import PowerBIService
from tests.authz_helpers import request_ctx

SETTINGS = Settings(_env_file=None, environment="test", value_cache_minutes=10)
CATEGORY = "Product[Category]"
STORED = ["CAT - BANGLE", "CAT - BANGLE SET", "CAT - CHAIN", "CAT - RING"]


def authz(oid: str = "user-a") -> AuthorizedContext:
    model = ModelSummary("sales-ds", "Sales", "Sales", uuid.uuid4())
    return AuthorizedContext(request_ctx(oid), uuid.uuid4(), {"sales-ds": model}, datetime.now(UTC))


def plan_with(values: list[str], column: str = CATEGORY, **extra: Any) -> QueryPlan:
    step = {
        "model_id": "sales-ds",
        "label": "sales",
        "measures": ["Sales[Total Net Sales]"],
        "filters": [{"column": column, "op": "in", "values": values}],
    }
    return QueryPlan.model_validate({"status": "ready", "steps": [step], **extra})


class FakePowerBI:
    """ValueSearch over STORED in Product[Category] (scores like Fabric IQ), or unavailable."""

    def __init__(self, *, search: bool = True, column_name: str = "Product[Category]") -> None:
        self.search = search
        self.column_name = column_name
        self.searches: list[tuple[list[str], Any]] = []
        self.queries: list[tuple[str, str]] = []

    async def search_values(
        self, authz: AuthorizedContext, *, model_id: str, terms: list[str], scope: Any = None
    ) -> GatewayPayload:
        self.searches.append((terms, scope))
        if not self.search:
            raise AppError(ErrorCode.POWERBI_UNAVAILABLE, "down", 503)
        results = {
            term: [
                {"Table": "Product", "Column": "Category", "Value": v, "Score": similarity(term, v)}
                for v in STORED
                if similarity(term, v) >= 0.6
            ]
            for term in terms
        }
        return GatewayPayload({"Results": results}, "fake")

    async def execute_query(
        self, authz: AuthorizedContext, *, model_id: str, dax: str, max_rows: int
    ) -> QueryResult:
        self.queries.append((authz.request.user.key, dax))
        rows = [{self.column_name: v} for v in STORED]
        return QueryResult([self.column_name], rows, False, "fake")


def evidence_for(fake: FakePowerBI) -> ValueEvidence:
    return ValueEvidence(cast(PowerBIService, fake), SETTINGS)


# --- ranking (fallback only) --------------------------------------------------------------------


def test_a_word_inside_a_prefixed_name_ranks_high_and_typos_rank_by_closeness() -> None:
    assert similarity("kosamba", "STR - KOSAMBA") >= 0.95
    assert similarity("KOSAMBA", "STR - KOSAMBA") >= 0.95
    assert similarity("kismaba", "STR - KOSAMBA") > similarity("kismaba", "STR - SURAT")
    assert similarity("gold", "GOLD") == 1.0


# --- gathering ----------------------------------------------------------------------------------


async def test_value_stored_as_written_needs_no_decision_and_gets_its_stored_spelling() -> None:
    fake = FakePowerBI()

    evidence = await evidence_for(fake).gather(plan_with(["cat - ring"]), authz())

    assert not evidence.needs_decision()
    assert fake.searches[0][1] == [{"tableName": "Product", "columnNames": ["Category"]}]
    fixed = apply_stored_values(plan_with(["cat - ring"]), evidence)
    assert fixed.steps[0].filters[0].values == ["CAT - RING"]


async def test_misspelt_value_brings_the_closest_stored_values_for_the_agent() -> None:
    evidence = await evidence_for(FakePowerBI()).gather(plan_with(["bangel"]), authz())

    assert evidence.needs_decision()
    line = evidence.lines()[0]
    assert line.startswith('"bangel" (used for Product[Category]): not stored as written.')
    assert "'Product'[Category] = \"CAT - BANGLE\"" in line


async def test_word_the_plan_could_not_place_is_searched_model_wide() -> None:
    fake = FakePowerBI()
    plan = QueryPlan(status="cannot_answer", unresolved_terms=["bangle"])

    evidence = await evidence_for(fake).gather(plan, authz(), unresolved_models=["sales-ds"])

    assert fake.searches == [(["bangle"], None)]
    assert evidence.needs_decision() and evidence.lines()[0].startswith('"bangle": ')


async def test_nothing_similar_is_evidence_too() -> None:
    evidence = await evidence_for(FakePowerBI()).gather(plan_with(["xyzzy"]), authz())

    assert evidence.needs_decision()
    assert evidence.lines() == ['"xyzzy" (used for Product[Category]): nothing similar is stored']


async def test_without_value_search_the_columns_values_are_read_and_ranked_as_the_user() -> None:
    fake = FakePowerBI(search=False)
    values = evidence_for(fake)

    evidence = await values.gather(plan_with(["bangel"]), authz("user-a"))
    await values.gather(plan_with(["chian"]), authz("user-a"))  # same column: cached
    await values.gather(plan_with(["bangel"]), authz("user-b"))  # RLS: never shared

    assert [user.split(":")[1] for user, _ in fake.queries] == ["user-a", "user-b"]
    assert "TOPN(" in fake.queries[0][1] and "VALUES('Product'[Category])" in fake.queries[0][1]
    assert evidence.terms[0].candidates[0].value == "CAT - BANGLE"


async def test_values_in_an_unexpected_column_are_not_used_as_evidence() -> None:
    fake = FakePowerBI(search=False, column_name="[Total Net Sales]")

    evidence = await evidence_for(fake).gather(plan_with(["bangel"]), authz())

    assert not evidence.needs_decision()  # no evidence either way: the plan runs as written


async def test_values_are_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    await evidence_for(FakePowerBI()).gather(plan_with(["bangel"]), authz())

    assert "bangel" not in caplog.text and "BANGLE" not in caplog.text


# --- checking options ---------------------------------------------------------------------------


def test_option_values_must_be_stored_in_the_users_data() -> None:
    evidence = Evidence(
        [
            TermEvidence(
                "sales-ds", "bangel", CATEGORY, True, None, [Candidate("'Product'[Category]",
                                                                       "CAT - BANGLE", 0.8)]
            )
        ]
    )  # fmt: skip

    assert unknown_values(plan_with(["CAT - BANGLE"]), evidence) == []
    assert unknown_values(plan_with(["CAT - INVENTED"]), evidence) == [
        "Product[Category] has no stored value 'CAT - INVENTED'"
    ]
    # Nothing could be looked up for another model: nothing to check there.
    other = plan_with(["anything"]).model_copy(
        update={"steps": [plan_with(["anything"]).steps[0].model_copy(update={"model_id": "x"})]}
    )
    assert unknown_values(other, evidence) == []
