"""Quantum Fiber C5500XK.

The admin interface is a React app talking to a small CGI API, so we speak to that
API directly rather than driving a browser:

    GET  /cgi/cgi_get?Object=<tr181 path>   read objects
    POST /cgi/cgi_set                       body is an operation string
    POST /cgi/cgi_action                    login, and actions such as Action=Reboot

Every request carries ``X-Requested-With: XMLHttpRequest``; without it the device
answers with the SPA shell. HTTP 444 means the session is gone.

Port forwards live under ``Device.NAT.PortMapping``. One rule covering both TCP and
UDP is stored as two instances, which is also how the web UI does it.
"""

from __future__ import annotations

import logging
import re
import ssl
from typing import Any, ClassVar

import httpx

from .base import Router
from .errors import AuthError, DeviceError, RouterError
from .models import PortForward, Protocol, RouterConfig

logger = logging.getLogger(__name__)

PORT_MAPPING_OBJECT = "Device.NAT.PortMapping"
#: Vendor prefix this model uses for its TR-181 extensions (tr181Prefix1/2 in the web UI).
TR181_PREFIX = "X_AXON_"
#: Descriptions the web UI generates, e.g. "PortMapping_3".
DESCRIPTION_TEMPLATE = "PortMapping_{index}"
_DESCRIPTION_INDEX = re.compile(r"_(\d+)$")
#: Trailing instance number of a TR-181 object name, e.g. "Device.NAT.PortMapping.3.".
_INSTANCE_INDEX = re.compile(r"\.(\d+)\.?$")
#: Characters safe to drop into an operation string without escaping.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_NOT_LOGGED_IN = 444
#: Cheap read used both to confirm a login and to tell when the CGI layer is alive.
_SESSION_PROBE_QUERY = "Object=Device.UserInterface&PasswordRequired="
#: Statuses that prove the CGI layer is answering (444 = alive but not logged in).
_READY_STATUSES = frozenset({200, _NOT_LOGGED_IN})
#: Statuses that mean "your credentials or session are the problem".
_AUTH_STATUSES = frozenset({401, 403, _NOT_LOGGED_IN})

_DEVICE_PROTOCOLS: dict[Protocol, tuple[str, ...]] = {
    Protocol.TCP: ("TCP",),
    Protocol.UDP: ("UDP",),
    Protocol.BOTH: ("TCP", "UDP"),
}


def validate_rule_name(name: str) -> str:
    """Reject names that would need escaping inside an operation string."""
    if not _SAFE_NAME.match(name):
        raise RouterError(f"Rule name {name!r} may only contain letters, digits, '.', '-' and '_'")
    return name


class QuantumC5500XK(Router):
    """Quantum Fiber C5500XK (Calix/AXON platform)."""

    model: ClassVar[str] = "quantum-c5500xk"
    display_name: ClassVar[str] = "Quantum Fiber C5500XK"
    manufacturer: ClassVar[str] = "Quantum Fiber"
    model_name: ClassVar[str] = "C5500XK"

    def __init__(self, config: RouterConfig, *, ssl_context: ssl.SSLContext) -> None:
        super().__init__(config, ssl_context=ssl_context)
        self._client: httpx.AsyncClient | None = None

    # -- transport ---------------------------------------------------------

    @property
    def client(self) -> httpx.AsyncClient:
        """Lazily created HTTP session; cookies carry the login."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url,
                verify=self.ssl_context,
                timeout=self.config.timeout,
                headers={"X-Requested-With": "XMLHttpRequest"},
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise DeviceError(f"Could not reach {self.config.base_url}: {exc}") from exc
        if response.status_code in _AUTH_STATUSES:
            raise AuthError(
                f"Router rejected the request (HTTP {response.status_code}); "
                "check the password, or log in again"
            )
        if response.status_code >= 400:
            raise DeviceError(f"{method} {url} failed: HTTP {response.status_code}")
        return response

    async def _cgi_get(self, query: str) -> dict[str, dict[str, str]]:
        """Read objects and flatten them to {object name: {param: value}}."""
        response = await self._request("GET", f"/cgi/cgi_get?{query}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DeviceError(f"Router returned a non-JSON reply to {query!r}") from exc
        return _flatten_objects(payload)

    async def _cgi_set(self, operations: str) -> None:
        """Apply one or more comma-separated operation strings."""
        logger.debug("POST /cgi/cgi_set %s", operations)
        await self._request(
            "POST",
            "/cgi/cgi_set",
            content=operations.encode(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    # -- session -----------------------------------------------------------

    async def login(self) -> None:
        body = f"username={self.config.username}&password={self.config.password}"
        response = await self._request("POST", "/cgi/cgi_action", content=body.encode())
        if not await self.logged_in():
            raise AuthError(
                f"Login failed for user {self.config.username!r} "
                f"(router replied HTTP {response.status_code})"
            )
        logger.debug("Logged in to %s at %s", self.display_name, self.config.base_url)

    async def logged_in(self) -> bool:
        """True when the current session can read a protected object."""
        try:
            data = await self._cgi_get(_SESSION_PROBE_QUERY)
        except AuthError:
            return False
        return bool(data)

    # -- port forwarding ---------------------------------------------------

    async def list_forwards(self) -> list[PortForward]:
        data = await self._cgi_get(f"Object={PORT_MAPPING_OBJECT}")
        forwards = []
        for key, params in sorted(data.items(), key=_instance_sort_key):
            if not params.get("Protocol"):
                continue  # the container object itself, not an instance
            forwards.append(_to_port_forward(key, params))
        return forwards

    async def add_forward(self, forward: PortForward) -> None:
        existing = await self.list_forwards()
        self._check_conflicts(forward, existing)
        name = validate_rule_name(forward.name or _next_rule_name(existing))
        operations = [
            self._add_operation(forward, name, device_protocol)
            for device_protocol in _DEVICE_PROTOCOLS[forward.protocol]
        ]
        logger.info("Adding port forward %s as %s", forward.summary, name)
        await self._cgi_set(",".join(operations))

    @staticmethod
    def _check_conflicts(forward: PortForward, existing: list[PortForward]) -> None:
        """Refuse a WAN port already claimed by another rule.

        The web UI enforces this in the browser only -- the device itself will happily
        store two rules competing for the same port, and which one wins is anyone's
        guess.
        """
        wanted = set(_DEVICE_PROTOCOLS[forward.protocol])
        for rule in existing:
            if rule.external_port != forward.external_port:
                continue
            if not wanted & set(_DEVICE_PROTOCOLS[rule.protocol]):
                continue
            raise DeviceError(
                f"WAN port {forward.external_port} is already forwarded to "
                f"{rule.internal_ip}:{rule.internal_port} by rule {rule.name!r}. "
                "Remove that rule first."
            )

    def _add_operation(self, forward: PortForward, name: str, device_protocol: str) -> str:
        """Build one Add operation string, mirroring what the web UI sends."""
        return (
            f"Object={PORT_MAPPING_OBJECT}"
            "&Operation=Add"
            "&AllInterfaces=0"
            "&RemoteHost="  # empty means "All IP Addresses"
            f"&{TR181_PREFIX}INTERFACE=wan"
            f"&ExternalPort={forward.external_port}"
            f"&ExternalPortEndRange={forward.external_port}"
            f"&InternalPort={forward.internal_port}"
            f"&InternalClient={forward.internal_ip}"
            "&Enable=1"
            f"&Description={name}"
            f"&{TR181_PREFIX}Via=UI"
            f"&Protocol={device_protocol}"
        )

    async def remove_forward(self, forward: PortForward) -> None:
        if not forward.key:
            raise DeviceError(f"Cannot remove {forward.summary}: no device key")
        await self._remove_keys([forward.key])

    async def remove_by_name(self, name: str) -> int:
        matching = [f for f in await self.list_forwards() if f.name == name]
        if not matching:
            logger.info("No port forward named %s to remove", name)
            return 0
        for forward in matching:
            logger.info("Removing port forward %s (%s)", forward.summary, forward.name)
        await self._remove_keys([f.key for f in matching])
        return len(matching)

    async def clear_forwards(self) -> int:
        forwards = await self.list_forwards()
        if not forwards:
            logger.info("No port forwards to remove")
            return 0
        for forward in forwards:
            logger.info("Removing port forward %s (%s)", forward.summary, forward.name)
        await self._remove_keys([f.key for f in forwards])
        return len(forwards)

    async def _remove_keys(self, keys: list[str]) -> None:
        """Delete instances in one request, then confirm they are gone.

        Deleting renumbers the remaining instances, so a batch that the device only
        partially applied would leave stale rules behind; re-read and retry until the
        list stops shrinking.
        """
        remaining = list(keys)
        while remaining:
            await self._cgi_set(",".join(f"Object={key}&Operation=Del&" for key in remaining))
            still_there = {f.key for f in await self.list_forwards()}
            left = [key for key in remaining if key in still_there]
            if len(left) == len(remaining):
                raise DeviceError(f"Router did not remove port forward(s): {', '.join(left)}")
            remaining = left

    # -- power -------------------------------------------------------------

    async def _probe(self, client: httpx.AsyncClient) -> bool:
        """True once the CGI layer answers, not merely the web server.

        During a reboot lighttpd serves "/" (302) and static pages (200) well before
        the management API exists, so those tell us nothing. Only the CGI layer
        produces 444 for an unauthenticated read, which makes it a real readiness
        signal.

        Observed across a full reboot: 444 while still serving, then 503 for about
        100 seconds, then 444 again once it is genuinely back. Treating that 503 as
        "answered" is what used to report recovery a minute early.
        """
        try:
            response = await client.get(
                f"/cgi/cgi_get?{_SESSION_PROBE_QUERY}",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        except (httpx.HTTPError, OSError):
            return False
        return response.status_code in _READY_STATUSES

    async def reboot(self) -> None:
        logger.info("Sending reboot command")
        await self._request("POST", "/cgi/cgi_action", content=b"Action=Reboot")
        logger.info("Reboot command accepted")


def _next_rule_name(existing: list[PortForward]) -> str:
    """Continue the web UI's PortMapping_N numbering."""
    highest = 0
    for forward in existing:
        match = _DESCRIPTION_INDEX.search(forward.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return DESCRIPTION_TEMPLATE.format(index=highest + 1)


def _flatten_objects(payload: Any) -> dict[str, dict[str, str]]:
    """Turn the CGI reply into {"Device.NAT.PortMapping.1.": {"Protocol": "TCP", ...}}."""
    if not isinstance(payload, dict):
        return {}
    objects = payload.get("Objects")
    if not isinstance(objects, list):
        return {}
    flattened: dict[str, dict[str, str]] = {}
    for obj in objects:
        name = str(obj.get("ObjName", ""))
        if not name:
            continue
        if not name.endswith("."):
            name += "."
        flattened[name] = {
            str(param.get("ParamName")): str(param.get("ParamValue", ""))
            for param in obj.get("Param", [])
        }
    return flattened


def _instance_sort_key(item: tuple[str, dict[str, str]]) -> tuple[int, str]:
    """Sort Device.NAT.PortMapping.10. after .2. rather than lexically."""
    name = item[0]
    match = _INSTANCE_INDEX.search(name)
    return (int(match.group(1)) if match else 0, name)


def _to_port_forward(key: str, params: dict[str, str]) -> PortForward:
    """Build a PortForward from one PortMapping instance."""
    protocol = Protocol.UDP if params.get("Protocol", "").upper() == "UDP" else Protocol.TCP
    external = params.get("ExternalPort") or "0"
    internal = params.get("InternalPort") or external
    return PortForward(
        internal_ip=params.get("InternalClient", "0.0.0.0"),
        internal_port=int(internal),
        external_port=int(external),
        protocol=protocol,
        name=params.get("Description", ""),
        key=key,
    )
