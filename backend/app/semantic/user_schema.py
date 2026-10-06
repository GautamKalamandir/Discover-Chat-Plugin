"""Each user's own view of a model (OLS applied by Power BI), cached briefly.

This is what retrieval intersects against (scenario 16), and fetching it is also the trigger for
on-use metadata sync (Q10b).
"""

import logging
import time
from collections.abc import Callable

from app.authz.models import AuthorizedContext
from app.core.config import Settings
from app.semantic.normalizer import NormalizedSchema, normalize
from app.semantic.schema_source import SchemaSource
from app.semantic.sync import MetadataSync

logger = logging.getLogger(__name__)

MAX_ENTRIES = 5000


class UserSchemaService:
    def __init__(
        self,
        source: SchemaSource,
        sync: MetadataSync | None,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._sync = sync if settings.metadata_sync_on_use else None
        self._ttl = settings.user_schema_cache_minutes * 60
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, NormalizedSchema]] = {}

    async def visible(self, authz: AuthorizedContext, dataset_id: str) -> NormalizedSchema | None:
        """The user's schema, or None when it can't be obtained (callers must fail closed)."""
        key = (authz.request.user.key, dataset_id)
        cached = self._cache.get(key)
        now = self._clock()
        if cached and cached[0] > now:
            return cached[1]
        try:
            schema = normalize(await self._source.fetch(authz, dataset_id))
        except Exception as exc:
            logger.warning("User schema unavailable for %s: %s", dataset_id, exc)
            return None
        if len(self._cache) >= MAX_ENTRIES:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        self._cache[key] = (now + self._ttl, schema)
        if self._sync is not None and schema.objects:
            self._sync.schedule(dataset_id, schema)
        return schema
