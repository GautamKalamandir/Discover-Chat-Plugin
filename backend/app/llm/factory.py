from collections.abc import Callable

from app.core.config import LLMProviderName, Settings, get_settings
from app.core.errors import ProviderNotAvailableError
from app.llm.base import LLMProvider

LLMBuilder = Callable[[Settings], LLMProvider]

# Implementations register themselves here (Phase 8). Selection is driven only by LLM_PROVIDER.
_REGISTRY: dict[LLMProviderName, LLMBuilder] = {}


def register_llm_provider(name: LLMProviderName, builder: LLMBuilder) -> None:
    _REGISTRY[name] = builder


def create_llm_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    builder = _REGISTRY.get(settings.llm_provider)
    if builder is None:
        raise ProviderNotAvailableError("LLM", settings.llm_provider, [p.value for p in _REGISTRY])
    return builder(settings)
