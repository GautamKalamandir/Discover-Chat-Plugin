"""`python -m app.jobs.registry kpi ...`: admin KPI pins for /summary reports (ADR 0015 §5)."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.jobs import registry as registry_cli
from tests.authz_helpers import seed_registry

pytestmark = pytest.mark.integration


@pytest.fixture
async def cli(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> async_sessionmaker[AsyncSession]:
    await seed_registry(committed_sessionmaker)
    settings = Settings(_env_file=None, database_url=test_database_url)
    monkeypatch.setattr(registry_cli, "get_settings", lambda: settings)
    return committed_sessionmaker


async def pins(sm: async_sessionmaker[AsyncSession]) -> list[tuple[str, str]]:
    async with sm() as session:
        rows = await session.execute(
            text(
                "SELECT maps_to, term FROM business_glossary "
                "WHERE entry_type = 'kpi_definition' ORDER BY created_at"
            )
        )
        return [(r.maps_to, r.term) for r in rows]


async def test_kpis_can_be_pinned_relabelled_listed_and_removed(
    cli: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    measure = "Sales[Total Net Sales]"
    base = ["kpi", "add", "--dataset-id", "sales-ds", "--measure", measure]

    assert await registry_cli.run(base) == 0
    assert await pins(cli) == [(measure, "Total Net Sales")]  # label defaults to the measure
    assert await registry_cli.run([*base, "--label", "Net sales"]) == 0
    assert await pins(cli) == [(measure, "Net sales")]  # relabelled, not duplicated

    assert await registry_cli.run(["kpi", "list", "--dataset-id", "sales-ds"]) == 0
    assert "Sales[Total Net Sales]  (Net sales)" in capsys.readouterr().out

    remove = ["kpi", "remove", "--dataset-id", "sales-ds", "--measure", measure]
    assert await registry_cli.run(remove) == 0
    assert await pins(cli) == []
    assert await registry_cli.run(remove) == 1  # nothing left to remove


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["kpi", "add", "--dataset-id", "unknown-ds", "--measure", "Sales[X]"], 1),
        (["kpi", "add", "--dataset-id", "sales-ds", "--measure", "Total Net Sales"], 2),
    ],
)
async def test_unknown_models_and_unqualified_measures_are_refused(
    cli: async_sessionmaker[AsyncSession], argv: list[str], code: int
) -> None:
    assert await registry_cli.run(argv) == code
    assert await pins(cli) == []
