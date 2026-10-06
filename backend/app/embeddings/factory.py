from collections.abc import Callable

from app.core.config import EmbeddingProviderName, Settings, get_settings
from app.core.errors import ProviderNotAvailableError
from app.embeddings.base import EmbeddingProvider
from app.embeddings.local import FastEmbedProvider
from app.embeddings.openai import OpenAIEmbeddingProvider

EmbeddingBuilder = Callable[[Settings], EmbeddingProvider]

# Selection is driven only by EMBEDDING_PROVIDER.
_REGISTRY: dict[EmbeddingProviderName, EmbeddingBuilder] = {
    EmbeddingProviderName.LOCAL: FastEmbedProvider,
    EmbeddingProviderName.OPENAI: OpenAIEmbeddingProvider,
}


def register_embedding_provider(name: EmbeddingProviderName, builder: EmbeddingBuilder) -> None:
    _REGISTRY[name] = builder


def create_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    settings = settings or get_settings()
    builder = _REGISTRY.get(settings.embedding_provider)
    if builder is None:
        raise ProviderNotAvailableError(
            "Embedding", settings.embedding_provider, [p.value for p in _REGISTRY]
        )
    return builder(settings)
