"""Exceptions raised by the router layer."""


class RouterError(Exception):
    """Base class for all expected failures."""


class AuthError(RouterError):
    """The router rejected our credentials, or the session expired."""


class DeviceError(RouterError):
    """The router was unreachable, or reachable but refused or failed the request."""


class UnknownDeviceError(RouterError):
    """The requested device model has no implementation."""
