"""Power BI gateway contract.

Gateways receive the *user's* delegated Power BI token (On-Behalf-Of), so Power BI enforces
workspace/item permissions, RLS and OLS. They never decide authorization themselves: callers go
through `PowerBIService`, which applies gate G3 first.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.powerbi.errors import CapabilityNotSupportedError


class GatewayCapability(StrEnum):
    EXECUTE = "execute"
    SCHEMA = "schema"
    VALUE_SEARCH = "value_search"


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool
    gateway: str
    duration_ms: int = 0

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class GatewayPayload:
    """Schema / value-search output, kept as returned until its real shape is confirmed live
    (spike S3); Phase 7 builds the structured metadata from it."""

    data: Any
    gateway: str
    metadata: dict[str, Any] = field(default_factory=dict)


class PowerBIGateway(ABC):
    name: str
    capabilities: frozenset[GatewayCapability]
    # True when the path needs Build permission on the model (Execute Queries REST API).
    requires_build_permission: bool = False
    # OAuth scope of the token this gateway needs; None = the Power BI REST scope.
    token_scope: str | None = None

    @abstractmethod
    async def execute_dax(
        self, user_token: str, dataset_id: str, dax: str, *, max_rows: int, user_key: str
    ) -> QueryResult:
        """`user_key` (tenant:object id) identifies the user for per-user limits."""

    async def get_schema(self, user_token: str, dataset_id: str) -> GatewayPayload:
        raise CapabilityNotSupportedError(f"{self.name} cannot read model schemas")

    async def search_values(
        self, user_token: str, dataset_id: str, terms: list[str]
    ) -> GatewayPayload:
        raise CapabilityNotSupportedError(f"{self.name} cannot search values")

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""
