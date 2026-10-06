"""Embedding provider contract.

Concrete providers (local sentence-transformers, OpenAI) implement this in Phase 7.
Switching provider/model changes `dimension`, which requires re-indexing the vector store.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence


class EmbeddingProvider(ABC):
    name: str
    model: str

    @property
    @abstractmethod
    def dimension(self) -> int: ...

    @abstractmethod
    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    @abstractmethod
    async def embed_query(self, text: str) -> list[float]: ...
