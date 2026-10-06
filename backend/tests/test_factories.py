import pytest

from app.core.config import (
    EmbeddingProviderName,
    LLMProviderName,
    PowerBIGatewayName,
    Settings,
)
from app.core.errors import ProviderNotAvailableError
from app.embeddings.factory import create_embedding_provider
from app.llm.factory import create_llm_provider
from app.powerbi.factory import create_powerbi_gateway


@pytest.mark.parametrize("provider", list(LLMProviderName))
def test_llm_factory_reports_unregistered_provider(provider: LLMProviderName) -> None:
    with pytest.raises(ProviderNotAvailableError, match=provider.value):
        create_llm_provider(Settings(llm_provider=provider))


@pytest.mark.parametrize("provider", list(EmbeddingProviderName))
def test_embedding_factory_reports_unregistered_provider(provider: EmbeddingProviderName) -> None:
    with pytest.raises(ProviderNotAvailableError, match=provider.value):
        create_embedding_provider(Settings(embedding_provider=provider))


@pytest.mark.parametrize("gateway", list(PowerBIGatewayName))
def test_powerbi_factory_reports_unregistered_gateway(gateway: PowerBIGatewayName) -> None:
    with pytest.raises(ProviderNotAvailableError, match=gateway.value):
        create_powerbi_gateway(Settings(powerbi_gateway=gateway))


def test_providers_are_selected_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("POWERBI_GATEWAY", "rest")

    settings = Settings(_env_file=None)

    assert settings.llm_provider is LLMProviderName.OPENAI
    assert settings.embedding_provider is EmbeddingProviderName.OPENAI
    assert settings.powerbi_gateway is PowerBIGatewayName.REST
