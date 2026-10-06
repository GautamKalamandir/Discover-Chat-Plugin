import pytest

from app.core.errors import AppError
from app.powerbi.dax_guard import ensure_read_only_query
from app.powerbi.results import columns_of, rows_from_csv, rows_from_json

ROWS = [{"a": 1}, {"a": 2, "b": 3}]


@pytest.mark.parametrize(
    "payload",
    [
        {"results": [{"tables": [{"rows": ROWS}]}]},
        {"tables": [{"rows": ROWS}]},
        {"rows": ROWS},
        ROWS,
        {"result": '{"results": [{"tables": [{"rows": [{"a": 1}, {"a": 2, "b": 3}]}]}]}'},
    ],
)
def test_known_result_shapes(payload: object) -> None:
    assert rows_from_json(payload) == ROWS


@pytest.mark.parametrize("payload", [None, "text", 42, {"other": 1}, {"result": "not json"}])
def test_unknown_shapes_return_none(payload: object) -> None:
    assert rows_from_json(payload) is None


def test_columns_keep_first_seen_order_across_rows() -> None:
    assert columns_of(ROWS) == ["a", "b"]


def test_csv_rows() -> None:
    assert rows_from_csv("x,y\n1,2\n") == [{"x": "1", "y": "2"}]


@pytest.mark.parametrize(
    "dax",
    [
        "EVALUATE VALUES(T[c])",
        '  evaluate ROW("x", 1)',
        'DEFINE MEASURE T[m] = 1 EVALUATE ROW("m", [m])',
        "// comment\nEVALUATE T",
        "/* block */ EVALUATE T",
    ],
)
def test_read_only_queries_pass(dax: str) -> None:
    assert ensure_read_only_query(dax) == dax


@pytest.mark.parametrize(
    "dax",
    [
        "",
        "   ",
        "-- only a comment",
        "SELECT * FROM T",
        "EVALUATE $SYSTEM.TMSCHEMA_MEASURES",
        "EVALUATE INFO.TABLES()",
        "EVALUATE " + "x" * 20_001,
    ],
)
def test_everything_else_is_rejected(dax: str) -> None:
    with pytest.raises(AppError) as excinfo:
        ensure_read_only_query(dax)

    assert excinfo.value.code == "query_rejected"
