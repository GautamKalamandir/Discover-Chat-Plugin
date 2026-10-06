from collections.abc import Callable

from app.core.config import PowerBIGatewayName, Settings, get_settings
from app.core.errors import ProviderNotAvailableError
from app.powerbi.base import PowerBIGateway

GatewayBuilder = Callable[[Settings], PowerBIGateway]

# Implementations register themselves here (Phase 6). Selection is driven only by POWERBI_GATEWAY.
_REGISTRY: dict[PowerBIGatewayName, GatewayBuilder] = {}


def register_powerbi_gateway(name: PowerBIGatewayName, builder: GatewayBuilder) -> None:
    _REGISTRY[name] = builder


def create_powerbi_gateway(
    settings: Settings | None = None, name: PowerBIGatewayName | None = None
) -> PowerBIGateway:
    settings = settings or get_settings()
    selected = name or settings.powerbi_gateway
    builder = _REGISTRY.get(selected)
    if builder is None:
        raise ProviderNotAvailableError("Power BI gateway", selected, [g.value for g in _REGISTRY])
    return builder(settings)
