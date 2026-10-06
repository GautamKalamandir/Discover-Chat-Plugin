import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from app.auth.models import RequestContext


class ProbeOutcome(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    UNKNOWN = (
        "unknown"  # Power BI didn't answer (throttled, 5xx, timeout): fail closed, don't cache
    )


@dataclass(frozen=True)
class ModelSummary:
    dataset_id: str  # Power BI dataset (semantic model) id — the id used by the API and visual
    name: str
    domain: str | None
    model_uuid: uuid.UUID  # registry primary key


@dataclass(frozen=True)
class AuthorizedContext:
    """Built by the server for each request (gate G1). Holds ONLY models the user may access.

    Nothing produced by the LLM can add to it: the only way to grow it is
    `AuthorizationService.assert_allowed`, which verifies with Power BI first.
    """

    request: RequestContext
    user_id: uuid.UUID
    allowed: Mapping[str, ModelSummary]
    resolved_at: datetime
    # Models Power BI couldn't be asked about this request (fail closed; reported as 503 if needed).
    unverified: frozenset[str] = frozenset()
    # Models whose decision came from a live Power BI check during this request.
    verified_live: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed", MappingProxyType(dict(self.allowed)))

    def is_allowed(self, dataset_id: str) -> bool:
        return dataset_id in self.allowed

    @property
    def allowed_models(self) -> list[ModelSummary]:
        return sorted(self.allowed.values(), key=lambda m: m.name.lower())

    def with_allowed(
        self,
        added: Iterable[ModelSummary],
        *,
        verified_live: Iterable[str],
        unverified: Iterable[str] = (),
    ) -> "AuthorizedContext":
        merged = {**self.allowed, **{m.dataset_id: m for m in added}}
        return replace(
            self,
            allowed=merged,
            unverified=(self.unverified | set(unverified)) - set(merged),
            verified_live=self.verified_live | set(verified_live),
        )
