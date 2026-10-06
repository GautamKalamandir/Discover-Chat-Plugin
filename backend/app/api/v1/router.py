from fastapi import APIRouter

from app.api.v1 import health, models, session

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(session.router)
api_router.include_router(models.router)
