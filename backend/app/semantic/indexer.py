"""Writes documents into pgvector; one embedding space per (provider, model) — ADR 0007."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import EmbeddingSpace, SemanticDocument, SemanticModel
from app.embeddings.base import EmbeddingProvider
from app.semantic.documents import SemanticDoc

logger = logging.getLogger(__name__)

EMBED_BATCH = 64


def hnsw_index_name(space_id: int) -> str:
    return f"ix_semdoc_hnsw_space_{space_id}"


@dataclass(frozen=True)
class IndexStats:
    embedded: int
    unchanged: int


class SemanticIndexer:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], embedder: EmbeddingProvider
    ) -> None:
        self._sessionmaker = sessionmaker
        self._embedder = embedder
        self._space: EmbeddingSpace | None = None

    @property
    def embedder(self) -> EmbeddingProvider:
        return self._embedder

    async def space(self) -> EmbeddingSpace:
        """The active space for the configured provider/model; created (with its index) once."""
        if self._space is not None:
            return self._space
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                insert(EmbeddingSpace)
                .values(
                    provider=self._embedder.name,
                    model=self._embedder.model,
                    dimensions=self._embedder.dimension,
                )
                .on_conflict_do_nothing(index_elements=["provider", "model"])
            )
            space = (
                await session.scalars(
                    select(EmbeddingSpace).where(
                        EmbeddingSpace.provider == self._embedder.name,
                        EmbeddingSpace.model == self._embedder.model,
                    )
                )
            ).one()
            # Partial expression index: the cast fixes the dimension for this space only.
            await session.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {hnsw_index_name(space.id)} "
                    "ON semantic_documents USING hnsw "
                    f"((embedding::vector({int(space.dimensions)})) vector_cosine_ops) "
                    f"WHERE embedding_space_id = {int(space.id)}"
                )
            )
        self._space = space
        return space

    async def index(self, semantic_model_id: uuid.UUID, docs: list[SemanticDoc]) -> IndexStats:
        space = await self.space()
        async with self._sessionmaker() as session:
            rows = await session.execute(
                select(SemanticDocument.doc_key, SemanticDocument.content_hash).where(
                    SemanticDocument.semantic_model_id == semantic_model_id,
                    SemanticDocument.embedding_space_id == space.id,
                )
            )
            existing = {doc_key: content_hash for doc_key, content_hash in rows.all()}

        changed = [d for d in docs if existing.get(d.doc_key) != d.content_hash]
        unchanged_keys = [d.doc_key for d in docs if existing.get(d.doc_key) == d.content_hash]

        vectors: list[list[float]] = []
        for start in range(0, len(changed), EMBED_BATCH):
            batch = changed[start : start + EMBED_BATCH]
            vectors.extend(
                await self._embedder.embed_documents([f"{d.title}. {d.content}" for d in batch])
            )

        async with self._sessionmaker() as session, session.begin():
            for doc, vector in zip(changed, vectors, strict=True):
                values = {
                    "doc_type": doc.doc_type,
                    "source": doc.source,
                    "title": doc.title[:600],
                    "content": doc.content,
                    "content_hash": doc.content_hash,
                    "referenced_objects": list(doc.referenced_objects),
                    "extra": doc.extra or None,
                    "embedding": vector,
                    "last_seen_at": func.now(),
                }
                await session.execute(
                    insert(SemanticDocument)
                    .values(
                        semantic_model_id=semantic_model_id,
                        embedding_space_id=space.id,
                        doc_key=doc.doc_key,
                        **values,
                    )
                    .on_conflict_do_update(
                        index_elements=["semantic_model_id", "embedding_space_id", "doc_key"],
                        set_={**values, "updated_at": func.now()},
                    )
                )
            if unchanged_keys:
                await session.execute(
                    update(SemanticDocument)
                    .where(
                        SemanticDocument.semantic_model_id == semantic_model_id,
                        SemanticDocument.embedding_space_id == space.id,
                        SemanticDocument.doc_key.in_(unchanged_keys),
                    )
                    .values(last_seen_at=func.now())
                )
        logger.info(
            "Indexed model %s: %d embedded, %d unchanged",
            semantic_model_id,
            len(changed),
            len(unchanged_keys),
        )
        return IndexStats(embedded=len(changed), unchanged=len(unchanged_keys))

    async def mark_synced(
        self, semantic_model_id: uuid.UUID, schema_hash: str, now: datetime
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                update(SemanticModel)
                .where(SemanticModel.id == semantic_model_id)
                .values(schema_version=schema_hash, last_synced_at=now)
            )
