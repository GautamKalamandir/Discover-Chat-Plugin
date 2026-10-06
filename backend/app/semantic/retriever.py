"""Semantic retrieval (ADR 0007): gate -> hybrid ranking -> per-user schema intersection.

1. Only the user's allowed models are searched; the filter is part of the SQL, applied before
   ranking (scenario 9).
2. Vector similarity + full-text search (acronyms such as LOB/FY) are fused with Reciprocal Rank
   Fusion; verified answers and exact object-name matches get a small boost.
3. A document is returned only if the user can see every object it references in their own live
   schema (scenario 16). If the schema can't be fetched, object documents are dropped.
"""

import asyncio
import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz import messages
from app.authz.models import AuthorizedContext, ModelSummary
from app.core.config import Settings
from app.db.models import SemanticDocument
from app.semantic.indexer import SemanticIndexer
from app.semantic.normalizer import NormalizedSchema
from app.semantic.user_schema import UserSchemaService

RRF_K = 60
VERIFIED_ANSWER_BOOST = 0.02
EXACT_NAME_BOOST = 0.03
SCHEMA_FETCH_CONCURRENCY = 4
_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}")


@dataclass(frozen=True)
class RetrievedDoc:
    dataset_id: str
    model_name: str
    doc_type: str
    title: str
    content: str
    referenced_objects: tuple[str, ...]
    score: float
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutedModel:
    model: ModelSummary
    score: float
    evidence: tuple[str, ...]


class SemanticRetriever:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        indexer: SemanticIndexer,
        user_schemas: UserSchemaService,
        settings: Settings,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._indexer = indexer
        self._user_schemas = user_schemas
        self._top_k = settings.retrieval_top_k
        self._candidates = settings.retrieval_candidates

    async def search(
        self,
        authz: AuthorizedContext,
        query: str,
        *,
        model_ids: Sequence[str] | None = None,
        doc_types: Sequence[str] | None = None,
        k: int | None = None,
    ) -> list[RetrievedDoc]:
        models = self._allowed_models(authz, model_ids)
        if not models or not query.strip():
            return []
        by_uuid = {m.model_uuid: m for m in models}
        space = await self._indexer.space()
        query_vector = await self._indexer.embedder.embed_query(query)

        async with self._sessionmaker() as session:
            vector_ids = await self._vector_candidates(
                session, space.id, space.dimensions, list(by_uuid), doc_types, query_vector
            )
            text_ids = await self._text_candidates(
                session, space.id, list(by_uuid), doc_types, query
            )
            scores: dict[uuid.UUID, float] = defaultdict(float)
            for ranked in (vector_ids, text_ids):
                for rank, doc_id in enumerate(ranked):
                    scores[doc_id] += 1 / (RRF_K + rank + 1)
            rows = (
                (
                    await session.scalars(
                        select(SemanticDocument).where(SemanticDocument.id.in_(list(scores)))
                    )
                ).all()
                if scores
                else []
            )

        tokens = {t.lower() for t in _TOKEN.findall(query)}
        candidates = []
        for row in rows:
            score = scores[row.id]
            if row.doc_type == "verified_answer":
                score += VERIFIED_ANSWER_BOOST
            if _object_name(row.title).lower() in tokens:
                score += EXACT_NAME_BOOST
            candidates.append((score, row))
        candidates.sort(key=lambda item: item[0], reverse=True)

        visible = await self._visible_schemas(
            authz,
            {
                by_uuid[row.semantic_model_id].dataset_id
                for _, row in candidates
                if row.referenced_objects
            },
        )
        results: list[RetrievedDoc] = []
        for score, row in candidates:
            model = by_uuid[row.semantic_model_id]
            if row.referenced_objects:
                schema = visible.get(model.dataset_id)
                if schema is None or not set(row.referenced_objects) <= schema.visible_keys:
                    continue
            results.append(
                RetrievedDoc(
                    dataset_id=model.dataset_id,
                    model_name=model.name,
                    doc_type=row.doc_type,
                    title=row.title,
                    content=row.content,
                    referenced_objects=tuple(row.referenced_objects),
                    score=round(score, 6),
                    extra=dict(row.extra or {}),
                )
            )
            if len(results) >= (k or self._top_k):
                break
        return results

    async def route_models(
        self, authz: AuthorizedContext, query: str, *, k: int = 3
    ) -> list[RoutedModel]:
        """Which of the user's allowed models a question is about (cross-model routing).

        Only allowed models can ever be returned. A question about a domain the user can't access
        simply doesn't route there; the agent must then refuse rather than answer partially.
        """
        docs = await self.search(authz, query, k=self._candidates)
        per_model: dict[str, list[RetrievedDoc]] = defaultdict(list)
        for doc in docs:
            per_model[doc.dataset_id].append(doc)
        tokens = {t.lower() for t in _TOKEN.findall(query)}
        routed = []
        for dataset_id, model_docs in per_model.items():
            model = authz.allowed[dataset_id]
            score = sum(d.score for d in sorted(model_docs, key=lambda d: -d.score)[:3])
            names = {model.name.lower(), (model.domain or "").lower()}
            if names & tokens:
                score += EXACT_NAME_BOOST * 2
            routed.append(
                RoutedModel(model, round(score, 6), tuple(d.title for d in model_docs[:3]))
            )
        routed.sort(key=lambda r: r.score, reverse=True)
        return routed[:k]

    # --- internals -----------------------------------------------------------------------------

    def _allowed_models(
        self, authz: AuthorizedContext, model_ids: Sequence[str] | None
    ) -> list[ModelSummary]:
        if model_ids is None:
            return list(authz.allowed.values())
        denied = [m for m in model_ids if not authz.is_allowed(m)]
        if denied:
            raise messages.access_denied(f"Retrieval requested non-allowed models {denied}")
        return [authz.allowed[m] for m in dict.fromkeys(model_ids)]

    async def _vector_candidates(
        self,
        session: AsyncSession,
        space_id: int,
        dims: int,
        model_uuids: list[uuid.UUID],
        doc_types: Sequence[str] | None,
        query_vector: list[float],
    ) -> list[uuid.UUID]:
        type_filter = "AND doc_type = ANY(:types)" if doc_types else ""
        stmt = text(
            "SELECT id FROM semantic_documents "  # noqa: S608 - dims is an int, filters bound
            "WHERE embedding_space_id = :space AND semantic_model_id = ANY(:models) "
            f"{type_filter} "
            f"ORDER BY embedding::vector({int(dims)}) <=> CAST(:query AS vector({int(dims)})) "
            "LIMIT :limit"
        ).bindparams(bindparam("models", type_=ARRAY(UUID(as_uuid=True))))
        params: dict[str, Any] = {
            "space": space_id,
            "models": model_uuids,
            "query": "[" + ",".join(f"{x:.7f}" for x in query_vector) + "]",
            "limit": self._candidates,
        }
        if doc_types:
            params["types"] = list(doc_types)
        return list((await session.execute(stmt, params)).scalars())

    async def _text_candidates(
        self,
        session: AsyncSession,
        space_id: int,
        model_uuids: list[uuid.UUID],
        doc_types: Sequence[str] | None,
        query: str,
    ) -> list[uuid.UUID]:
        terms = list(dict.fromkeys(t.lower() for t in _TOKEN.findall(query)))
        if not terms:
            return []
        type_filter = "AND doc_type = ANY(:types)" if doc_types else ""
        stmt = text(
            "SELECT id FROM semantic_documents, to_tsquery('simple', :tsq) AS q "  # noqa: S608
            "WHERE embedding_space_id = :space AND semantic_model_id = ANY(:models) "
            f"{type_filter} AND search_tsv @@ q "
            "ORDER BY ts_rank(search_tsv, q) DESC LIMIT :limit"
        ).bindparams(bindparam("models", type_=ARRAY(UUID(as_uuid=True))))
        params: dict[str, Any] = {
            "space": space_id,
            "models": model_uuids,
            "tsq": " | ".join(terms),
            "limit": self._candidates,
        }
        if doc_types:
            params["types"] = list(doc_types)
        return list((await session.execute(stmt, params)).scalars())

    async def _visible_schemas(
        self, authz: AuthorizedContext, dataset_ids: set[str]
    ) -> dict[str, NormalizedSchema | None]:
        semaphore = asyncio.Semaphore(SCHEMA_FETCH_CONCURRENCY)

        async def one(dataset_id: str) -> tuple[str, NormalizedSchema | None]:
            async with semaphore:
                return dataset_id, await self._user_schemas.visible(authz, dataset_id)

        return dict(await asyncio.gather(*(one(d) for d in dataset_ids)))


def _object_name(title: str) -> str:
    """'[Total Net Sales]' -> 'Total Net Sales', 'Product[LOB]' -> 'LOB'."""
    if "[" in title and title.endswith("]"):
        return title[title.rindex("[") + 1 : -1]
    return title
