from fastapi import APIRouter
from pydantic import BaseModel

from app.authz.dependencies import AuthorizedDep, AuthorizedModel
from app.authz.models import ModelSummary

router = APIRouter(prefix="/models", tags=["models"])


class ModelOut(BaseModel):
    id: str
    name: str
    domain: str | None

    @classmethod
    def from_summary(cls, model: ModelSummary) -> "ModelOut":
        return cls(id=model.dataset_id, name=model.name, domain=model.domain)


class AccessibleModels(BaseModel):
    models: list[ModelOut]


@router.get("/accessible")
async def list_accessible_models(authz: AuthorizedDep) -> AccessibleModels:
    """Only models the signed-in user may use. Feeds the visual's Format-pane dropdown."""
    return AccessibleModels(models=[ModelOut.from_summary(m) for m in authz.allowed_models])


@router.get("/{model_id}")
async def get_model(model: AuthorizedModel) -> ModelOut:
    return ModelOut.from_summary(model)
