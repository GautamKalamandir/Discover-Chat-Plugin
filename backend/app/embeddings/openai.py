"""OpenAI (or OpenAI-compatible) embeddings. ADR 0007."""

from collections.abc import Sequence
from typing import Any

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.embeddings.base import EmbeddingProvider

_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}
_BATCH = 256


class OpenAIEmbeddingProvider(EmbeddingProvider):
    name = "openai"

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.model = settings.openai_embedding_model
        dimension = settings.openai_embedding_dimensions or _DIMENSIONS.get(self.model)
        if dimension is None:
            raise ConfigurationError(
                f"Set OPENAI_EMBEDDING_DIMENSIONS for embedding model {self.model!r}"
            )
        self._dimension = dimension
        if client is None:
            if not settings.openai_api_key:
                raise ConfigurationError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY")
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=settings.openai_api_key.get_secret_value(),
                base_url=settings.openai_base_url or None,
            )
        self._client = client

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            batch = list(texts[start : start + _BATCH])
            response = await self._client.embeddings.create(model=self.model, input=batch)
            ordered = sorted(response.data, key=lambda item: item.index)
            vectors.extend([list(item.embedding) for item in ordered])
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]
