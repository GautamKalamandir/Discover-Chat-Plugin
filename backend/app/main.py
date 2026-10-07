"""Application factory. Run with: uvicorn app.main:create_app --factory"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.orchestrator import Agent
from app.api.v1 import dev as dev_api
from app.api.v1.router import api_router
from app.auth.base import AuthProvider
from app.auth.factory import create_auth_provider
from app.auth.token_broker import TokenBroker, create_token_broker
from app.authz.probe import ModelAccessProbe, create_access_probe
from app.authz.service import AuthorizationService
from app.chat.turns import TurnGuard
from app.core.config import (
    AuthProviderName,
    Environment,
    Settings,
    get_settings,
    production_problems,
)
from app.core.errors import ConfigurationError, register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import (
    CORRELATION_HEADER,
    BodySizeLimitMiddleware,
    CorrelationIdMiddleware,
    SecurityHeadersMiddleware,
)
from app.db.session import create_engine, create_sessionmaker
from app.embeddings.base import EmbeddingProvider
from app.embeddings.factory import create_embedding_provider
from app.llm.base import LLMProvider
from app.llm.factory import create_llm_provider_for_app
from app.powerbi.base import PowerBIGateway
from app.powerbi.factory import create_gateways
from app.powerbi.service import PowerBIService
from app.retention.scheduler import CleanupScheduler
from app.semantic.indexer import SemanticIndexer
from app.semantic.retriever import SemanticRetriever
from app.semantic.schema_source import SchemaSource, create_schema_source
from app.semantic.sync import MetadataSync
from app.semantic.user_schema import UserSchemaService


def create_app(
    settings: Settings | None = None,
    *,
    auth_provider: AuthProvider | None = None,
    token_broker: TokenBroker | None = None,
    access_probe: ModelAccessProbe | None = None,
    gateways: tuple[PowerBIGateway, PowerBIGateway | None] | None = None,
    embedder: EmbeddingProvider | None = None,
    schema_source: SchemaSource | None = None,
    llm: LLMProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    is_prod = settings.environment is Environment.PROD
    if is_prod and (problems := production_problems(settings)):
        raise ConfigurationError("Unsafe production configuration: " + "; ".join(problems))

    # Built eagerly so a misconfigured deployment fails at startup, not on the first request.
    provider = auth_provider or create_auth_provider(settings)
    broker = token_broker or create_token_broker(settings)
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    authz_service = AuthorizationService(
        sessionmaker, access_probe or create_access_probe(settings, broker), settings
    )
    primary, fallback = gateways or create_gateways(settings)
    powerbi_service = PowerBIService(primary, fallback, broker, authz_service, settings)
    # Semantic knowledge (Phase 7). The local embedding model loads on first use, not here.
    indexer = SemanticIndexer(sessionmaker, embedder or create_embedding_provider(settings))
    metadata_sync = MetadataSync(indexer, sessionmaker)
    user_schemas = UserSchemaService(
        schema_source or create_schema_source(settings, powerbi_service), metadata_sync, settings
    )
    retriever = SemanticRetriever(sessionmaker, indexer, user_schemas, settings)
    # Agent (Phase 8). Without an LLM key the app still starts outside production.
    llm_provider = llm or create_llm_provider_for_app(settings)
    agent = Agent(
        llm_provider,
        retriever,
        user_schemas,
        powerbi_service,
        authz_service,
        sessionmaker,
        settings,
    )
    scheduler = (
        CleanupScheduler(sessionmaker, settings) if settings.cleanup_scheduler_enabled else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if scheduler:
            scheduler.start()
        yield
        if scheduler:
            await scheduler.stop()
        await llm_provider.aclose()
        await metadata_sync.aclose()
        await powerbi_service.aclose()
        await authz_service.aclose()
        await provider.aclose()
        await engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        # The API description is not published in production.
        docs_url=None if is_prod else "/docs",
        redoc_url=None if is_prod else "/redoc",
        openapi_url=None if is_prod else "/openapi.json",
    )
    app.state.settings = settings
    app.state.auth_provider = provider
    app.state.token_broker = broker
    app.state.db_engine = engine
    app.state.db_sessionmaker = sessionmaker
    app.state.authz_service = authz_service
    app.state.powerbi_service = powerbi_service
    app.state.metadata_sync = metadata_sync
    app.state.retriever = retriever
    app.state.agent = agent
    app.state.llm = llm_provider
    app.state.turn_guard = TurnGuard(settings)

    register_error_handlers(app)
    app.include_router(api_router)
    if settings.auth_provider is AuthProviderName.DEV and settings.environment in (
        Environment.LOCAL,
        Environment.TEST,
    ):
        # Local development only: lets the dev build of the visual sign in (ADR 0010).
        app.include_router(dev_api.router, prefix="/api/v1")

    # Added innermost → outermost; the correlation id wraps everything so errors carry it.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", CORRELATION_HEADER],
        expose_headers=[CORRELATION_HEADER],
        max_age=600,
    )
    app.add_middleware(
        SecurityHeadersMiddleware, hsts=settings.environment is not Environment.LOCAL
    )
    app.add_middleware(CorrelationIdMiddleware)
    return app
