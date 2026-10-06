"""Gate G3: a tool never runs for a model outside the server-built context."""

import uuid
from datetime import UTC, datetime

import pytest

from app.authz.guards import requires_model_access
from app.authz.messages import GENERIC_DENIAL
from app.authz.models import AuthorizedContext, ModelSummary
from app.core.errors import AppError
from tests.authz_helpers import request_ctx

ran: list[str] = []


@requires_model_access("model_id")
async def execute_query(authz: AuthorizedContext, *, model_id: str, dax: str) -> str:
    ran.append(model_id)
    return f"result from {model_id}"


@requires_model_access("model_ids")
async def compare(authz: AuthorizedContext, *, model_ids: list[str]) -> str:
    ran.extend(model_ids)
    return "combined"


@pytest.fixture
def authz() -> AuthorizedContext:
    ran.clear()
    sales = ModelSummary("sales-ds", "Sales", "Sales", uuid.uuid4())
    return AuthorizedContext(
        request=request_ctx(),
        user_id=uuid.uuid4(),
        allowed={"sales-ds": sales},
        resolved_at=datetime.now(UTC),
    )


async def test_allowed_model_runs_the_tool(authz: AuthorizedContext) -> None:
    assert await execute_query(authz, model_id="sales-ds", dax="EVALUATE {1}") == (
        "result from sales-ds"
    )


@pytest.mark.parametrize("model_id", ["finance-ds", "", "SALES-DS"])
async def test_other_model_is_refused_before_the_tool_body(
    authz: AuthorizedContext, model_id: str
) -> None:
    with pytest.raises(AppError) as excinfo:
        await execute_query(authz, model_id=model_id, dax="EVALUATE {1}")

    assert excinfo.value.message == GENERIC_DENIAL
    assert ran == []


async def test_one_disallowed_model_blocks_a_multi_model_tool(authz: AuthorizedContext) -> None:
    with pytest.raises(AppError):
        await compare(authz, model_ids=["sales-ds", "finance-ds"])

    assert ran == []


async def test_empty_model_list_is_refused(authz: AuthorizedContext) -> None:
    with pytest.raises(AppError):
        await compare(authz, model_ids=[])


async def test_tool_without_context_is_a_programming_error() -> None:
    with pytest.raises(TypeError, match="AuthorizedContext"):
        await execute_query(None, model_id="sales-ds", dax="x")  # type: ignore[arg-type]


def test_decorating_a_tool_without_the_parameter_fails_at_import() -> None:
    with pytest.raises(TypeError, match="no parameter"):

        @requires_model_access("model_id")
        async def broken(authz: AuthorizedContext) -> None: ...


def test_context_cannot_be_mutated(authz: AuthorizedContext) -> None:
    with pytest.raises(TypeError):
        authz.allowed["finance-ds"] = authz.allowed["sales-ds"]  # type: ignore[index]
