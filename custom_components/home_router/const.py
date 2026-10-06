"""Constants for the Home Router integration."""

from typing import Final

DOMAIN: Final = "home_router"

CONF_MODEL: Final = "model"
CONF_VERIFY_TLS: Final = "verify_tls"

DEFAULT_HOST: Final = "192.168.1.1"
DEFAULT_USERNAME: Final = "admin"
DEFAULT_SCAN_INTERVAL: Final = 60

SUBENTRY_PORT_FORWARD: Final = "port_forward"
CONF_INTERNAL_IP: Final = "internal_ip"
CONF_INTERNAL_PORT: Final = "internal_port"
CONF_EXTERNAL_PORT: Final = "external_port"
CONF_PROTOCOL: Final = "protocol"

EVENT_REBOOT_FINISHED: Final = f"{DOMAIN}_reboot_finished"
SERVICE_CLEAR_PORT_FORWARDS: Final = "clear_port_forwards"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
