"""Where a user's view of a model schema comes from."""

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from app.authz.models import AuthorizedContext
from app.core.config import AuthProviderName, Settings
from app.powerbi.service import PowerBIService


class SchemaSource(ABC):
    @abstractmethod
    async def fetch(self, authz: AuthorizedContext, dataset_id: str) -> Any:
        """Schema payload as the user sees it (OLS applied by Power BI)."""


class PowerBISchemaSource(SchemaSource):
    """Fabric IQ GetSemanticModelSchema with the user's own token (via PowerBIService, gate G3)."""

    def __init__(self, powerbi: PowerBIService) -> None:
        self._powerbi = powerbi

    async def fetch(self, authz: AuthorizedContext, dataset_id: str) -> Any:
        return (await self._powerbi.get_schema(authz, model_id=dataset_id)).data


class DevFixtureSchemaSource(SchemaSource):
    """AUTH_PROVIDER=dev only. Reads `<dir>/<dataset id>.json`; a per-user file
    `<dataset id>.<object id>.json` takes precedence, to simulate object-level security locally."""

    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)

    async def fetch(self, authz: AuthorizedContext, dataset_id: str) -> Any:
        for name in (f"{dataset_id}.{authz.request.user.object_id}.json", f"{dataset_id}.json"):
            path = self._dir / name
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        raise FileNotFoundError(f"No dev schema fixture for {dataset_id} in {self._dir}")


def create_schema_source(settings: Settings, powerbi: PowerBIService) -> SchemaSource:
    if settings.auth_provider is AuthProviderName.DEV:
        return DevFixtureSchemaSource(settings.dev_schema_fixture_dir)
    return PowerBISchemaSource(powerbi)
