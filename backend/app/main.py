"""Application factory. Run with: uvicorn app.main:create_app --factory"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.auth.base import AuthProvider
from app.auth.factory import create_auth_provider
from app.auth.token_broker import TokenBroker, create_token_broker
from app.core.config import Environment, Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import (
    CORRELATION_HEADER,
    BodySizeLimitMiddleware,
    CorrelationIdMiddleware,
    SecurityHeadersMiddleware,
)
from app.db.session import create_engine, create_sessionmaker
from app.retention.scheduler import CleanupScheduler


def create_app(
    settings: Settings | None = None,
    *,
    auth_provider: AuthProvider | None = None,
    token_broker: TokenBroker | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    # Built eagerly so a misconfigured deployment fails at startup, not on the first request.
    provider = auth_provider or create_auth_provider(settings)
    broker = token_broker or create_token_broker(settings)
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
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
        await provider.aclose()
        await engine.dispose()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.auth_provider = provider
    app.state.token_broker = broker
    app.state.db_engine = engine
    app.state.db_sessionmaker = sessionmaker

    register_error_handlers(app)
    app.include_router(api_router)

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
