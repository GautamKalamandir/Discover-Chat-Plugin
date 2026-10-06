from app.auth.base import AuthProvider
from app.auth.dev import DevAuthProvider
from app.auth.entra import EntraAuthProvider
from app.core.config import AuthProviderName, Settings


def create_auth_provider(settings: Settings) -> AuthProvider:
    """Selected only by AUTH_PROVIDER. Raises ConfigurationError at startup if misconfigured."""
    if settings.auth_provider is AuthProviderName.DEV:
        return DevAuthProvider(settings)
    return EntraAuthProvider(settings)
