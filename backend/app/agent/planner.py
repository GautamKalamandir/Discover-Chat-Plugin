"""Stage 4: the LLM turns the question + allowed context into a structured QueryPlan."""

import json
from datetime import date
from pathlib import Path
from typing import Any

from app.agent.models import QueryPlan, RepairedDax
from app.llm.base import ChatMessage, LLMProvider

PROMPTS = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def untrusted(text: str) -> str:
    # Neutralise any closing tag inside the data so it can't break out of the block.
    return "<untrusted_data>\n" + text.replace("</untrusted_data>", "") + "\n</untrusted_data>"


class Planner:
    def __init__(self, llm: LLMProvider, *, fiscal_start_month: int, max_steps: int) -> None:
        self._llm = llm
        self._fiscal_start_month = fiscal_start_month
        self._max_steps = max_steps
        self._schema = json.dumps(QueryPlan.model_json_schema(), separators=(",", ":"))

    def _system(self, today: date) -> str:
        return (
            load_prompt("planner")
            .replace("{fiscal_start_month}", str(self._fiscal_start_month))
            .replace("{today}", today.isoformat())
            .replace("{max_steps}", str(self._max_steps))
            .replace("{schema}", self._schema)
        )

    def messages(
        self,
        question: str,
        context: str,
        *,
        today: date,
        previous: dict[str, Any] | None = None,
        report_filters: list[dict[str, Any]] | None = None,
    ) -> list[ChatMessage]:
        parts = ["CONTEXT", untrusted(context)]
        if previous:
            parts += [
                "PREVIOUS TURN (for follow-up questions)",
                untrusted(json.dumps(previous, default=str)[:4000]),
            ]
        if report_filters:
            parts += [
                "REPORT FILTERS CURRENTLY APPLIED (apply them unless the user asks otherwise)",
                untrusted(json.dumps(report_filters, default=str)[:2000]),
            ]
        parts += ["QUESTION", untrusted(question)]
        return [ChatMessage("system", self._system(today)), ChatMessage("user", "\n".join(parts))]

    async def plan(self, messages: list[ChatMessage]) -> QueryPlan:
        return await self._llm.complete_json(messages, QueryPlan)

    async def replan(
        self, messages: list[ChatMessage], previous: QueryPlan, problems: list[str]
    ) -> QueryPlan:
        feedback = (
            "Your plan cannot be used: " + "; ".join(problems)[:1500] + ". Use only objects and "
            "models listed in the CONTEXT. If the question needs something that is not listed, "
            'return "status": "cannot_answer" with the missing terms in "unresolved_terms".'
        )
        return await self._llm.complete_json(
            [
                *messages,
                ChatMessage("assistant", previous.model_dump_json()),
                ChatMessage("user", feedback),
            ],
            QueryPlan,
        )

    async def replan_with_values(
        self, messages: list[ChatMessage], previous: QueryPlan, hints: list[str]
    ) -> QueryPlan:
        """One more try after words the plan couldn't place were found as values in the data."""
        feedback = (
            "Some words you could not place were found as values in the data (word: column = "
            "stored value, match score). The user may have misspelt them. Use a match as a filter "
            "when it fits the question, with the stored value exactly as given. Words that still "
            'don\'t fit stay in "unresolved_terms".\n' + untrusted("\n".join(hints)[:3000])
        )
        return await self._llm.complete_json(
            [
                *messages,
                ChatMessage("assistant", previous.model_dump_json()),
                ChatMessage("user", feedback),
            ],
            QueryPlan,
        )

    async def repair_dax(self, dax: str, error: str, context: str) -> str:
        messages = [
            ChatMessage("system", load_prompt("repair").replace("{{", "{").replace("}}", "}")),
            ChatMessage(
                "user",
                "\n".join(
                    [
                        "CONTEXT",
                        untrusted(context),
                        "FAILED QUERY",
                        untrusted(dax),
                        "ERROR",
                        untrusted(error[:1500]),
                    ]
                ),
            ),
        ]
        return (await self._llm.complete_json(messages, RepairedDax)).dax
