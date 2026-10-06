"""Count of port forwards on the router, managed by Home Assistant or not."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HomeRouterConfigEntry
from .entity import HomeRouterEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeRouterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([PortForwardCountSensor(entry.runtime_data, "port_forwards")])


class PortForwardCountSensor(HomeRouterEntity, SensorEntity):
    """How many port-forward entries the router holds; a tcp+udp rule counts twice."""

    _attr_translation_key = "port_forwards"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"rules": [f.as_dict() for f in self.coordinator.data or []]}
