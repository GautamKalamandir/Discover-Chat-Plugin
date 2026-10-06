"""Server-side validation of an LLM plan (stage 6). The LLM is never trusted.

- every model in the plan must be allowed for the user -> AuthorizationService.assert_allowed
  (whole request denied otherwise, audited; scenario 7)
- every model must be one we showed the planner (its schema was fetched as the user)
- every referenced table column / measure must be in the user's own visible schema (scenario 16)
"""

import uuid
from dataclasses import dataclass

from app.agent.models import PlanStep, QueryPlan, parse_ref
from app.authz.models import AuthorizedContext
from app.authz.service import AuthorizationService
from app.semantic.normalizer import NormalizedSchema, object_key


class PlanInvalidError(Exception):
    """The plan references things it may not use. `problems` go back to the planner once."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class ValidatedPlan:
    plan: QueryPlan
    authz: AuthorizedContext


async def validate_plan(
    plan: QueryPlan,
    authz: AuthorizedContext,
    authz_service: AuthorizationService,
    schemas: dict[str, NormalizedSchema],
    *,
    max_steps: int,
    session_id: uuid.UUID | None,
) -> ValidatedPlan:
    if not plan.steps:
        raise PlanInvalidError(["a ready plan needs at least one step"])
    if len(plan.steps) > max_steps:
        raise PlanInvalidError([f"use at most {max_steps} steps"])

    # Authorization first: a non-allowed model denies the whole request (generic message).
    model_ids = list(dict.fromkeys(step.model_id for step in plan.steps))
    authz = await authz_service.assert_allowed(authz, model_ids, session_id=session_id)

    problems: list[str] = []
    steps: list[PlanStep] = []
    for step in plan.steps:
        schema = schemas.get(step.model_id)
        if schema is None:
            problems.append(f"model {step.model_id} was not in the provided context")
            continue
        fixed, step_problems = _check_step(step, schema)
        problems += step_problems
        steps.append(fixed)
    if problems:
        raise PlanInvalidError(problems)
    return ValidatedPlan(plan.model_copy(update={"steps": steps}), authz)


def _check_step(step: PlanStep, schema: NormalizedSchema) -> tuple[PlanStep, list[str]]:
    problems: list[str] = []
    visible = schema.visible_keys

    measures = []
    for ref in step.measures:
        resolved = _resolve_measure(ref, schema)
        if resolved is None:
            problems.append(f"unknown measure {ref}")
        else:
            measures.append(resolved)

    def need_column(ref: str) -> None:
        parsed = parse_ref(ref)
        if parsed is None or object_key("column", *parsed) not in visible:
            problems.append(f"unknown column {ref}")

    for ref in step.group_by:
        need_column(ref)
    for flt in step.filters:
        need_column(flt.column)
    for agg in step.aggregations:
        need_column(agg.column)
    if step.time is not None:
        need_column(step.time.column)
    if not (measures or step.aggregations or step.custom_dax):
        problems.append(f"step '{step.label}' has nothing to calculate")
    return step.model_copy(update={"measures": measures}), problems


def _resolve_measure(ref: str, schema: NormalizedSchema) -> str | None:
    """Accepts 'Table[Measure]' or a bare/bracketed measure name when it is unambiguous."""
    parsed = parse_ref(ref)
    if parsed is not None:
        return ref if object_key("measure", *parsed) in schema.visible_keys else None
    name = ref.strip().strip("[]")
    matches = [o for o in schema.objects if o.kind == "measure" and o.name == name]
    if len(matches) == 1:
        return f"{matches[0].table}[{matches[0].name}]"
    return None
