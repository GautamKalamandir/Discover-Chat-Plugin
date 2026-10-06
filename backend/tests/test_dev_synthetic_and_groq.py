import os
from datetime import date

import pytest

from app.agent.dax_builder import build_dax
from app.agent.models import PlanStep, QueryPlan
from app.agent.planner import Planner
from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.llm.factory import create_llm_provider
from app.powerbi.dev_synthetic import DevSyntheticGateway

DAX = build_dax(
    PlanStep.model_validate(
        {
            "model_id": "sales-ds",
            "label": "x",
            "measures": ["Sales[Total Net Sales]"],
            "group_by": ["Product[Category]"],
            "filters": [{"column": "Product[LOB]", "op": "in", "values": ["GOLD", "SILVER"]}],
        }
    ),
    today=date(2026, 10, 6),
    fiscal_start_month=4,
)


async def test_dev_gateway_returns_grouped_deterministic_rows() -> None:
    gateway = DevSyntheticGateway(Settings(_env_file=None, environment="local"))

    first = await gateway.execute_dax("", "sales-ds", DAX, max_rows=100, user_key="u")
    second = await gateway.execute_dax("", "sales-ds", DAX, max_rows=100, user_key="u")

    assert first.columns == ["Product[Category]", "[Total Net Sales]"]
    assert [r["Product[Category]"] for r in first.rows] == ["DEV-A", "DEV-B", "DEV-C"]
    assert first.rows == second.rows
    assert first.gateway == "dev_synthetic"


async def test_dev_gateway_uses_filter_values_for_filtered_group_columns() -> None:
    gateway = DevSyntheticGateway(Settings(_env_file=None, environment="local"))
    dax = DAX.replace("'Product'[Category],\n", "'Product'[LOB],\n")

    result = await gateway.execute_dax("", "sales-ds", dax, max_rows=100, user_key="u")

    assert [r["Product[LOB]"] for r in result.rows] == ["GOLD", "SILVER"]


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_dev_gateway_is_refused_outside_local(environment: str) -> None:
    with pytest.raises(ConfigurationError, match="dev_synthetic"):
        DevSyntheticGateway(Settings(_env_file=None, environment=environment))


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("GROQ_API_KEY"), reason="GROQ_API_KEY not set")
async def test_real_groq_model_returns_a_valid_plan() -> None:
    """Run with: GROQ_API_KEY=... uv run pytest -m network -k groq"""
    llm = create_llm_provider(Settings(_env_file=None, groq_api_key=os.environ["GROQ_API_KEY"]))
    planner = Planner(llm, fiscal_start_month=4, max_steps=4)
    context = (
        '<model id="sales-ds" name="Sales" domain="Sales">\nMeasures:\n'
        "  Sales[Total Net Sales] - Revenue after returns\nColumns:\n"
        "  Product[LOB] (String) - Line of Business\n  Sales[InvoiceDate] (DateTime)\n</model>"
    )
    messages = planner.messages(
        "What are GOLD sales this financial year?", context, today=date(2026, 10, 6)
    )

    plan = await planner.plan(messages)

    assert isinstance(plan, QueryPlan) and plan.status == "ready"
    step = plan.steps[0]
    assert step.model_id == "sales-ds" and "Total Net Sales" in step.measures[0]
    assert step.time is not None and step.time.period in ("current_fy", "fytd")
    await llm.aclose()
