"""Fixtures for tests that need PostgreSQL (`docker compose -f infra/docker-compose.yml up -d`).

Uses a separate database (TEST_DATABASE_URL, default `discover_test`), migrated once per run.
Each test runs inside a transaction that is rolled back, so tests never see each other's data.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://discover:discover@localhost:5432/discover_test"
)
BACKEND_DIR = Path(__file__).resolve().parents[1]


async def _ensure_database(url: str) -> None:
    target = make_url(url)
    admin = create_async_engine(
        target.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with admin.connect() as conn:
            found = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target.database}
            )
            if not found:
                await conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    try:
        asyncio.run(_ensure_database(TEST_DATABASE_URL))
    except OSError as exc:
        pytest.skip(f"PostgreSQL is not reachable ({exc}). Run: docker compose -f infra/... up -d")

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def db_session(test_database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        outer = await conn.begin()
        session = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await outer.rollback()
    await engine.dispose()


APP_TABLES = (
    "audit_events, query_executions, chat_messages, chat_sessions, user_model_access, "
    "business_glossary, model_metadata, reports, semantic_models, workspaces, users, tenants"
)


@pytest.fixture
async def committed_sessionmaker(
    test_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """For code that commits its own transactions. Tables are wiped before and after the test."""
    engine = create_async_engine(test_database_url, poolclass=NullPool)

    async def wipe() -> None:
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {APP_TABLES} RESTART IDENTITY CASCADE"))

    await wipe()
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await wipe()
        await engine.dispose()
