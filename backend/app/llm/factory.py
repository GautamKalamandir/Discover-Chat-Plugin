import logging
from collections.abc import Callable

from app.core.config import Environment, LLMProviderName, Settings, get_settings
from app.core.errors import ConfigurationError, ProviderNotAvailableError
from app.llm.base import LLMProvider, UnconfiguredLLM
from app.llm.openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

LLMBuilder = Callable[[Settings], LLMProvider]


def _groq(settings: Settings) -> LLMProvider:
    if not settings.groq_api_key:
        raise ConfigurationError("LLM_PROVIDER=groq requires GROQ_API_KEY")
    return OpenAICompatibleProvider(
        name="groq",
        model=settings.llm_model,
        api_key=settings.groq_api_key.get_secret_value(),
        base_url=settings.groq_base_url,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


def _openai(settings: Settings) -> LLMProvider:
    if not settings.openai_api_key:
        raise ConfigurationError("LLM_PROVIDER=openai requires OPENAI_API_KEY")
    return OpenAICompatibleProvider(
        name="openai",
        model=settings.llm_model,
        api_key=settings.openai_api_key.get_secret_value(),
        base_url=settings.openai_base_url or None,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


# Selection is driven only by LLM_PROVIDER.
_REGISTRY: dict[LLMProviderName, LLMBuilder] = {
    LLMProviderName.GROQ: _groq,
    LLMProviderName.OPENAI: _openai,
}


def register_llm_provider(name: LLMProviderName, builder: LLMBuilder) -> None:
    _REGISTRY[name] = builder


def create_llm_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    builder = _REGISTRY.get(settings.llm_provider)
    if builder is None:
        raise ProviderNotAvailableError("LLM", settings.llm_provider, [p.value for p in _REGISTRY])
    return builder(settings)


def create_llm_provider_for_app(settings: Settings) -> LLMProvider:
    """Production refuses to start without a working LLM configuration; elsewhere the app still
    starts (health, auth, models) and chat requests explain that the assistant isn't configured."""
    try:
        return create_llm_provider(settings)
    except ConfigurationError as exc:
        if settings.environment is Environment.PROD:
            raise
        logger.warning("LLM not configured (%s); chat will be unavailable", exc)
        return UnconfiguredLLM(str(exc))
