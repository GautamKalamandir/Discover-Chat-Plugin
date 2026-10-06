from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthenticatedUser
from app.db.models import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_from_identity(self, identity: AuthenticatedUser) -> User:
        """Create the user on first sight; refresh display fields and last_seen_at afterwards."""
        stmt = (
            insert(User)
            .values(
                entra_tenant_id=identity.tenant_id,
                entra_object_id=identity.object_id,
                username=identity.username,
                display_name=identity.display_name,
            )
            .on_conflict_do_update(
                index_elements=[User.entra_tenant_id, User.entra_object_id],
                set_={
                    "username": identity.username,
                    "display_name": identity.display_name,
                    "last_seen_at": func.now(),
                },
            )
            .returning(User)
        )
        result = await self._session.scalars(stmt, execution_options={"populate_existing": True})
        return result.one()

    async def get_by_identity(self, identity: AuthenticatedUser) -> User | None:
        stmt = select(User).where(
            User.entra_tenant_id == identity.tenant_id,
            User.entra_object_id == identity.object_id,
        )
        return (await self._session.scalars(stmt)).one_or_none()
