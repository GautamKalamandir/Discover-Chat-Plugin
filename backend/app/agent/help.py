"""Conversational turns that need no data: greetings, "what can I ask?", "what data do I have?".

The reply text is written by the server, never by the LLM. It is built only from the user's own
allowed models (gate G1) and their visible schema (OLS), so it can't reveal anything the user may
not see (Q16): nothing in it depends on models outside the user's AuthorizedContext.
"""

import calendar
import re
from collections.abc import Sequence

from app.authz.models import ModelSummary
from app.semantic.normalizer import NormalizedSchema

QUICK = re.compile(
    r"^\s*(?:(?P<greeting>hi+|hello+|hey+|hiya|namaste|good\s+(?:morning|afternoon|evening|day))"
    r"|(?P<thanks>thanks?|thank\s+you(?:\s+so\s+much)?|thx|ty|ok(?:ay)?|cool|great|bye|goodbye))"
    r"(?:\s+(?:there|bot|chatbot|team|all))?\s*[!.?]*\s*$",
    re.IGNORECASE,
)

NO_SOURCES = (
    "No data sources are available to you in the chatbot yet. Ask your report author or "
    "Power BI administrator to give you access."
)

_TEXT_TYPES = {"text", "string"}
_PREFERRED_MEASURE_WORDS = ("net sales", "sales", "revenue", "amount", "total")
_PREFERRED_DIMENSION_WORDS = (
    "lob",
    "category",
    "location",
    "store",
    "region",
    "product",
    "brand",
    "city",
    "vendor",
    "department",
)


def quick_topic(question: str) -> str | None:
    """'greeting' or 'thanks' for small talk that needs no planning at all."""
    match = QUICK.match(question)
    if match is None:
        return None
    return "greeting" if match.group("greeting") else "thanks"


def example_questions(schema: NormalizedSchema | None) -> list[str]:
    """Up to three example questions from the user's own visible measures and columns."""
    if schema is None:
        return []
    measures = [o for o in schema.objects if o.kind == "measure" and not o.is_hidden]
    columns = [
        o
        for o in schema.objects
        if o.kind == "column"
        and not o.is_hidden
        and (o.data_type or "").lower() in _TEXT_TYPES
        and any(w in o.name.lower() for w in _PREFERRED_DIMENSION_WORDS)
    ]
    if not measures:
        return []
    measure = min(
        measures,
        key=lambda m: next(
            (i for i, w in enumerate(_PREFERRED_MEASURE_WORDS) if w in m.name.lower()),
            len(_PREFERRED_MEASURE_WORDS),
        ),
    ).name
    examples = [f"What is {measure} this financial year?"]
    if columns:
        examples.append(f"{measure} by {columns[0].name} for last financial year")
    examples.append(f"Why did {measure} change this financial year compared to last year?")
    return examples


def help_reply(
    topic: str,
    *,
    user_name: str | None,
    models: Sequence[ModelSummary],
    schema: NormalizedSchema | None,
    fiscal_start_month: int,
) -> str:
    if topic == "thanks":
        return "You're welcome! Ask me anything else about your data."
    if not models:
        greeting = f"Hi {_first_name(user_name)}! " if topic == "greeting" else ""
        return greeting + NO_SOURCES

    sources = "You can ask about: " + ", ".join(f"**{m.name}**" for m in models) + "."
    examples = example_questions(schema)
    try_these = (
        ("\n\nTry for example:\n" + "\n".join(f"- {q}" for q in examples)) if examples else ""
    )

    if topic == "data_sources":
        return sources + try_these
    if topic == "out_of_scope":
        return "I can only answer questions about your Power BI data. " + sources + try_these
    capabilities = (
        "I answer questions about your Power BI data, using your own Power BI permissions. I can:\n"
        "- give totals and KPIs for any period (the financial year starts in "
        f"{calendar.month_name[fiscal_start_month]})\n"
        "- break numbers down and rank them (by store, category, month, top 5 …)\n"
        "- compare periods or data sources, and show where a change came from\n"
        '- follow up on my last answer ("and last year?", "only GOLD")'
    )
    if topic == "greeting":
        return f"Hi {_first_name(user_name)}! {capabilities}\n\n{sources}{try_these}"
    return f"{capabilities}\n\n{sources}{try_these}"  # capabilities


def _first_name(name: str | None) -> str:
    return (name or "there").split()[0]
