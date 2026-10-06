"""Router drivers. Nothing in this package imports Home Assistant."""

from __future__ import annotations

from .base import Router
from .errors import AuthError, DeviceError, RouterError, UnknownDeviceError
from .models import PortForward, Protocol, RebootWait, RouterConfig
from .quantum_c5500xk import QuantumC5500XK, validate_rule_name

#: Supported router models, keyed by the value stored in the config entry.
DEVICES: dict[str, type[Router]] = {
    QuantumC5500XK.model: QuantumC5500XK,
}


def get_device(model: str) -> type[Router]:
    """Look up a device class by model name."""
    try:
        return DEVICES[model]
    except KeyError:
        known = ", ".join(sorted(DEVICES))
        raise UnknownDeviceError(f"Unknown device {model!r}. Known models: {known}") from None


__all__ = [
    "DEVICES",
    "AuthError",
    "DeviceError",
    "PortForward",
    "Protocol",
    "QuantumC5500XK",
    "RebootWait",
    "Router",
    "RouterConfig",
    "RouterError",
    "UnknownDeviceError",
    "get_device",
    "validate_rule_name",
]
