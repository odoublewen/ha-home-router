"""Shared fixtures: an in-memory router standing in for the real device."""

from __future__ import annotations

import ssl
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_router.const import CONF_MODEL, CONF_VERIFY_TLS, DOMAIN
from custom_components.home_router.router import (
    AuthError,
    DeviceError,
    PortForward,
    Protocol,
    RebootWait,
    Router,
    RouterConfig,
)
from custom_components.home_router.router.quantum_c5500xk import QuantumC5500XK

ENTRY_DATA = {
    CONF_HOST: "192.168.1.1",
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "secret",
    CONF_MODEL: "quantum-c5500xk",
    CONF_VERIFY_TLS: False,
}


class FakeRouter(Router):
    """Keeps rules in a list. Flip the flags to simulate failures."""

    model = QuantumC5500XK.model
    display_name = QuantumC5500XK.display_name
    manufacturer = QuantumC5500XK.manufacturer
    model_name = QuantumC5500XK.model_name

    def __init__(self) -> None:
        super().__init__(
            RouterConfig(host="192.168.1.1", username="admin", password="secret"),
            ssl_context=ssl.create_default_context(),
        )
        self.forwards: list[PortForward] = []
        self.password_ok = True
        self.reachable = True
        self.session_valid = False
        self.logins = 0
        self.reboots = 0
        self.reboot_outcome = RebootWait.RECOVERED
        self.closed = False

    def _check(self) -> None:
        if not self.reachable:
            raise DeviceError("Could not reach the router")
        if not self.session_valid:
            raise AuthError("Router rejected the request (HTTP 444)")

    async def login(self) -> None:
        if not self.reachable:
            raise DeviceError("Could not reach the router")
        if not self.password_ok:
            raise AuthError("Login failed")
        self.logins += 1
        self.session_valid = True

    async def close(self) -> None:
        self.closed = True

    async def list_forwards(self) -> list[PortForward]:
        self._check()
        return list(self.forwards)

    async def add_forward(self, forward: PortForward) -> None:
        self._check()
        QuantumC5500XK._check_conflicts(forward, self.forwards)
        both = forward.protocol is Protocol.BOTH
        protocols = [Protocol.TCP, Protocol.UDP] if both else [forward.protocol]
        for protocol in protocols:
            self.forwards.append(
                PortForward(
                    internal_ip=forward.internal_ip,
                    internal_port=forward.internal_port,
                    external_port=forward.external_port,
                    protocol=protocol,
                    name=forward.name,
                    key=f"key{len(self.forwards) + 1}",
                )
            )

    async def remove_forward(self, forward: PortForward) -> None:
        self._check()
        self.forwards = [f for f in self.forwards if f.key != forward.key]

    async def remove_by_name(self, name: str) -> int:
        self._check()
        before = len(self.forwards)
        self.forwards = [f for f in self.forwards if f.name != name]
        return before - len(self.forwards)

    async def clear_forwards(self) -> int:
        self._check()
        removed = len(self.forwards)
        self.forwards = []
        return removed

    async def reboot(self) -> None:
        self._check()
        self.reboots += 1
        self.session_valid = False

    async def wait_for_reboot(self, **kwargs) -> RebootWait:
        return self.reboot_outcome


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def router() -> FakeRouter:
    fake = FakeRouter()
    with (
        patch("custom_components.home_router.create_router", return_value=fake),
        patch("custom_components.home_router.config_flow.create_router", return_value=fake),
    ):
        yield fake


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, title="Quantum Fiber C5500XK", unique_id="192.168.1.1", data=ENTRY_DATA
    )
