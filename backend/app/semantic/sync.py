"""Keeps the semantic index current (Q10b: on use + admin CLI)."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import SemanticModel
from app.semantic.documents import build_documents
from app.semantic.indexer import IndexStats, SemanticIndexer
from app.semantic.normalizer import NormalizedSchema, normalize

logger = logging.getLogger(__name__)


class MetadataSync:
    def __init__(
        self,
        indexer: SemanticIndexer,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._indexer = indexer
        self._sessionmaker = sessionmaker
        self._clock = clock
        self._in_flight: dict[str, asyncio.Task[None]] = {}
        self._done: set[tuple[str, str]] = set()  # (dataset id, schema hash) already indexed

    async def sync_schema(self, dataset_id: str, schema: NormalizedSchema) -> IndexStats | None:
        """Index `schema` for a registered model. Returns None for unknown models."""
        async with self._sessionmaker() as session:
            model = (
                await session.scalars(
                    select(SemanticModel).where(SemanticModel.pbi_dataset_id == dataset_id)
                )
            ).one_or_none()
        if model is None:
            logger.warning("Skipping sync for unregistered model %s", dataset_id)
            return None
        docs = build_documents(
            schema, model_name=model.name, domain=model.domain, description=model.description
        )
        stats = await self._indexer.index(model.id, docs)
        await self._indexer.mark_synced(model.id, schema.schema_hash, self._clock())
        self._done.add((dataset_id, schema.schema_hash))
        return stats

    async def sync_payload(self, dataset_id: str, payload: Any) -> IndexStats | None:
        return await self.sync_schema(dataset_id, normalize(payload))

    def schedule(self, dataset_id: str, schema: NormalizedSchema) -> None:
        """On-use sync: runs in the background, at most once per model at a time, and only when
        this schema version hasn't been indexed yet. Never delays the user's answer."""
        if (dataset_id, schema.schema_hash) in self._done or dataset_id in self._in_flight:
            return
        task = asyncio.create_task(self._run(dataset_id, schema), name=f"sync-{dataset_id}")
        self._in_flight[dataset_id] = task
        task.add_done_callback(lambda _: self._in_flight.pop(dataset_id, None))

    async def wait_idle(self) -> None:
        """For tests and shutdown: wait for background syncs to finish."""
        while self._in_flight:
            await asyncio.gather(*self._in_flight.values(), return_exceptions=True)

    async def _run(self, dataset_id: str, schema: NormalizedSchema) -> None:
        try:
            async with self._sessionmaker() as session:
                current = await session.scalar(
                    select(SemanticModel.schema_version).where(
                        SemanticModel.pbi_dataset_id == dataset_id
                    )
                )
            if current == schema.schema_hash:
                self._done.add((dataset_id, schema.schema_hash))
                return
            await self.sync_schema(dataset_id, schema)
        except Exception:
            logger.exception("Background metadata sync failed for %s", dataset_id)

    async def aclose(self) -> None:
        for task in list(self._in_flight.values()):
            task.cancel()
        await asyncio.gather(*self._in_flight.values(), return_exceptions=True)
