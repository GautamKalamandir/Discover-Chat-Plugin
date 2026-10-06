from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthenticatedUser:
    """Identity extracted from a validated bearer token. Holds no secrets."""

    object_id: str  # Entra `oid`: stable, per-tenant user identifier
    tenant_id: str  # Entra `tid`
    username: str | None  # `upn` (v1) / `preferred_username` (v2); display only, never a key
    display_name: str | None
    scopes: frozenset[str]
    client_app_id: str | None  # `appid` (v1) / `azp` (v2): the app that requested the token

    @property
    def key(self) -> str:
        """Stable identity key for caches and ownership checks."""
        return f"{self.tenant_id}:{self.object_id}"


@dataclass(frozen=True)
class RequestContext:
    """Server-built context passed to every downstream layer (authz, tools, Power BI)."""

    user: AuthenticatedUser
    correlation_id: str | None
    # The validated inbound token is the user assertion for the On-Behalf-Of exchange.
    # Excluded from repr so it can't leak through logging of the context object.
    access_token: str = field(repr=False)
