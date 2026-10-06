from collections.abc import Callable

from app.core.config import PowerBIGatewayName, Settings, get_settings
from app.core.errors import ProviderNotAvailableError
from app.powerbi.base import PowerBIGateway
from app.powerbi.dev_synthetic import DevSyntheticGateway
from app.powerbi.fabric_iq import FabricIqMcpGateway
from app.powerbi.rest import PowerBiRestGateway

GatewayBuilder = Callable[[Settings], PowerBIGateway]

# Selection is driven only by POWERBI_GATEWAY / POWERBI_FALLBACK_GATEWAY.
_REGISTRY: dict[PowerBIGatewayName, GatewayBuilder] = {
    PowerBIGatewayName.FABRIC_IQ_MCP: FabricIqMcpGateway,
    PowerBIGatewayName.REST: PowerBiRestGateway,
    PowerBIGatewayName.DEV_SYNTHETIC: DevSyntheticGateway,
}


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


def create_gateways(settings: Settings) -> tuple[PowerBIGateway, PowerBIGateway | None]:
    """Primary and optional fallback (a fallback equal to the primary is ignored)."""
    primary = create_powerbi_gateway(settings, settings.powerbi_gateway)
    fallback_name = settings.powerbi_fallback_gateway
    if fallback_name is None or fallback_name == settings.powerbi_gateway:
        return primary, None
    return primary, create_powerbi_gateway(settings, fallback_name)
