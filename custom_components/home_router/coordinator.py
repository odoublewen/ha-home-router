"""Polls the router's port forwards and serialises every call to it."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, EVENT_REBOOT_FINISHED
from .router import AuthError, DeviceError, PortForward, RebootWait, Router, RouterError

if TYPE_CHECKING:
    from . import HomeRouterConfigEntry

_LOGGER = logging.getLogger(__name__)


class HomeRouterCoordinator(DataUpdateCoordinator[list[PortForward]]):
    """Owns the router session.

    The router keeps one login cookie per session, so calls go through ``async_call``
    one at a time, and a call that finds the session expired logs in again and retries
    once.
    """

    config_entry: HomeRouterConfigEntry

    def __init__(self, hass: HomeAssistant, entry: HomeRouterConfigEntry, router: Router) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.router = router
        self._lock = asyncio.Lock()
        #: True while a reboot is being watched. A flag rather than "is there a task",
        #: because background tasks start eagerly and can finish before being assigned.
        self.rebooting = False
        #: Port-forward subentries at setup, so a deleted one's rule can be removed.
        self.port_forward_names: dict[str, str] = {}

    async def async_call[T](self, action: Callable[[Router], Awaitable[T]]) -> T:
        """Run ``action`` against the router, logging in again if the session expired."""
        async with self._lock:
            try:
                return await action(self.router)
            except AuthError:
                _LOGGER.debug("Router session expired; logging in again")
                await self.router.login()
                return await action(self.router)

    async def async_change[T](self, action: Callable[[Router], Awaitable[T]]) -> T:
        """Run a modifying ``action`` for a user request, then refresh.

        Failures surface in the UI as HomeAssistantError rather than tracebacks.
        """
        if self.rebooting:
            raise HomeAssistantError("The router is rebooting; try again once it is back")
        try:
            result = await self.async_call(action)
        except RouterError as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            await self.async_request_refresh()
        return result

    async def _async_update_data(self) -> list[PortForward]:
        try:
            return await self.async_call(lambda router: router.list_forwards())
        except AuthError as err:
            if self.rebooting:
                # A half-booted router can refuse a login it will accept a minute later.
                raise UpdateFailed(f"Router is rebooting: {err}") from err
            raise ConfigEntryAuthFailed(str(err)) from err
        except DeviceError as err:
            raise UpdateFailed(str(err)) from err

    async def async_reboot(self) -> None:
        """Send the reboot, then watch for it to finish in the background."""
        if self.rebooting:
            raise HomeAssistantError("The router is already rebooting")
        try:
            await self.async_call(lambda router: router.reboot())
        except RouterError as err:
            raise HomeAssistantError(str(err)) from err
        self.rebooting = True
        self.config_entry.async_create_background_task(
            self.hass, self._async_watch_reboot(), f"{DOMAIN} reboot watch"
        )

    async def _async_watch_reboot(self) -> None:
        try:
            outcome = await self.router.wait_for_reboot()
        finally:
            self.rebooting = False
        if outcome is RebootWait.RECOVERED:
            _LOGGER.info("Router rebooted and is back online")
        elif outcome is RebootWait.NEVER_WENT_DOWN:
            _LOGGER.warning("Router accepted the reboot command but never went offline")
        else:
            _LOGGER.error("Router went offline to reboot and has not come back")
        self.hass.bus.async_fire(
            EVENT_REBOOT_FINISHED,
            {"config_entry_id": self.config_entry.entry_id, "result": outcome.value},
        )
        await self.async_request_refresh()
