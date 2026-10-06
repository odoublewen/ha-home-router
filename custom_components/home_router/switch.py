"""One switch per predefined port forward: on means the rule exists on the router."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HomeRouterConfigEntry
from .const import (
    CONF_EXTERNAL_PORT,
    CONF_INTERNAL_IP,
    CONF_INTERNAL_PORT,
    CONF_PROTOCOL,
    DOMAIN,
    SUBENTRY_PORT_FORWARD,
)
from .coordinator import HomeRouterCoordinator
from .entity import HomeRouterEntity
from .router import PortForward, Protocol

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeRouterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_PORT_FORWARD:
            continue
        async_add_entities(
            [PortForwardSwitch(coordinator, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


def forward_from_subentry(data: dict[str, Any]) -> PortForward:
    """The rule a port-forward subentry describes."""
    return PortForward(
        internal_ip=data[CONF_INTERNAL_IP],
        internal_port=int(data[CONF_INTERNAL_PORT]),
        external_port=int(data[CONF_EXTERNAL_PORT]),
        protocol=Protocol(data[CONF_PROTOCOL]),
        name=data[CONF_NAME],
    )


class PortForwardSwitch(HomeRouterEntity, SwitchEntity):
    """Turns one predefined rule on (created on the router) or off (deleted).

    The rule is recognised on the router by its name, so the state also reflects
    changes made in the router's own web UI.

    Each port forward gets its own device, linked to the router. Home Assistant's
    integration page lists a device under whichever subentry it belongs to, so a
    switch on the router's own device would drag the restart button and sensor out
    of sight under the port forward.
    """

    _attr_translation_key = "port_forward"
    _attr_name = None

    def __init__(self, coordinator: HomeRouterCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator, subentry.subentry_id)
        self.forward = forward_from_subentry(dict(subentry.data))
        entry_id = coordinator.config_entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{subentry.subentry_id}")},
            name=subentry.title,
            model="Port forward",
            entry_type=DeviceEntryType.SERVICE,
            via_device=(DOMAIN, entry_id),
        )

    def _live(self) -> list[PortForward]:
        """Rules on the router carrying this switch's name (two for tcp+udp)."""
        return [f for f in self.coordinator.data or [] if f.name == self.forward.name]

    @property
    def is_on(self) -> bool:
        return bool(self._live())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = self.forward.as_dict()
        live = self._live()
        # A rule of the same name that forwards somewhere else: edited in the web UI,
        # or left over from before this switch's definition changed.
        attributes["mismatch"] = any(not _matches(self.forward, rule) for rule in live)
        return attributes

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.is_on:
            return
        await self.coordinator.async_change(lambda router: router.add_forward(self.forward))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_change(lambda router: router.remove_by_name(self.forward.name))


def _matches(wanted: PortForward, live: PortForward) -> bool:
    """True if ``live`` is (one half of) the rule ``wanted`` describes."""
    protocols = (
        {Protocol.TCP, Protocol.UDP} if wanted.protocol is Protocol.BOTH else {wanted.protocol}
    )
    return (
        live.protocol in protocols
        and live.external_port == wanted.external_port
        and live.internal_ip == wanted.internal_ip
        and live.internal_port == wanted.internal_port
    )
