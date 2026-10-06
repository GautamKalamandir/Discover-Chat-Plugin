class ProviderNotAvailableError(RuntimeError):
    """The provider selected in `.env` has no registered implementation yet."""

    def __init__(self, kind: str, name: str, available: list[str]) -> None:
        super().__init__(
            f"{kind} provider '{name}' is not available. "
            f"Registered providers: {available or 'none yet'}."
        )
