"""Home Router: port forwarding switches and a reboot button for home routers."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.ssl import get_default_context, get_default_no_verify_context

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    CONF_MODEL,
    CONF_VERIFY_TLS,
    DOMAIN,
    SERVICE_CLEAR_PORT_FORWARDS,
    SUBENTRY_PORT_FORWARD,
)
from .coordinator import HomeRouterCoordinator
from .router import AuthError, DeviceError, Router, RouterConfig, RouterError, get_device

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type HomeRouterConfigEntry = ConfigEntry[HomeRouterCoordinator]

CLEAR_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})


def create_router(data: dict) -> Router:
    """Build an unconnected router from config entry data."""
    config = RouterConfig(
        host=data[CONF_HOST], username=data[CONF_USERNAME], password=data[CONF_PASSWORD]
    )
    ssl_context = (
        get_default_context() if data.get(CONF_VERIFY_TLS) else get_default_no_verify_context()
    )
    return get_device(data[CONF_MODEL])(config, ssl_context=ssl_context)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""

    async def clear_port_forwards(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator_for(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        removed = await coordinator.async_change(lambda router: router.clear_forwards())
        return {"removed": removed}

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_PORT_FORWARDS,
        clear_port_forwards,
        schema=CLEAR_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


def _coordinator_for(hass: HomeAssistant, entry_id: str | None) -> HomeRouterCoordinator:
    """The loaded entry an action targets; the only one when none is named."""
    loaded = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id is not None:
        loaded = [entry for entry in loaded if entry.entry_id == entry_id]
        if not loaded:
            raise ServiceValidationError(f"No loaded {DOMAIN} entry with id {entry_id}")
    elif len(loaded) != 1:
        raise ServiceValidationError(
            f"Found {len(loaded)} loaded routers; say which one with {ATTR_CONFIG_ENTRY_ID}"
        )
    return loaded[0].runtime_data


async def async_setup_entry(hass: HomeAssistant, entry: HomeRouterConfigEntry) -> bool:
    """Log in, fetch the current rules and set up the platforms."""
    router = create_router(dict(entry.data))
    try:
        await router.login()
        coordinator = HomeRouterCoordinator(hass, entry, router)
        await coordinator.async_config_entry_first_refresh()
    except AuthError as err:
        await router.close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except DeviceError as err:
        await router.close()
        raise ConfigEntryNotReady(str(err)) from err
    except Exception:
        await router.close()
        raise

    entry.runtime_data = coordinator
    coordinator.port_forward_names = _port_forward_names(entry)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _port_forward_names(entry: HomeRouterConfigEntry) -> dict[str, str]:
    """Rule name of each port-forward subentry, by subentry id."""
    return {
        subentry.subentry_id: subentry.data[CONF_NAME]
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_PORT_FORWARD
    }


async def _async_entry_updated(hass: HomeAssistant, entry: HomeRouterConfigEntry) -> None:
    """Options or port-forward subentries changed: tidy up, then reload.

    A deleted port forward takes its rule on the router with it. Failing to reach the
    router is logged, not raised, so the reload still happens.
    """
    coordinator = entry.runtime_data
    current = _port_forward_names(entry)
    for subentry_id, name in coordinator.port_forward_names.items():
        if subentry_id in current:
            continue
        try:
            await coordinator.async_call(lambda router, name=name: router.remove_by_name(name))
        except RouterError as err:
            _LOGGER.warning("Could not remove port forward %s from the router: %s", name, err)
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HomeRouterConfigEntry) -> bool:
    """Tear down the platforms and the router session."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.router.close()
    return unloaded
