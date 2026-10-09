"""Stage 12: grounded answer.

The LLM writes the wording from computed FACTS and ROWS; then every number in its text must match a
number we computed or received (to the precision it is shown). Otherwise a deterministic template
answer is used instead. The answer is checked before any of it is sent to the user.
"""

import json
import logging
import re
from typing import Any

from app.agent.analyzer import Facts, StepFacts, to_number
from app.agent.executor import StepOutcome
from app.agent.planner import load_prompt, untrusted
from app.core.errors import AppError
from app.llm.base import ChatMessage, LLMProvider

logger = logging.getLogger(__name__)

DEV_DATA_NOTE = "\n\n_(Development data, not real figures.)_"

_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_DATES = re.compile(
    rf"\b\d{{1,2}}\s+{_MONTHS}\s+\d{{4}}\b|\b{_MONTHS}\s+\d{{4}}\b|\bfy\s?\d{{2,4}}(?:\s?[-/]\s?\d{{2,4}})?\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(
    r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)\s*(%|k|m|mn|million|b|bn|billion|lakhs?|lacs?|crores?|cr)?(?![\w])",
    re.IGNORECASE,
)
_SCALE = {
    "k": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
    "lakh": 1e5,
    "lakhs": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "crore": 1e7,
    "crores": 1e7,
    "cr": 1e7,
}


def ungrounded_numbers(text: str, allowed: list[float]) -> list[str]:
    """Numbers in `text` that don't match any allowed value at the precision they're written."""
    bad: list[str] = []
    for match in _NUMBER.finditer(_DATES.sub(" ", text)):
        raw, suffix = match.group(1), (match.group(2) or "").lower()
        digits = raw.replace(",", "")
        try:
            number = float(digits)
        except ValueError:
            continue
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        if suffix != "%" and decimals == 0 and abs(number) <= 10:
            continue  # ordinals and small counts ("top 5", "2 models")
        if suffix != "%" and 1900 <= number <= 2100 and decimals == 0:
            continue  # years
        scale = _SCALE.get(suffix, 1.0)
        value = number * scale
        tolerance = 0.5 * (10**-decimals) * scale + 1e-9
        if not any(abs(abs(value) - abs(a)) <= max(tolerance, abs(a) * 1e-6) for a in allowed):
            bad.append(match.group(0).strip())
    return bad


def format_number(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def template_answer(facts: Facts) -> str:
    notes = "".join(f"\nNote: {n}" for n in facts.notes)
    if facts.change is not None:
        return _template_change(facts.change) + notes
    return _template_steps(facts) + notes


def _template_steps(facts: Facts) -> str:
    parts: list[str] = []
    for step in facts.steps:
        parts.append(_template_step(step))
    for comparison in facts.comparisons:
        pct = comparison["percent_change"]
        subject = comparison.get("measure") or comparison.get("group")
        parts.append(
            f"Difference in {subject} ({comparison['first']} vs "
            f"{comparison['second']}): {format_number(comparison['difference'])}"
            + (f" ({pct:+.2f}%)." if pct is not None else ".")
        )
    return "\n".join(parts)


def _template_change(change: dict[str, Any]) -> str:
    scope = f" ({', '.join(change['filters'])})" if change["filters"] else ""
    pct = change["percent_change"]
    lines = [
        f"{change['analysed']}{scope} went from {format_number(change['baseline_value'])} "
        f"({change['baseline_period']}) to {format_number(change['current_value'])} "
        f"({change['current_period']}), a change of {format_number(change['change'])}"
        + (f" ({pct:+.2f}%)." if pct is not None else ".")
    ]
    for breakdown in change["breakdowns"]:
        for key, title in (("biggest_decreases", "decreases"), ("biggest_increases", "increases")):
            movers = breakdown[key]
            if movers:
                items = "; ".join(
                    f"{m['item']} {format_number(m['change'])}"
                    + (
                        f" ({format_number(m['share_of_total_change'])}% of the change)"
                        if m["share_of_total_change"] is not None
                        else ""
                    )
                    for m in movers
                )
                lines.append(f"- By {breakdown['by']}, biggest {title}: {items}.")
    lines.append(
        "These show where the change happened in your data; the data can't show outside reasons."
    )
    return "\n".join(lines)


def _template_step(step: StepFacts) -> str:
    scope = "; ".join(filter(None, [", ".join(step.filters), step.period]))
    scope = f" ({scope})" if scope else ""
    if step.row_count == 0:
        return f"No data was found for {step.label}{scope}."
    if step.single_values:
        values = ", ".join(f"{k}: {format_number(v)}" for k, v in step.single_values.items())
        return f"{step.label}{scope}: {values}."
    lines = [f"{step.label}{scope}:"]
    for row in step.top_rows:
        label = " / ".join(str(row.get(c)) for c in step.group_columns) or "-"
        value = ", ".join(
            format_number(n) if (n := to_number(row.get(c))) is not None else str(row.get(c))
            for c in step.value_columns
        )
        lines.append(f"- {label}: {value}")
    if step.truncated:
        lines.append("(Only part of the list is shown.)")
    return "\n".join(lines)


def _rounded(node: Any) -> Any:
    """Floats to 2 decimals for the LLM (live: it echoed 2,699.952970939907). The grounding
    check still compares against the exact values, at the precision the answer shows."""
    if isinstance(node, float):
        return round(node, 2)
    if isinstance(node, dict):
        return {k: _rounded(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_rounded(v) for v in node]
    return node


class AnswerWriter:
    def __init__(self, llm: LLMProvider, *, rows_to_llm: int) -> None:
        self._llm = llm
        self._rows = rows_to_llm

    async def write(self, question: str, facts: Facts, outcomes: list[StepOutcome]) -> str:
        rows: list[dict[str, Any]] = []
        allowed = facts.numbers()
        for outcome in outcomes:
            for row in outcome.result.rows[: self._rows]:
                rows.append({"step": outcome.step.label, **row})
                allowed += [n for n in (to_number(v) for v in row.values()) if n is not None]
        messages = [
            ChatMessage("system", load_prompt("answer")),
            ChatMessage(
                "user",
                "\n".join(
                    [
                        "QUESTION",
                        untrusted(question),
                        "FACTS",
                        untrusted(json.dumps(_rounded(facts.as_dict()), default=str)),
                        "ROWS",
                        untrusted(json.dumps(_rounded(rows), default=str)[:20000]),
                    ]
                ),
            ),
        ]
        try:
            text = (await self._llm.complete(messages)).content.strip()
        except AppError as exc:
            logger.warning("Answer generation failed (%s); using template answer", exc.code)
            text = ""
        bad = ungrounded_numbers(text, allowed) if text else ["<empty>"]
        if bad:
            logger.warning("Answer not grounded (%d numbers); using template answer", len(bad))
            text = template_answer(facts)
        if "dev_synthetic" in facts.data_sources:
            text += DEV_DATA_NOTE
        return text
