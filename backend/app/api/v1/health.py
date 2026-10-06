import logging

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    """Liveness: the process is up. Never touches dependencies."""
    settings = request.app.state.settings
    return {"status": "ok", "service": settings.app_name, "environment": settings.environment}


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict[str, str]:
    """Readiness: dependencies (database) are reachable."""
    engine: AsyncEngine = request.app.state.db_engine
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Readiness check failed: database unreachable")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}
