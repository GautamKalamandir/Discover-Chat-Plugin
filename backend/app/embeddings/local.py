"""Local open-source embeddings via fastembed (ONNX runtime, no PyTorch). ADR 0007 / Q10c."""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.embeddings.base import EmbeddingProvider


def _load_fastembed(model: str, cache_dir: str) -> Any:
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model, cache_dir=cache_dir)


class FastEmbedProvider(EmbeddingProvider):
    name = "local"

    def __init__(self, settings: Settings, loader: Callable[[str, str], Any] | None = None) -> None:
        self.model = settings.embedding_model
        self._cache_dir = settings.embedding_cache_dir
        self._loader = loader or _load_fastembed
        self._engine: Any = None
        self._dimension = _known_dimension(self.model)
        self._lock = asyncio.Lock()

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        engine = await self._get_engine()
        return await asyncio.to_thread(lambda: _to_lists(engine.passage_embed(list(texts))))

    async def embed_query(self, text: str) -> list[float]:
        engine = await self._get_engine()
        return (await asyncio.to_thread(lambda: _to_lists(engine.query_embed(text))))[0]

    async def _get_engine(self) -> Any:
        # Loaded on first use: the first load downloads the model (~70 MB for bge-small).
        async with self._lock:
            if self._engine is None:
                self._engine = await asyncio.to_thread(self._loader, self.model, self._cache_dir)
        return self._engine


def _known_dimension(model: str) -> int:
    from fastembed import TextEmbedding

    for info in TextEmbedding.list_supported_models():
        if info["model"].lower() == model.lower():
            return int(info["dim"])
    raise ConfigurationError(f"EMBEDDING_MODEL={model!r} is not a fastembed-supported model")


def _to_lists(vectors: Any) -> list[list[float]]:
    return [[float(x) for x in vector] for vector in vectors]
