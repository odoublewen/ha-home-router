"""The interface every supported router model implements."""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from abc import ABC, abstractmethod
from typing import ClassVar

import httpx

from .models import PortForward, RebootWait, RouterConfig

logger = logging.getLogger(__name__)

#: How often to report that we are still waiting, in seconds.
_PROGRESS_INTERVAL = 30.0


class Router(ABC):
    """A router we can log into, inspect and change.

    ``ssl_context`` is used for every connection. Pass a prebuilt one: building an
    SSL context loads certificates from disk, which must not happen in the event loop.
    The router serves a self-signed certificate, so callers normally pass a context
    that does not verify.
    """

    #: Registry key, stored in the config entry.
    model: ClassVar[str]
    #: Human-readable name shown in the UI and logs.
    display_name: ClassVar[str]
    #: Shown on the Home Assistant device page.
    manufacturer: ClassVar[str]
    model_name: ClassVar[str]

    def __init__(self, config: RouterConfig, *, ssl_context: ssl.SSLContext) -> None:
        self.config = config
        self.ssl_context = ssl_context

    @abstractmethod
    async def login(self) -> None:
        """Authenticate. Raises AuthError if the credentials are rejected."""

    async def close(self) -> None:
        """Release any transport resources. Safe to call more than once."""
        return None

    @abstractmethod
    async def list_forwards(self) -> list[PortForward]:
        """Return every port-forwarding rule currently configured."""

    @abstractmethod
    async def add_forward(self, forward: PortForward) -> None:
        """Create one port-forwarding rule."""

    @abstractmethod
    async def remove_forward(self, forward: PortForward) -> None:
        """Delete the rule identified by ``forward.key``."""

    @abstractmethod
    async def remove_by_name(self, name: str) -> int:
        """Delete every rule named ``name``. Returns how many were removed."""

    @abstractmethod
    async def clear_forwards(self) -> int:
        """Delete every rule. Returns how many were removed."""

    @abstractmethod
    async def reboot(self) -> None:
        """Ask the router to restart. Returns as soon as the request is accepted."""

    async def wait_for_reboot(
        self,
        *,
        down_timeout: float = 120.0,
        up_timeout: float = 420.0,
        interval: float = 5.0,
        down_streak: int = 3,
        up_streak: int = 3,
    ) -> RebootWait:
        """Watch the router through a reboot.

        A router does not go down the instant it accepts the command -- this model
        keeps serving for another 5-10 seconds. Polling only for "is it up again"
        therefore reports success immediately, having never seen it leave. So wait
        for it to go offline first, and only then for it to return.

        Both phases need several consecutive probes to agree. One failed probe is not
        proof the router went down, and one success is not proof it is back: the web
        server answers well before the device is actually usable, which is why
        ``_probe`` should test the management API rather than just the socket.
        """
        if not await self._wait_for_state(
            online=False, timeout=down_timeout, interval=interval, streak=down_streak
        ):
            return RebootWait.NEVER_WENT_DOWN
        logger.info("Router went offline; waiting for it to come back")
        if not await self._wait_for_state(
            online=True, timeout=up_timeout, interval=interval, streak=up_streak
        ):
            return RebootWait.STILL_DOWN
        return RebootWait.RECOVERED

    async def _wait_for_state(
        self, *, online: bool, timeout: float, interval: float, streak: int = 1
    ) -> bool:
        """Poll until the router has been in the wanted state ``streak`` times running."""
        deadline = time.monotonic() + timeout
        seen = 0
        probe = httpx.AsyncClient(
            base_url=self.config.base_url,
            verify=self.ssl_context,
            timeout=min(interval, 5.0),
            follow_redirects=False,
        )
        started = time.monotonic()
        wanted = "online" if online else "offline"
        next_report = started + _PROGRESS_INTERVAL
        try:
            while time.monotonic() < deadline:
                reachable = await self._probe(probe)
                seen = seen + 1 if reachable is online else 0
                logger.debug(
                    "probe: router %s (want %s, %d/%d)",
                    "answered" if reachable else "did not answer",
                    wanted,
                    seen,
                    streak,
                )
                if seen >= streak:
                    return True
                now = time.monotonic()
                if now >= next_report:
                    logger.info(
                        "Still waiting for the router to be %s (%ds elapsed)",
                        wanted,
                        int(now - started),
                    )
                    next_report = now + _PROGRESS_INTERVAL
                await asyncio.sleep(interval)
        finally:
            await probe.aclose()
        return False

    async def _probe(self, client: httpx.AsyncClient) -> bool:
        """True if the router is up and its management interface is usable.

        The default only checks that something answered. Subclasses should override
        this to test the layer they actually need, because a router's web server
        starts answering long before its management API works.
        """
        try:
            await client.get("/")
        except (httpx.HTTPError, OSError):
            return False
        return True
