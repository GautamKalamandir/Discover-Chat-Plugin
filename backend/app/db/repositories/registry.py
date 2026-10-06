"""Power BI registry (workspaces, semantic models, reports) and the authorization cache rows."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AccessStatus,
    ModelStatus,
    Report,
    SemanticModel,
    UserModelAccess,
    Workspace,
)


class RegistryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_workspace(
        self, *, pbi_workspace_id: str, entra_tenant_id: str, name: str
    ) -> Workspace:
        stmt = (
            insert(Workspace)
            .values(pbi_workspace_id=pbi_workspace_id, entra_tenant_id=entra_tenant_id, name=name)
            .on_conflict_do_update(
                index_elements=[Workspace.pbi_workspace_id],
                set_={"name": name, "entra_tenant_id": entra_tenant_id},
            )
            .returning(Workspace)
        )
        result = await self._session.scalars(stmt, execution_options={"populate_existing": True})
        return result.one()

    async def upsert_semantic_model(
        self,
        *,
        pbi_dataset_id: str,
        workspace: Workspace,
        name: str,
        domain: str | None = None,
        description: str | None = None,
        chatbot_enabled: bool = False,
    ) -> SemanticModel:
        values = {
            "workspace_id": workspace.id,
            "name": name,
            "domain": domain,
            "description": description,
            "chatbot_enabled": chatbot_enabled,
        }
        stmt = (
            insert(SemanticModel)
            .values(pbi_dataset_id=pbi_dataset_id, **values)
            .on_conflict_do_update(index_elements=[SemanticModel.pbi_dataset_id], set_=values)
            .returning(SemanticModel)
        )
        result = await self._session.scalars(stmt, execution_options={"populate_existing": True})
        return result.one()

    async def upsert_report(
        self, *, pbi_report_id: str, workspace: Workspace, model: SemanticModel, name: str
    ) -> Report:
        values = {"workspace_id": workspace.id, "semantic_model_id": model.id, "name": name}
        stmt = (
            insert(Report)
            .values(pbi_report_id=pbi_report_id, **values)
            .on_conflict_do_update(index_elements=[Report.pbi_report_id], set_=values)
            .returning(Report)
        )
        result = await self._session.scalars(stmt, execution_options={"populate_existing": True})
        return result.one()

    async def get_model_by_pbi_id(self, pbi_dataset_id: str) -> SemanticModel | None:
        stmt = select(SemanticModel).where(SemanticModel.pbi_dataset_id == pbi_dataset_id)
        return (await self._session.scalars(stmt)).one_or_none()

    async def set_model_enabled(self, pbi_dataset_id: str, enabled: bool) -> bool:
        model = await self.get_model_by_pbi_id(pbi_dataset_id)
        if model is None:
            return False
        model.chatbot_enabled = enabled
        await self._session.flush()
        return True

    async def list_all_models(self) -> Sequence[SemanticModel]:
        stmt = select(SemanticModel).order_by(SemanticModel.name)
        return (await self._session.scalars(stmt)).all()

    async def list_chatbot_models(self) -> Sequence[SemanticModel]:
        """Candidate models for authorization: enabled for the chatbot and active."""
        stmt = (
            select(SemanticModel)
            .where(
                SemanticModel.chatbot_enabled.is_(True),
                SemanticModel.status == ModelStatus.ACTIVE,
            )
            .order_by(SemanticModel.name)
        )
        return (await self._session.scalars(stmt)).all()

    # --- authorization cache (policy lives in Phase 5) ---

    async def get_access(
        self, user_id: uuid.UUID, model_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, UserModelAccess]:
        if not model_ids:
            return {}
        stmt = select(UserModelAccess).where(
            UserModelAccess.user_id == user_id,
            UserModelAccess.semantic_model_id.in_(model_ids),
        )
        return {row.semantic_model_id: row for row in (await self._session.scalars(stmt)).all()}

    async def put_access(
        self,
        *,
        user_id: uuid.UUID,
        semantic_model_id: uuid.UUID,
        status: AccessStatus,
        source: str,
        verified_at: datetime,
        expires_at: datetime,
        capability: str | None = None,
    ) -> None:
        values = {
            "status": status,
            "capability": capability,
            "source": source,
            "last_verified_at": verified_at,
            "expires_at": expires_at,
        }
        stmt = (
            insert(UserModelAccess)
            .values(user_id=user_id, semantic_model_id=semantic_model_id, **values)
            .on_conflict_do_update(
                index_elements=[UserModelAccess.user_id, UserModelAccess.semantic_model_id],
                set_=values,
            )
        )
        await self._session.execute(stmt)
