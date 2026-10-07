"""Authorization service: which semantic models may this user use? (gates G1 and G2, ADR 0005)

Power BI is the source of truth; `user_model_access` is a shared cache in front of it.
Cache and audit writes commit in their own short transactions, so a denial that aborts the
request still leaves its audit record and refreshed cache behind.
"""

import logging
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import RequestContext
from app.authz import messages
from app.authz.models import AuthorizedContext, ModelSummary, ProbeOutcome
from app.authz.probe import ModelAccessProbe
from app.core.config import Settings
from app.db.models import AccessStatus, AuditOutcome, SemanticModel
from app.db.repositories.audit import AuditRepository
from app.db.repositories.registry import RegistryRepository
from app.db.repositories.users import UserRepository

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def safe_ids(ids: Iterable[str]) -> list[str]:
    """Model ids can come from LLM output; anything not shaped like an id is not logged (Q18)."""
    return [i if _ID.fullmatch(i) else "<invalid-id>" for i in ids]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _summary(model: SemanticModel) -> ModelSummary:
    return ModelSummary(
        dataset_id=model.pbi_dataset_id, name=model.name, domain=model.domain, model_uuid=model.id
    )


class AuthorizationService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        probe: ModelAccessProbe,
        settings: Settings,
        *,
        clock: Clock = _utcnow,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._probe = probe
        self._allowed_ttl = timedelta(minutes=settings.authz_allowed_ttl_minutes)
        self._denied_ttl = timedelta(minutes=settings.authz_denied_ttl_minutes)
        self._clock = clock

    # --- G1: resolve the allowed model set ----------------------------------------------------

    async def build_context(self, ctx: RequestContext) -> AuthorizedContext:
        now = self._clock()
        async with self._sessionmaker() as session, session.begin():
            user = await UserRepository(session).upsert_from_identity(ctx.user)
            registry = RegistryRepository(session)
            candidates = list(await registry.list_chatbot_models())
            cached = await registry.get_access(user.id, [m.id for m in candidates])

        allowed: dict[str, ModelSummary] = {}
        stale: list[SemanticModel] = []
        for model in candidates:
            row = cached.get(model.id)
            if row is not None and row.expires_at > now:
                if row.status is AccessStatus.ALLOWED:
                    allowed[model.pbi_dataset_id] = _summary(model)
            else:
                stale.append(model)

        outcomes = await self._probe_and_cache(ctx, user.id, stale, now)
        for model in stale:
            if outcomes.get(model.pbi_dataset_id) is ProbeOutcome.ALLOWED:
                allowed[model.pbi_dataset_id] = _summary(model)

        return AuthorizedContext(
            request=ctx,
            user_id=user.id,
            allowed=allowed,
            resolved_at=now,
            unverified=frozenset(d for d, o in outcomes.items() if o is ProbeOutcome.UNKNOWN),
            verified_live=frozenset(outcomes),
        )

    # --- G2: explicit check for the models a request needs -------------------------------------

    async def assert_allowed(
        self,
        authz: AuthorizedContext,
        dataset_ids: Sequence[str],
        *,
        session_id: uuid.UUID | None = None,
    ) -> AuthorizedContext:
        """All requested models must be allowed, or the whole request is denied (generic message).

        A model denied only by cache is re-checked live first, so access granted moments ago
        works immediately. Returns the (possibly extended) context to use from here on.
        """
        requested = list(dict.fromkeys(dataset_ids))
        missing = [d for d in requested if not authz.is_allowed(d)]
        if missing:
            authz = await self._recheck_live(authz, missing)
            missing = [d for d in requested if not authz.is_allowed(d)]

        if missing:
            unverified = [d for d in missing if d in authz.unverified]
            await self._audit(
                authz,
                AuditOutcome.DENY,
                requested,
                session_id,
                reason="access_check_unavailable" if unverified else "model_not_allowed",
                denied=missing,
            )
            if unverified:
                raise messages.access_check_unavailable(
                    f"Unverified models: {safe_ids(unverified)}"
                )
            raise messages.access_denied(f"Denied models: {safe_ids(missing)}")

        await self._audit(authz, AuditOutcome.ALLOW, requested, session_id, reason="allowed")
        return authz

    # --- G4 feedback: Power BI refused a real query ---------------------------------------------

    async def on_power_bi_denied(self, authz: AuthorizedContext, dataset_id: str) -> None:
        """Called when Power BI rejects a query with 401/403: access was revoked."""
        model = authz.allowed.get(dataset_id)
        now = self._clock()
        async with self._sessionmaker() as session, session.begin():
            if model is not None:
                await RegistryRepository(session).put_access(
                    user_id=authz.user_id,
                    semantic_model_id=model.model_uuid,
                    status=AccessStatus.DENIED,
                    source="powerbi_query",
                    verified_at=now,
                    expires_at=now + self._denied_ttl,
                )
            await AuditRepository(session).record(
                "authz.revoked",
                AuditOutcome.DENY,
                user=authz.request.user,
                pbi_dataset_id=dataset_id,
                correlation_id=authz.request.correlation_id,
                reason="power_bi_rejected_query",
            )

    async def verify_live(self, authz: AuthorizedContext, dataset_id: str) -> bool:
        """Asks Power BI right now whether the user can still access `dataset_id` (cache updated).

        Used when a query is refused, to tell "access revoked" from "missing Build permission".
        Raises 503 if Power BI can't answer.
        """
        async with self._sessionmaker() as session:
            enabled = {
                m.pbi_dataset_id: m for m in await RegistryRepository(session).list_chatbot_models()
            }
        model = enabled.get(dataset_id)
        if model is None:
            return False
        outcome = (
            await self._probe_and_cache(authz.request, authz.user_id, [model], self._clock())
        )[dataset_id]
        if outcome is ProbeOutcome.UNKNOWN:
            raise messages.access_check_unavailable(f"Live re-check failed for {dataset_id}")
        return outcome is ProbeOutcome.ALLOWED

    # --- internals -----------------------------------------------------------------------------

    async def _recheck_live(
        self, authz: AuthorizedContext, dataset_ids: Iterable[str]
    ) -> AuthorizedContext:
        # Only registered + enabled models can ever be allowed; unknown ids are never probed.
        to_check = [d for d in dataset_ids if d not in authz.verified_live]
        if not to_check:
            return authz
        now = self._clock()
        async with self._sessionmaker() as session:
            registry = RegistryRepository(session)
            enabled = {m.pbi_dataset_id: m for m in await registry.list_chatbot_models()}
        models = [enabled[d] for d in to_check if d in enabled]
        outcomes = await self._probe_and_cache(authz.request, authz.user_id, models, now)
        granted = [
            _summary(m) for m in models if outcomes[m.pbi_dataset_id] is ProbeOutcome.ALLOWED
        ]
        return authz.with_allowed(
            granted,
            verified_live=outcomes,
            unverified=[d for d, o in outcomes.items() if o is ProbeOutcome.UNKNOWN],
        )

    async def _probe_and_cache(
        self,
        ctx: RequestContext,
        user_id: uuid.UUID,
        models: Sequence[SemanticModel],
        now: datetime,
    ) -> dict[str, ProbeOutcome]:
        if not models:
            return {}
        outcomes = await self._probe.check(ctx, [m.pbi_dataset_id for m in models])
        async with self._sessionmaker() as session, session.begin():
            registry = RegistryRepository(session)
            for model in models:
                outcome = outcomes.get(model.pbi_dataset_id, ProbeOutcome.UNKNOWN)
                if outcome is ProbeOutcome.UNKNOWN:
                    continue  # fail closed for this request; don't cache a non-answer
                allowed = outcome is ProbeOutcome.ALLOWED
                await registry.put_access(
                    user_id=user_id,
                    semantic_model_id=model.id,
                    status=AccessStatus.ALLOWED if allowed else AccessStatus.DENIED,
                    source=self._probe.source,
                    verified_at=now,
                    expires_at=now + (self._allowed_ttl if allowed else self._denied_ttl),
                )
        return {
            m.pbi_dataset_id: outcomes.get(m.pbi_dataset_id, ProbeOutcome.UNKNOWN) for m in models
        }

    async def _audit(
        self,
        authz: AuthorizedContext,
        outcome: AuditOutcome,
        requested: Sequence[str],
        session_id: uuid.UUID | None,
        *,
        reason: str,
        denied: Sequence[str] = (),
    ) -> None:
        details: dict[str, object] = {"model_ids": safe_ids(requested)}
        if denied:
            details["denied_model_ids"] = safe_ids(denied)
        async with self._sessionmaker() as session, session.begin():
            await AuditRepository(session).record(
                "authz.decision",
                outcome,
                user=authz.request.user,
                pbi_dataset_id=safe_ids(requested)[0] if len(requested) == 1 else None,
                session_id=session_id,
                correlation_id=authz.request.correlation_id,
                reason=reason,
                details=details,
            )

    async def aclose(self) -> None:
        await self._probe.aclose()
