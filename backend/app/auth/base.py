from abc import ABC, abstractmethod

from app.auth.models import AuthenticatedUser


class AuthProvider(ABC):
    """Validates an inbound bearer token and returns the user it represents.

    Implementations raise `AppError` (401/403) for any token that is not acceptable.
    """

    name: str

    @abstractmethod
    async def authenticate(self, token: str) -> AuthenticatedUser: ...

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network resources held by the provider."""
