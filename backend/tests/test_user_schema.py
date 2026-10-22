"""The per-user schema cache: concurrent requests share one fetch, failures fail closed."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.authz.models import AuthorizedContext
from app.core.config import Settings
from app.semantic.schema_source import SchemaSource
from app.semantic.user_schema import UserSchemaService
from tests.authz_helpers import request_ctx
from tests.semantic_helpers import fixture

SETTINGS = Settings(_env_file=None)


class SlowSource(SchemaSource):
    """Counts fetches; each one takes a moment so that callers overlap."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fetches = 0
        self.fail = fail

    async def fetch(self, authz: AuthorizedContext, dataset_id: str) -> Any:
        self.fetches += 1
        await asyncio.sleep(0.05)
        if self.fail:
            raise RuntimeError("Power BI said: secret value 42")
        return fixture(dataset_id)


def authz(oid: str) -> AuthorizedContext:
    return AuthorizedContext(request_ctx(oid), uuid.uuid4(), {}, datetime.now(UTC))


async def test_concurrent_requests_share_one_fetch() -> None:
    source = SlowSource()
    service = UserSchemaService(source, None, SETTINGS)
    user = authz("user-a")

    schemas = await asyncio.gather(*(service.visible(user, "sales-ds") for _ in range(5)))

    assert source.fetches == 1
    assert all(s is schemas[0] for s in schemas) and schemas[0] is not None
    await service.visible(user, "sales-ds")
    assert source.fetches == 1  # cached afterwards


async def test_each_user_gets_their_own_fetch() -> None:
    source = SlowSource()
    service = UserSchemaService(source, None, SETTINGS)

    await asyncio.gather(
        service.visible(authz("user-a"), "sales-ds"), service.visible(authz("user-b"), "sales-ds")
    )

    assert source.fetches == 2  # never shared across users (OLS differs per user)


async def test_a_caller_giving_up_does_not_cancel_the_shared_fetch() -> None:
    source = SlowSource()
    service = UserSchemaService(source, None, SETTINGS)
    user = authz("user-a")

    impatient = asyncio.create_task(service.visible(user, "sales-ds"))
    patient = asyncio.create_task(service.visible(user, "sales-ds"))
    await asyncio.sleep(0)
    impatient.cancel()

    assert await patient is not None
    with pytest.raises(asyncio.CancelledError):
        await impatient
    assert source.fetches == 1


async def test_a_failed_fetch_fails_closed_without_logging_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = SlowSource(fail=True)
    service = UserSchemaService(source, None, SETTINGS)
    user = authz("user-a")

    assert await service.visible(user, "sales-ds") is None
    assert "secret value" not in caplog.text and "RuntimeError" in caplog.text
    assert await service.visible(user, "sales-ds") is None
    assert source.fetches == 2  # failures aren't cached
