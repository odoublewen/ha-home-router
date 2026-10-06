"""Reboot button."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HomeRouterConfigEntry
from .entity import HomeRouterEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeRouterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RebootButton(entry.runtime_data, "reboot")])


class RebootButton(HomeRouterEntity, ButtonEntity):
    """Reboots the router.

    Pressing returns as soon as the router accepts the command. The outcome arrives
    later as a ``home_router_reboot_finished`` event, about two minutes on.
    """

    _attr_device_class = ButtonDeviceClass.RESTART

    @property
    def available(self) -> bool:
        # Polling fails while the router is down; that is no reason to hide the button.
        return not self.coordinator.rebooting

    async def async_press(self) -> None:
        await self.coordinator.async_reboot()
        self.async_write_ha_state()
