"""Formats captured live on the tenant in Phase 2 (spikes S3/S4, 2026-10-08), pinned so the
parsers keep matching what Fabric IQ and the Power BI REST API really return (docs/spikes.md)."""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from app.agent.values import _matches, value_hints
from app.core.config import Settings
from app.diagnostics.capture import mask_text_payload
from app.powerbi.errors import DaxQueryError
from app.powerbi.fabric_iq import (
    FabricIqMcpGateway,
    _canonical_columns,
    _dax_error_text,
    _payload_from_result,
    _rows_from_result,
)
from app.powerbi.rest import _error_message
from app.semantic.normalizer import normalize
from tests.fake_fabric_iq import FakeFabricIq, running

CAPTURED_SCHEMA = Path(__file__).parent / "fixtures" / "schemas" / "captured-fabric-iq-schema.json"
CITATION: dict[str, Any] = {
    "artifact_citation": {"ArtifactId": "m-1", "Name": "Sales", "Description": "", "Url": "u"}
}
DAX_ERROR = (
    "DAX query syntax error: DAX query execution failed: Query (1, 18) Cannot find table 'Table'."
)


def _result(text: str, structured: dict[str, Any] | None = None) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured,
        is_error=False,
    )


# --- GetSemanticModelSchema ---------------------------------------------------------------------


def test_schema_payload_is_the_json_text_not_the_citation() -> None:
    schema_text = json.dumps({"schema": {"Tables": [{"Name": "Sales", "Columns": []}]}})

    payload = _payload_from_result(_result(schema_text, CITATION))

    assert payload == {"schema": {"Tables": [{"Name": "Sales", "Columns": []}]}}


def test_captured_schema_shape_normalizes_completely() -> None:
    """The anonymized real payload: 26 tables, 928 columns, 105 measures."""
    schema = normalize(json.loads(CAPTURED_SCHEMA.read_text(encoding="utf-8")))

    kinds = [o.kind for o in schema.objects]
    assert (kinds.count("table"), kinds.count("column"), kinds.count("measure")) == (26, 928, 105)
    assert {o.data_type for o in schema.objects if o.kind == "column"} == {
        "DateTime",
        "Text",
        "Integer",
        "Double",
        "Boolean",
    }


# --- ExecuteQuery -------------------------------------------------------------------------------


def test_dax_error_arrives_as_ordinary_text() -> None:
    assert _dax_error_text(_result(DAX_ERROR, CITATION)) == DAX_ERROR
    assert _dax_error_text(_result('[{"[Total]": 1}]', CITATION)) is None


def test_rows_ignore_the_citation_and_read_the_text() -> None:
    rows = _rows_from_result(_result('[{"Product[LOB]": "GOLD", "[Net Sales]": 12.5}]', CITATION))

    assert rows == [{"Product[LOB]": "GOLD", "[Net Sales]": 12.5}]


async def test_text_dax_error_raises_dax_query_error_for_the_repair_loop() -> None:
    fake = FakeFabricIq(dax_errors={"Bad": DAX_ERROR}, dax_errors_as_text=True)
    settings = Settings(_env_file=None, fabric_iq_mcp_url="http://fabric-iq.test/mcp")
    error: Exception | None = None
    async with running(fake) as factory:
        gateway = FabricIqMcpGateway(settings, http_client_factory=factory)
        try:
            await gateway.execute_dax("tok", "m-1", "EVALUATE 'Bad'", max_rows=5, user_key="u")
        except Exception as exc:  # re-raised outside the fake server's task group
            error = exc

    assert isinstance(error, DaxQueryError)
    assert error.dax_error == DAX_ERROR


# --- ValueSearch --------------------------------------------------------------------------------


def test_value_search_results_are_qualified_with_their_table() -> None:
    payload = {
        "Results": {
            "gold": [
                {"Table": "Dim_Product", "Column": "LOB", "Value": "GOLD", "Score": 1.0},
                {"Table": "NET SALES MASTER", "Column": "LOB", "Value": "GOLD", "Score": 1.0},
            ]
        }
    }

    assert _matches(payload) == [
        ("'Dim_Product'[LOB]", "GOLD"),
        ("'NET SALES MASTER'[LOB]", "GOLD"),
    ]


# --- REST executeQueries ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "Query (1, 18) Cannot find table '<oii>Table</oii>'.",
            "Query (1, 18) Cannot find table 'Table'.",
        ),
        ("Plain message", "Plain message"),
    ],
)
def test_rest_error_detail_drops_power_bi_name_markers(value: str, expected: str) -> None:
    body = {
        "error": {
            "code": "DatasetExecuteQueriesError",
            "pbi.error": {
                "code": "DatasetExecuteQueriesError",
                "details": [{"code": "DetailsMessage", "detail": {"type": 1, "value": value}}],
            },
        }
    }

    assert _error_message(body) == expected


# --- ExecuteQuery success (spike S3, second capture) ------------------------------------------


def test_successful_execute_query_rows_are_positional() -> None:
    text = json.dumps(
        {
            "executionResult": {
                "tables": [
                    {
                        "columns": [{"name": "Product.LOB", "type": "Text"},
                                    {"name": "Total Net Sales", "type": "Double"}],
                        "rows": [["GOLD", 12.5], ["SILVER", 3.25]],
                    }
                ]
            },
            "semanticModel": {"Name": "Sales"},
        }
    )  # fmt: skip

    rows = _rows_from_result(_result(text, CITATION))

    assert rows == [
        {"Product.LOB": "GOLD", "Total Net Sales": 12.5},
        {"Product.LOB": "SILVER", "Total Net Sales": 3.25},
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Product.LOB", "Product[LOB]"),  # dotted
        ("'NET SALES MASTER'[Location Name]", "NET SALES MASTER[Location Name]"),
        ("Product[LOB]", "Product[LOB]"),
        ("[Total Net Sales]", "[Total Net Sales]"),
        ("Total Net Sales", "[Total Net Sales]"),  # an alias of the query that was sent
    ],
)
def test_fabric_column_names_are_mapped_to_the_rest_spelling(name: str, expected: str) -> None:
    dax = "EVALUATE SUMMARIZECOLUMNS('Product'[LOB], \"Total Net Sales\", [Total Net Sales])"

    rows = _canonical_columns([{name: 1}], dax)

    assert list(rows[0]) == [expected]


def test_capture_keeps_result_column_names_but_masks_values() -> None:
    text = json.dumps(
        {"executionResult": {"tables": [{"columns": [{"name": "Product.LOB", "type": "Text"}],
                                         "rows": [["GOLD"]]}]}}
    )  # fmt: skip

    masked = mask_text_payload(text)

    table = masked["executionResult"]["tables"][0]
    assert table["columns"] == [{"name": "Product.LOB", "type": "Text"}]
    assert table["rows"] == [["<text:4>"]]


def test_value_hints_rank_matches_per_word() -> None:
    payload = {
        "Results": {
            "surar": [
                {"Table": "Dim Key", "Column": "Location", "Value": "SURAT", "Score": 0.71},
                {"Table": "NET SALES MASTER", "Column": "Location Name", "Value": "SURAT",
                 "Score": 0.86},
            ]
        }
    }  # fmt: skip

    assert value_hints(payload) == [
        '"surar": \'NET SALES MASTER\'[Location Name] = "SURAT" (match score 0.86)',
        '"surar": \'Dim Key\'[Location] = "SURAT" (match score 0.71)',
    ]
