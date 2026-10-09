"""Application settings.

Every switchable behaviour (auth provider, LLM provider, embedding provider, Power BI query path)
is selected here from environment variables / `.env` only — no code change is needed to switch.
"""

import logging
from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

DEFAULT_CONVERSATION_RETENTION_HOURS = 12
DEFAULT_AUDIT_RETENTION_HOURS = 2160  # 90 days
DEFAULT_CLEANUP_INTERVAL_MINUTES = 15
DEFAULT_AUTHZ_ALLOWED_TTL_MINUTES = 10
DEFAULT_AUTHZ_DENIED_TTL_MINUTES = 2
DEFAULT_AUTHZ_PROBE_CONCURRENCY = 8
DEFAULT_AUTHZ_PROBE_TIMEOUT_SECONDS = 10
DEFAULT_POWERBI_QUERY_TIMEOUT_SECONDS = 60
DEFAULT_POWERBI_MAX_RETRIES = 2
DEFAULT_POWERBI_REST_QUERIES_PER_MINUTE = 120  # Microsoft's per-user Execute Queries limit
DEFAULT_POWERBI_DEFAULT_MAX_ROWS = 250
DEFAULT_POWERBI_MAX_ROWS_LIMIT = 1000  # Fabric IQ ExecuteQuery maximum
DEFAULT_RETRIEVAL_TOP_K = 12
DEFAULT_RETRIEVAL_CANDIDATES = 50
DEFAULT_USER_SCHEMA_CACHE_MINUTES = 10
DEFAULT_METADATA_STALE_DAYS = 7
DEFAULT_LLM_TIMEOUT_SECONDS = 60
DEFAULT_LLM_MAX_RETRIES = 2
DEFAULT_AGENT_MAX_QUESTION_CHARS = 2000
DEFAULT_AGENT_HISTORY_TURNS = 6
DEFAULT_AGENT_MAX_QUERY_STEPS = 4
DEFAULT_AGENT_MAX_REPAIRS = 2
DEFAULT_AGENT_RESULT_ROWS_TO_LLM = 50
DEFAULT_AGENT_TABLE_ROWS = 200
DEFAULT_AGENT_CONTEXT_DOCS = 12
# Models with more visible columns than this get a table-selection step before planning.
DEFAULT_AGENT_TABLE_SELECTION_MIN_COLUMNS = 80
DEFAULT_FISCAL_YEAR_START_MONTH = 4  # Q13c: April-March
DEFAULT_CHAT_TURN_TIMEOUT_SECONDS = 120
DEFAULT_CHAT_QUESTIONS_PER_MINUTE = 20
DEFAULT_CHAT_MAX_CONCURRENT_TURNS = 2
DEFAULT_CHAT_HEARTBEAT_SECONDS = 15
_MAX_HOURS_OR_MINUTES = 1_000_000  # guards against overflow from absurd values
_FALLBACKS = {
    "conversation_retention_hours": DEFAULT_CONVERSATION_RETENTION_HOURS,
    "audit_retention_hours": DEFAULT_AUDIT_RETENTION_HOURS,
    "cleanup_interval_minutes": DEFAULT_CLEANUP_INTERVAL_MINUTES,
    "authz_allowed_ttl_minutes": DEFAULT_AUTHZ_ALLOWED_TTL_MINUTES,
    "authz_denied_ttl_minutes": DEFAULT_AUTHZ_DENIED_TTL_MINUTES,
    "authz_probe_concurrency": DEFAULT_AUTHZ_PROBE_CONCURRENCY,
    "authz_probe_timeout_seconds": DEFAULT_AUTHZ_PROBE_TIMEOUT_SECONDS,
    "powerbi_query_timeout_seconds": DEFAULT_POWERBI_QUERY_TIMEOUT_SECONDS,
    "powerbi_max_retries": DEFAULT_POWERBI_MAX_RETRIES,
    "powerbi_rest_queries_per_minute": DEFAULT_POWERBI_REST_QUERIES_PER_MINUTE,
    "powerbi_default_max_rows": DEFAULT_POWERBI_DEFAULT_MAX_ROWS,
    "powerbi_max_rows_limit": DEFAULT_POWERBI_MAX_ROWS_LIMIT,
    "retrieval_top_k": DEFAULT_RETRIEVAL_TOP_K,
    "retrieval_candidates": DEFAULT_RETRIEVAL_CANDIDATES,
    "user_schema_cache_minutes": DEFAULT_USER_SCHEMA_CACHE_MINUTES,
    "metadata_stale_days": DEFAULT_METADATA_STALE_DAYS,
    "llm_timeout_seconds": DEFAULT_LLM_TIMEOUT_SECONDS,
    "llm_max_retries": DEFAULT_LLM_MAX_RETRIES,
    "agent_max_question_chars": DEFAULT_AGENT_MAX_QUESTION_CHARS,
    "agent_history_turns": DEFAULT_AGENT_HISTORY_TURNS,
    "agent_max_query_steps": DEFAULT_AGENT_MAX_QUERY_STEPS,
    "agent_max_repairs": DEFAULT_AGENT_MAX_REPAIRS,
    "agent_result_rows_to_llm": DEFAULT_AGENT_RESULT_ROWS_TO_LLM,
    "agent_table_rows": DEFAULT_AGENT_TABLE_ROWS,
    "agent_context_docs": DEFAULT_AGENT_CONTEXT_DOCS,
    "chat_turn_timeout_seconds": DEFAULT_CHAT_TURN_TIMEOUT_SECONDS,
    "chat_questions_per_minute": DEFAULT_CHAT_QUESTIONS_PER_MINUTE,
    "chat_max_concurrent_turns": DEFAULT_CHAT_MAX_CONCURRENT_TURNS,
    "chat_heartbeat_seconds": DEFAULT_CHAT_HEARTBEAT_SECONDS,
}

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
    DEV_SYNTHETIC = "dev_synthetic"  # ENVIRONMENT=local/test only: fake rows labelled DEV DATA


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

    # --- Retention (ADR 0004). Missing/invalid values fall back to the defaults below. ---
    # Conversations are deleted after this many hours without activity.
    conversation_retention_hours: int = DEFAULT_CONVERSATION_RETENTION_HOURS
    # Security audit events are deleted after this many hours.
    audit_retention_hours: int = DEFAULT_AUDIT_RETENTION_HOURS
    # Built-in cleanup scheduler; the same job is also runnable as `python -m app.jobs.cleanup`.
    cleanup_scheduler_enabled: bool = True
    cleanup_interval_minutes: int = DEFAULT_CLEANUP_INTERVAL_MINUTES

    # --- Authorization (ADR 0005). Missing/invalid values fall back to the defaults below. ---
    authz_allowed_ttl_minutes: int = DEFAULT_AUTHZ_ALLOWED_TTL_MINUTES
    authz_denied_ttl_minutes: int = DEFAULT_AUTHZ_DENIED_TTL_MINUTES
    authz_probe_concurrency: int = DEFAULT_AUTHZ_PROBE_CONCURRENCY
    authz_probe_timeout_seconds: int = DEFAULT_AUTHZ_PROBE_TIMEOUT_SECONDS
    # AUTH_PROVIDER=dev only: {"<object id>": ["<dataset id>", ...]} — who may access what locally.
    dev_model_access: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator(
        "conversation_retention_hours",
        "audit_retention_hours",
        "cleanup_interval_minutes",
        "authz_allowed_ttl_minutes",
        "authz_denied_ttl_minutes",
        "authz_probe_concurrency",
        "authz_probe_timeout_seconds",
        "powerbi_query_timeout_seconds",
        "powerbi_rest_queries_per_minute",
        "powerbi_default_max_rows",
        "powerbi_max_rows_limit",
        "retrieval_top_k",
        "retrieval_candidates",
        "user_schema_cache_minutes",
        "metadata_stale_days",
        "llm_timeout_seconds",
        "agent_max_question_chars",
        "agent_history_turns",
        "agent_max_query_steps",
        "agent_result_rows_to_llm",
        "agent_table_rows",
        "agent_context_docs",
        "chat_turn_timeout_seconds",
        "chat_questions_per_minute",
        "chat_max_concurrent_turns",
        "chat_heartbeat_seconds",
        mode="before",
    )
    @classmethod
    def _positive_int_or_default(cls, value: object, info: ValidationInfo) -> int:
        default = _FALLBACKS[info.field_name or ""]
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            number = 0
        if 0 < number <= _MAX_HOURS_OR_MINUTES:
            return number
        if value not in (None, ""):
            logger.warning(
                "%s=%r is not a positive whole number; using fallback %d",
                (info.field_name or "").upper(),
                value,
                default,
            )
        return default

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
    powerbi_api_base_url: str = "https://api.powerbi.com/v1.0/myorg"
    powerbi_gateway: PowerBIGatewayName = PowerBIGatewayName.FABRIC_IQ_MCP
    powerbi_fallback_gateway: PowerBIGatewayName | None = PowerBIGatewayName.REST
    # Fabric IQ MCP (primary). The variant header pins the tool contract version.
    fabric_iq_mcp_url: str = "https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq"
    fabric_iq_tool_variant: str = "Fabric.Routing.FabricIQ.V1"
    # Advertised by the endpoint's OAuth protected-resource metadata (checked 2026-10-06).
    fabric_iq_token_scope: str = "https://api.fabric.microsoft.com/.default"  # noqa: S105 - scope
    # Query execution (ADR 0006). Missing/invalid values fall back to the defaults.
    powerbi_query_timeout_seconds: int = DEFAULT_POWERBI_QUERY_TIMEOUT_SECONDS
    powerbi_max_retries: int = DEFAULT_POWERBI_MAX_RETRIES
    powerbi_rest_queries_per_minute: int = DEFAULT_POWERBI_REST_QUERIES_PER_MINUTE
    powerbi_default_max_rows: int = DEFAULT_POWERBI_DEFAULT_MAX_ROWS
    powerbi_max_rows_limit: int = DEFAULT_POWERBI_MAX_ROWS_LIMIT

    # --- LLM (provider/factory, switched via .env; Q13b) ---
    llm_provider: LLMProviderName = LLMProviderName.GROQ
    llm_model: str = "openai/gpt-oss-120b"
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_output_tokens: int = Field(default=4096, gt=0)
    llm_timeout_seconds: int = DEFAULT_LLM_TIMEOUT_SECONDS
    llm_max_retries: int = DEFAULT_LLM_MAX_RETRIES
    groq_api_key: SecretStr | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None

    # --- Agent (ADR 0008). Missing/invalid values fall back to the defaults. ---
    agent_max_question_chars: int = DEFAULT_AGENT_MAX_QUESTION_CHARS
    agent_history_turns: int = DEFAULT_AGENT_HISTORY_TURNS
    agent_max_query_steps: int = DEFAULT_AGENT_MAX_QUERY_STEPS
    agent_max_repairs: int = DEFAULT_AGENT_MAX_REPAIRS
    agent_result_rows_to_llm: int = DEFAULT_AGENT_RESULT_ROWS_TO_LLM
    agent_table_rows: int = DEFAULT_AGENT_TABLE_ROWS
    agent_context_docs: int = DEFAULT_AGENT_CONTEXT_DOCS
    agent_table_selection_min_columns: int = DEFAULT_AGENT_TABLE_SELECTION_MIN_COLUMNS
    fiscal_year_start_month: int = DEFAULT_FISCAL_YEAR_START_MONTH

    @field_validator("agent_table_selection_min_columns", mode="before")
    @classmethod
    def _column_threshold_or_default(cls, value: object) -> int:
        # 0 = always select tables first; missing/invalid falls back to the default.
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return DEFAULT_AGENT_TABLE_SELECTION_MIN_COLUMNS
        return number if 0 <= number <= 100_000 else DEFAULT_AGENT_TABLE_SELECTION_MIN_COLUMNS

    # --- Phase 2 spikes: live diagnostics (never in production; docs/spikes-runbook.md) ---
    diagnostics_enabled: bool = False
    diagnostics_capture_dir: str = "spikes/captures"

    # --- Chat API (ADR 0009). Missing/invalid values fall back to the defaults. ---
    chat_turn_timeout_seconds: int = DEFAULT_CHAT_TURN_TIMEOUT_SECONDS
    chat_questions_per_minute: int = DEFAULT_CHAT_QUESTIONS_PER_MINUTE
    chat_max_concurrent_turns: int = DEFAULT_CHAT_MAX_CONCURRENT_TURNS
    chat_heartbeat_seconds: int = DEFAULT_CHAT_HEARTBEAT_SECONDS

    @field_validator("powerbi_max_retries", "llm_max_retries", "agent_max_repairs", mode="before")
    @classmethod
    def _non_negative_int_or_default(cls, value: object, info: ValidationInfo) -> int:
        # Retry/repair counts may be 0 ("don't retry").
        default = _FALLBACKS[info.field_name or ""]
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            number = -1
        if 0 <= number <= 10:
            return number
        if value not in (None, ""):
            logger.warning(
                "%s=%r is not a whole number 0-10; using fallback %d",
                (info.field_name or "").upper(),
                value,
                default,
            )
        return default

    @field_validator("fiscal_year_start_month", mode="before")
    @classmethod
    def _month_or_default(cls, value: object) -> int:
        try:
            month = int(str(value).strip())
        except (TypeError, ValueError):
            month = 0
        if 1 <= month <= 12:
            return month
        if value not in (None, ""):
            logger.warning(
                "FISCAL_YEAR_START_MONTH=%r is not 1-12; using fallback %d",
                value,
                DEFAULT_FISCAL_YEAR_START_MONTH,
            )
        return DEFAULT_FISCAL_YEAR_START_MONTH

    # --- Embeddings (provider/factory, switched via .env; ADR 0007) ---
    embedding_provider: EmbeddingProviderName = EmbeddingProviderName.LOCAL
    # Local (fastembed) model; downloaded once into embedding_cache_dir.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = ".cache/embeddings"
    openai_embedding_model: str = "text-embedding-3-small"
    # Only needed for OpenAI-compatible models not in the built-in dimension table.
    openai_embedding_dimensions: int | None = None

    @field_validator("openai_embedding_dimensions", mode="before")
    @classmethod
    def _optional_int(cls, value: object) -> object:
        # "OPENAI_EMBEDDING_DIMENSIONS=" (left empty in .env) means "not set".
        return None if isinstance(value, str) and not value.strip() else value

    # --- Semantic knowledge / retrieval (ADR 0007) ---
    retrieval_top_k: int = DEFAULT_RETRIEVAL_TOP_K
    retrieval_candidates: int = DEFAULT_RETRIEVAL_CANDIDATES
    user_schema_cache_minutes: int = DEFAULT_USER_SCHEMA_CACHE_MINUTES
    metadata_sync_on_use: bool = True
    metadata_stale_days: int = DEFAULT_METADATA_STALE_DAYS
    # AUTH_PROVIDER=dev only: folder with <dataset id>.json schema payloads (stand-in for Power BI).
    dev_schema_fixture_dir: str = "dev-fixtures/schemas"
    # Optional public-client app registration for the admin sync CLI (docs/entra-setup.md §4).
    admin_cli_client_id: str | None = None


def production_problems(settings: Settings) -> list[str]:
    """Settings that must never reach ENVIRONMENT=prod (ADR 0011). Empty list = fine."""
    problems = []
    if settings.auth_provider is not AuthProviderName.ENTRA:
        problems.append("AUTH_PROVIDER must be entra")
    gateways = {settings.powerbi_gateway, settings.powerbi_fallback_gateway}
    if PowerBIGatewayName.DEV_SYNTHETIC in gateways:
        problems.append("POWERBI_GATEWAY / POWERBI_FALLBACK_GATEWAY must not be dev_synthetic")
    if "*" in settings.cors_allowed_origins:
        problems.append("CORS_ALLOWED_ORIGINS must not contain *")
    if not settings.entra_allowed_tenant_ids:
        problems.append("ENTRA_ALLOWED_TENANT_IDS must not be empty")
    if settings.dev_auth_secret is not None or settings.dev_model_access:
        problems.append("DEV_AUTH_SECRET / DEV_MODEL_ACCESS must not be set")
    if settings.diagnostics_enabled:
        problems.append("DIAGNOSTICS_ENABLED must be false")
    if settings.log_level.upper() == "DEBUG":
        problems.append("LOG_LEVEL must not be DEBUG")
    return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
