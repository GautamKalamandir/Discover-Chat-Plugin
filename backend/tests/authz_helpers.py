from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import AuthenticatedUser, RequestContext
from app.authz.models import ProbeOutcome
from app.authz.probe import ModelAccessProbe
from app.db.repositories.registry import RegistryRepository

# dataset id -> (name, domain, chatbot_enabled)
REGISTRY = {
    "sales-ds": ("Sales", "Sales", True),
    "hr-ds": ("HR", "HR", True),
    "inventory-ds": ("Inventory", "Inventory", True),
    "finance-ds": ("Finance", "Finance", True),
    "manpower-ds": ("Manpower", "HR", True),
    "draft-ds": ("Draft model", None, False),  # registered but not enabled for the chatbot
}


class FakeProbe(ModelAccessProbe):
    """Simulates Power BI. `grants[oid]` = dataset ids the user can access; `unknown` = Power BI
    doesn't answer for these ids."""

    source = "fake"

    def __init__(self, grants: dict[str, set[str]], unknown: set[str] | None = None) -> None:
        self.grants = grants
        self.unknown = unknown or set()
        self.calls: list[list[str]] = []

    async def check(
        self, ctx: RequestContext, dataset_ids: Sequence[str]
    ) -> dict[str, ProbeOutcome]:
        self.calls.append(sorted(dataset_ids))
        granted = self.grants.get(ctx.user.object_id, set())
        return {
            d: ProbeOutcome.UNKNOWN
            if d in self.unknown
            else ProbeOutcome.ALLOWED
            if d in granted
            else ProbeOutcome.DENIED
            for d in dataset_ids
        }

    @property
    def probed(self) -> list[str]:
        return sorted(d for call in self.calls for d in call)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def request_ctx(oid: str = "user-a", correlation_id: str = "corr-1") -> RequestContext:
    user = AuthenticatedUser(oid, "tenant-1", f"{oid}@contoso.com", oid, frozenset(), None)
    return RequestContext(user=user, correlation_id=correlation_id, access_token="t")


async def seed_registry(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session, session.begin():
        repo = RegistryRepository(session)
        workspace = await repo.upsert_workspace(
            pbi_workspace_id="ws-x", entra_tenant_id="tenant-1", name="Workspace X"
        )
        for dataset_id, (name, domain, enabled) in REGISTRY.items():
            await repo.upsert_semantic_model(
                pbi_dataset_id=dataset_id,
                workspace=workspace,
                name=name,
                domain=domain,
                chatbot_enabled=enabled,
            )
