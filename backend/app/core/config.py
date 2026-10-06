"""Application settings.

Every switchable behaviour (auth provider, LLM provider, embedding provider, Power BI query path)
is selected here from environment variables / `.env` only — no code change is needed to switch.
"""

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Power BI client applications that request tokens for custom visuals (commercial cloud).
# https://learn.microsoft.com/en-us/power-bi/developer/visuals/entra-id-authentication
POWERBI_WFE_CLIENT_ID = "871c010f-5e61-4fb1-83ac-98610a7e9110"
POWERBI_DESKTOP_CLIENT_ID = "7f67af8a-fedc-4b08-8b4e-37c4d127b6cf"
POWERBI_MOBILE_CLIENT_ID = "c0d2a505-13b8-4ae0-aa9e-cddd5eab0b12"


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class AuthProviderName(StrEnum):
    ENTRA = "entra"
    DEV = "dev"


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

    # --- HTTP ---
    # Custom visuals run in a sandboxed iframe, so browsers send `Origin: null`. CORS is not the
    # security boundary here: every request must carry a valid bearer token (no cookies).
    cors_allowed_origins: list[str] = Field(default_factory=lambda: ["null"])
    max_request_body_bytes: int = Field(default=64 * 1024, gt=0)

    # --- Database ---
    database_url: str = "postgresql+asyncpg://discover:discover@localhost:5432/discover"

    # --- Authentication ---
    auth_provider: AuthProviderName = AuthProviderName.ENTRA
    jwt_leeway_seconds: int = Field(default=60, ge=0, le=300)

    # Microsoft Entra ID (values come from IT, see docs/entra-setup.md)
    entra_client_id: str | None = None
    entra_app_id_uri: str | None = None
    entra_allowed_tenant_ids: list[str] = Field(default_factory=list)
    entra_required_scope: str | None = None
    entra_allowed_client_app_ids: list[str] = Field(
        default_factory=lambda: [
            POWERBI_WFE_CLIENT_ID,
            POWERBI_DESKTOP_CLIENT_ID,
            POWERBI_MOBILE_CLIENT_ID,
        ]
    )
    entra_authority_host: str = "https://login.microsoftonline.com"
    entra_jwks_url: str = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
    entra_client_secret: SecretStr | None = None
    entra_client_certificate_path: str | None = None
    entra_client_certificate_thumbprint: str | None = None

    # Local development only (AUTH_PROVIDER=dev): HS256 tokens minted by scripts/mint_dev_token.py
    dev_auth_secret: SecretStr | None = None

    # --- Power BI ---
    powerbi_scope: str = "https://analysis.windows.net/powerbi/api/.default"
    powerbi_gateway: PowerBIGatewayName = PowerBIGatewayName.FABRIC_IQ_MCP
    powerbi_fallback_gateway: PowerBIGatewayName | None = PowerBIGatewayName.REST

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
