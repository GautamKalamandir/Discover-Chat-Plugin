import pytest

from app.core.config import (
    EmbeddingProviderName,
    LLMProviderName,
    PowerBIGatewayName,
    Settings,
)
from app.core.errors import ConfigurationError
from app.embeddings.factory import create_embedding_provider
from app.llm.base import UnconfiguredLLM
from app.llm.factory import create_llm_provider, create_llm_provider_for_app
from app.powerbi.factory import create_gateways, create_powerbi_gateway


@pytest.mark.parametrize(
    ("provider", "key_setting", "key_env"),
    [("groq", "groq_api_key", "GROQ_API_KEY"), ("openai", "openai_api_key", "OPENAI_API_KEY")],
)
def test_llm_provider_is_selected_from_env_and_needs_its_key(
    provider: str, key_setting: str, key_env: str
) -> None:
    with pytest.raises(ConfigurationError, match=key_env):
        create_llm_provider(Settings(_env_file=None, llm_provider=provider))

    settings = Settings.model_validate({"llm_provider": provider, key_setting: "k"})
    llm = create_llm_provider(settings)

    assert (llm.name, llm.model) == (provider, "openai/gpt-oss-120b")


def test_missing_llm_key_still_starts_outside_production() -> None:
    llm = create_llm_provider_for_app(Settings(_env_file=None, environment="local"))

    assert isinstance(llm, UnconfiguredLLM)


def test_missing_llm_key_refuses_to_start_in_production() -> None:
    with pytest.raises(ConfigurationError):
        create_llm_provider_for_app(Settings(_env_file=None, environment="prod"))


def test_local_embedding_provider_is_fastembed_bge_small() -> None:
    provider = create_embedding_provider(Settings(_env_file=None, embedding_provider="local"))

    assert (provider.name, provider.model, provider.dimension) == (
        "local",
        "BAAI/bge-small-en-v1.5",
        384,
    )


def test_openai_embedding_provider_needs_an_api_key() -> None:
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        create_embedding_provider(Settings(_env_file=None, embedding_provider="openai"))


def test_openai_embedding_dimensions_come_from_the_model() -> None:
    provider = create_embedding_provider(
        Settings(_env_file=None, embedding_provider="openai", openai_api_key="sk-test")
    )

    assert (provider.model, provider.dimension) == ("text-embedding-3-small", 1536)


def test_unknown_local_model_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="fastembed"):
        create_embedding_provider(Settings(_env_file=None, embedding_model="no/such-model"))


@pytest.mark.parametrize("gateway", list(PowerBIGatewayName))
def test_powerbi_factory_builds_every_configured_gateway(gateway: PowerBIGatewayName) -> None:
    assert (
        create_powerbi_gateway(Settings(_env_file=None, powerbi_gateway=gateway)).name
        == gateway.value
    )


def test_fallback_equal_to_primary_is_ignored() -> None:
    settings = Settings(_env_file=None, powerbi_gateway="rest", powerbi_fallback_gateway="rest")

    primary, fallback = create_gateways(settings)

    assert (primary.name, fallback) == ("rest", None)


def test_providers_are_selected_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("POWERBI_GATEWAY", "rest")

    settings = Settings(_env_file=None)

    assert settings.llm_provider is LLMProviderName.OPENAI
    assert settings.embedding_provider is EmbeddingProviderName.OPENAI
    assert settings.powerbi_gateway is PowerBIGatewayName.REST
