"""Power BI gateway contract.

Every method takes the *user's* delegated Power BI token (obtained via On-Behalf-Of), so Power BI
itself enforces workspace/item permissions, RLS and OLS. Implementations (Fabric IQ MCP, Execute
Queries REST) arrive in Phase 6.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SemanticModelRef:
    dataset_id: str
    name: str
    workspace_id: str | None = None


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class PowerBIGateway(ABC):
    name: str

    @abstractmethod
    async def list_accessible_models(self, user_token: str) -> list[SemanticModelRef]: ...

    @abstractmethod
    async def get_schema(self, user_token: str, dataset_id: str) -> dict[str, Any]: ...

    @abstractmethod
    async def search_values(
        self, user_token: str, dataset_id: str, text: str, column: str | None = None
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def execute_dax(
        self, user_token: str, dataset_id: str, dax: str, max_rows: int | None = None
    ) -> QueryResult: ...
