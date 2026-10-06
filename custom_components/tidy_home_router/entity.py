"""Base entity: everything hangs off one device per router."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HomeRouterCoordinator


def router_device_info(coordinator: HomeRouterCoordinator) -> DeviceInfo:
    """The router's device, which the port-forward devices link to."""
    router = coordinator.router
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
        name=router.display_name,
        manufacturer=router.manufacturer,
        model=router.model_name,
        configuration_url=router.config.base_url,
    )


class HomeRouterEntity(CoordinatorEntity[HomeRouterCoordinator]):
    """An entity on the router's device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HomeRouterCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._attr_device_info = router_device_info(coordinator)
