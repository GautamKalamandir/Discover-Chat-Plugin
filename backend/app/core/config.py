"""Application settings.

Every switchable behaviour (LLM provider, embedding provider, Power BI query path) is selected
here from environment variables / `.env` only — no code change is needed to switch providers.
"""

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class LLMProviderName(StrEnum):
    GROQ = "groq"
    OPENAI = "openai"


class EmbeddingProviderName(StrEnum):
    LOCAL = "local"
    OPENAI = "openai"


class PowerBIGatewayName(StrEnum):
    FABRIC_IQ_MCP = "fabric_iq_mcp"
    REST = "rest"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # backend/.env wins over the repo-root .env when both exist.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ---
    app_name: str = "Discover Chat Bot API"
    environment: Environment = Environment.LOCAL
    log_level: str = "INFO"
    log_json: bool = False

    # --- Database ---
    database_url: str = "postgresql+asyncpg://discover:discover@localhost:5432/discover"

    # --- LLM (provider/factory, switched via .env) ---
    llm_provider: LLMProviderName = LLMProviderName.GROQ
    llm_model: str = "llama-3.3-70b-versatile"
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_output_tokens: int = Field(default=2048, gt=0)
    groq_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None

    # --- Embeddings (provider/factory, switched via .env) ---
    embedding_provider: EmbeddingProviderName = EmbeddingProviderName.LOCAL
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    # --- Power BI query path ---
    powerbi_gateway: PowerBIGatewayName = PowerBIGatewayName.FABRIC_IQ_MCP
    powerbi_fallback_gateway: PowerBIGatewayName | None = PowerBIGatewayName.REST

    # --- Microsoft Entra ID (filled in Phase 0/3) ---
    entra_client_id: str | None = None
    entra_client_secret: SecretStr | None = None
    entra_app_id_uri: str | None = None
    entra_allowed_tenant_ids: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()
