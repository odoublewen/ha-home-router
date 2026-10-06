"""Value types shared by Home Assistant and the device implementations."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from enum import StrEnum

from .errors import RouterError

MIN_PORT = 1
MAX_PORT = 65535


@dataclass(frozen=True)
class RouterConfig:
    """Everything needed to talk to one router."""

    host: str
    username: str
    password: str
    #: Seconds to wait on any single HTTP request.
    timeout: float = 30.0

    @property
    def base_url(self) -> str:
        """Root URL of the router's admin interface."""
        host = self.host
        if "://" in host:
            return host.rstrip("/")
        return f"https://{host}"


class RebootWait(StrEnum):
    """How waiting for a rebooting router turned out."""

    #: Seen to go offline and then answer again.
    RECOVERED = "recovered"
    #: Never stopped answering, so we cannot confirm the reboot happened.
    NEVER_WENT_DOWN = "never_went_down"
    #: Went offline and had not returned before the timeout.
    STILL_DOWN = "still_down"


class Protocol(StrEnum):
    """Transport protocol a forwarding rule applies to."""

    TCP = "tcp"
    UDP = "udp"
    BOTH = "both"


def validate_ip(value: str) -> str:
    """Return *value* as a dotted-quad string, or raise if it is not an IPv4 address."""
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise RouterError(f"{value!r} is not a valid IPv4 address") from exc


def validate_port(value: int, label: str = "port") -> int:
    """Return *value* if it is a usable TCP/UDP port number, else raise."""
    if not MIN_PORT <= value <= MAX_PORT:
        raise RouterError(f"{label} {value} is out of range ({MIN_PORT}-{MAX_PORT})")
    return value


@dataclass(frozen=True)
class PortForward:
    """A single port-forwarding rule.

    The source IP is always "All IP Addresses" -- restricting it is not exposed.
    """

    internal_ip: str
    internal_port: int
    external_port: int
    protocol: Protocol = Protocol.TCP
    name: str = ""
    #: Opaque device-side identifier; set by the device when reading, empty when creating.
    key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "internal_ip", validate_ip(self.internal_ip))
        validate_port(self.internal_port, "internal port")
        validate_port(self.external_port, "external port")

    @property
    def summary(self) -> str:
        """One-line description used in logs and error messages."""
        return (
            f"{self.protocol.value}/{self.external_port} -> {self.internal_ip}:{self.internal_port}"
        )

    def as_dict(self) -> dict[str, object]:
        """Plain representation for entity attributes."""
        return {
            "name": self.name,
            "protocol": self.protocol.value,
            "external_port": self.external_port,
            "internal_ip": self.internal_ip,
            "internal_port": self.internal_port,
        }
