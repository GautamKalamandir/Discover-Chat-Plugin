"""app.jobs.metadata: dev fixture loading, status, and admin sync with a mocked sign-in."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.jobs import metadata as cli
from app.powerbi.base import GatewayPayload
from tests.authz_helpers import seed_registry
from tests.semantic_helpers import FIXTURES, HashEmbedder, fixture

pytestmark = pytest.mark.integration


class FakeGateway:
    instances: list["FakeGateway"] = []  # noqa: RUF012

    def __init__(self, settings: Settings) -> None:
        self.tokens: list[str] = []
        FakeGateway.instances.append(self)

    async def get_schema(self, user_token: str, dataset_id: str) -> GatewayPayload:
        self.tokens.append(user_token)
        if dataset_id == "inventory-ds":
            raise RuntimeError("model unavailable")
        return GatewayPayload(data=fixture(dataset_id.replace("manpower", "hr")), gateway="fake")

    async def aclose(self) -> None:
        pass


@pytest.fixture
async def cli_env(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> async_sessionmaker[AsyncSession]:
    await seed_registry(committed_sessionmaker)
    settings = Settings(_env_file=None, database_url=test_database_url)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "create_embedding_provider", lambda _: HashEmbedder())
    monkeypatch.setattr(cli, "FabricIqMcpGateway", FakeGateway)
    FakeGateway.instances = []
    return committed_sessionmaker


async def doc_count(sm: async_sessionmaker[AsyncSession], dataset_id: str) -> int:
    async with sm() as session:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM semantic_documents d JOIN semantic_models m "
                    "ON m.id = d.semantic_model_id WHERE m.pbi_dataset_id = :d"
                ),
                {"d": dataset_id},
            )
            or 0
        )


async def test_load_fixture_indexes_a_registered_model(
    cli_env: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    code = await cli.run(
        ["load-fixture", "--dataset-id", "sales-ds", "--file", str(FIXTURES / "sales-ds.json")]
    )

    assert code == 0
    assert await doc_count(cli_env, "sales-ds") > 5
    assert "embedded" in capsys.readouterr().out


async def test_load_fixture_refuses_unregistered_models(
    cli_env: async_sessionmaker[AsyncSession],
) -> None:
    code = await cli.run(
        ["load-fixture", "--dataset-id", "nope", "--file", str(FIXTURES / "sales-ds.json")]
    )

    assert code == 1


async def test_admin_sync_all_uses_the_admin_token_and_reports_failures(
    cli_env: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_sign_in(settings: Settings) -> str:
        return "admin-token"

    code = await cli.run(["sync", "--all"], acquire_token=fake_sign_in)

    out = capsys.readouterr().out
    assert code == 1  # one model failed
    assert "inventory-ds: FAILED" in out
    assert await doc_count(cli_env, "sales-ds") > 0
    assert set(FakeGateway.instances[0].tokens) == {"admin-token"}


async def test_status_lists_indexed_models(
    cli_env: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    await cli.run(["load-fixture", "--dataset-id", "hr-ds", "--file", str(FIXTURES / "hr-ds.json")])
    capsys.readouterr()

    assert await cli.run(["status"]) == 0
    assert "hr-ds  test/hash-64" in capsys.readouterr().out


def test_sign_in_requires_the_admin_cli_registration() -> None:
    with pytest.raises(SystemExit, match="ADMIN_CLI_CLIENT_ID"):
        cli.acquire_admin_token(Settings(_env_file=None))
