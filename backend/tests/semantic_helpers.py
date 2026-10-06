import hashlib
import json
import math
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.models import AuthorizedContext, ModelSummary
from app.db.models import SemanticModel
from app.embeddings.base import EmbeddingProvider
from tests.authz_helpers import request_ctx

FIXTURES = Path(__file__).parent / "fixtures" / "schemas"
_WORD = re.compile(r"[a-z0-9]+")


class HashEmbedder(EmbeddingProvider):
    """Deterministic bag-of-words embeddings: similar wording -> similar vectors. No downloads."""

    name = "test"

    def __init__(self, model: str = "hash-64", dims: int = 64) -> None:
        self.model = model
        self._dims = dims
        self.embedded_texts: list[str] = []

    @property
    def dimension(self) -> int:
        return self._dims

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self._dims
        vector[0] = 0.01  # never a zero vector
        for word in _WORD.findall(text.lower()):
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % self._dims] += 1.0  # noqa: S324
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded_texts.extend(texts)
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


async def authz_for(
    sessionmaker: async_sessionmaker[AsyncSession], oid: str, dataset_ids: list[str]
) -> AuthorizedContext:
    async with sessionmaker() as session:
        models = (
            await session.scalars(
                select(SemanticModel).where(SemanticModel.pbi_dataset_id.in_(dataset_ids))
            )
        ).all()
    allowed = {
        m.pbi_dataset_id: ModelSummary(m.pbi_dataset_id, m.name, m.domain, m.id) for m in models
    }
    return AuthorizedContext(request_ctx(oid), uuid.uuid4(), allowed, datetime.now(UTC))
