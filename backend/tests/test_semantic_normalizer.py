import pytest

from app.semantic.documents import build_documents
from app.semantic.normalizer import normalize
from tests.semantic_helpers import fixture


def test_fixture_payload_is_normalized() -> None:
    schema = normalize(fixture("sales-ds"))

    keys = {o.key for o in schema.objects}
    assert {
        "table:Sales",
        "column:Sales[InvoiceDate]",
        "column:Sales[SalesKey]",
        "measure:Sales[Total Net Sales]",
        "column:Product[LOB]",
        "column:Customer[CreditLimit]",
    } <= keys
    assert schema.visible_keys == frozenset(keys)
    measure = next(o for o in schema.objects if o.name == "Total Net Sales")
    assert measure.expression == "SUM(Sales[NetAmount])"
    assert schema.ai_instructions[0].startswith("Revenue means")
    assert schema.verified_answers[0].fields == (
        "column:Product[LOB]",
        "measure:Sales[Total Net Sales]",
    )


@pytest.mark.parametrize(
    "wrap",
    [
        lambda p: p,
        lambda p: {"schema": p},
        lambda p: {"result": __import__("json").dumps(p)},
        lambda p: __import__("json").dumps(p),
    ],
)
def test_wrapped_payloads_are_unwrapped(wrap: object) -> None:
    schema = normalize(wrap(fixture("hr-ds")))  # type: ignore[operator]

    assert "measure:Employee[Headcount]" in schema.visible_keys


def test_field_names_are_case_insensitive() -> None:
    payload = {"Tables": [{"Name": "T", "Columns": [{"Name": "C", "DataType": "String"}]}]}

    assert normalize(payload).visible_keys == {"table:T", "column:T[C]"}


@pytest.mark.parametrize("payload", [None, "not json", 42, [], {"unexpected": True}])
def test_unknown_shapes_give_an_empty_schema_not_guesses(payload: object) -> None:
    assert normalize(payload).objects == ()


def test_hash_is_stable_and_changes_with_content() -> None:
    a = normalize(fixture("sales-ds"))
    b = normalize(fixture("sales-ds"))
    c = normalize(fixture("sales-ds.user-b"))

    assert a.schema_hash == b.schema_hash
    assert a.schema_hash != c.schema_hash


def test_documents_mention_one_object_each_and_skip_hidden_objects() -> None:
    docs = build_documents(
        normalize(fixture("sales-ds")), model_name="Sales", domain="Sales", description=None
    )
    by_key = {d.doc_key: d for d in docs}

    assert "column:Sales[SalesKey]" not in by_key  # hidden technical column
    lob = by_key["column:Product[LOB]"]
    assert lob.referenced_objects == ("column:Product[LOB]",)
    assert "Line of Business" in lob.content
    measure = by_key["measure:Sales[Total Net Sales]"]
    assert measure.extra == {"expression": "SUM(Sales[NetAmount])"}
    assert "SUM(" not in measure.content  # DAX is kept in extra, not embedded


def test_model_summary_never_lists_objects() -> None:
    docs = build_documents(
        normalize(fixture("sales-ds")), model_name="Sales", domain="Sales", description="Sales KPIs"
    )
    summary = next(d for d in docs if d.doc_type == "model_summary")

    assert summary.referenced_objects == ()
    assert "CreditLimit" not in summary.content and "Customer" not in summary.content


def test_instructions_and_verified_answers_become_documents() -> None:
    docs = build_documents(
        normalize(fixture("sales-ds")), model_name="Sales", domain=None, description=None
    )
    types = [d.doc_type for d in docs]

    assert types.count("ai_instruction") == 1
    answer = next(d for d in docs if d.doc_type == "verified_answer")
    assert "LOB wise sales" in answer.content
    assert answer.referenced_objects == (
        "column:Product[LOB]",
        "measure:Sales[Total Net Sales]",
    )
