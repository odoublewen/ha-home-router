"""Config flow: the router itself, its options, and the port forwards it can toggle."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import create_router
from .const import (
    CONF_EXTERNAL_PORT,
    CONF_INTERNAL_IP,
    CONF_INTERNAL_PORT,
    CONF_MODEL,
    CONF_PROTOCOL,
    CONF_VERIFY_TLS,
    DEFAULT_HOST,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
    SUBENTRY_PORT_FORWARD,
)
from .router import (
    DEVICES,
    AuthError,
    DeviceError,
    PortForward,
    Protocol,
    RouterError,
    validate_rule_name,
)

_LOGGER = logging.getLogger(__name__)

PORT_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
)


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, DEFAULT_HOST)): str,
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, DEFAULT_USERNAME)): str,
            vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_MODEL, default=defaults.get(CONF_MODEL, next(iter(DEVICES)))): (
                SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": model, "label": device.display_name}
                            for model, device in DEVICES.items()
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            ),
            vol.Required(CONF_VERIFY_TLS, default=defaults.get(CONF_VERIFY_TLS, False)): bool,
        }
    )


async def _check_login(data: Mapping[str, Any]) -> str | None:
    """Try the credentials. Returns an error key, or None if they work."""
    router = create_router(dict(data))
    try:
        await router.login()
    except AuthError:
        return "invalid_auth"
    except DeviceError:
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error logging in to the router")
        return "unknown"
    finally:
        await router.close()
    return None


class HomeRouterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a router."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()
            if (error := await _check_login(user_input)) is None:
                return self.async_create_entry(
                    title=DEVICES[user_input[CONF_MODEL]].display_name, data=user_input
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user", data_schema=_user_schema(user_input or {}), errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            if (error := await _check_login(data)) is None:
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            description_placeholders={
                CONF_USERNAME: entry.data[CONF_USERNAME],
                CONF_HOST: entry.data[CONF_HOST],
            },
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HomeRouterOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_PORT_FORWARD: PortForwardSubentryFlow}


class HomeRouterOptionsFlow(OptionsFlow):
    """How often to poll."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=10,
                            max=3600,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )


class PortForwardSubentryFlow(ConfigSubentryFlow):
    """Define a port forward that a switch can turn on and off."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._async_step_form("user", user_input, None)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        # Changing a rule that is live would strand the old one on the router under
        # a name the switch no longer looks for.
        coordinator = getattr(self._get_entry(), "runtime_data", None)
        if coordinator is not None and any(
            f.name == subentry.data[CONF_NAME] for f in coordinator.data or []
        ):
            return self.async_abort(reason="rule_active")
        return await self._async_step_form("reconfigure", user_input, subentry.subentry_id)

    async def _async_step_form(
        self, step_id: str, user_input: dict[str, Any] | None, editing: str | None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = self._validate(user_input, editing)
            if not errors:
                if editing is None:
                    return self.async_create_entry(
                        title=data[CONF_NAME], data=data, unique_id=data[CONF_NAME]
                    )
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    title=data[CONF_NAME],
                    unique_id=data[CONF_NAME],
                    data=data,
                )
        if user_input is None and editing is not None:
            user_input = dict(self._get_reconfigure_subentry().data)
        return self.async_show_form(
            step_id=step_id, data_schema=_forward_schema(user_input or {}), errors=errors
        )

    def _validate(
        self, user_input: dict[str, Any], editing: str | None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        name = user_input[CONF_NAME].strip()
        internal_port = int(user_input[CONF_INTERNAL_PORT])
        external = user_input.get(CONF_EXTERNAL_PORT)
        data = {
            CONF_NAME: name,
            CONF_INTERNAL_IP: user_input[CONF_INTERNAL_IP].strip(),
            CONF_INTERNAL_PORT: internal_port,
            CONF_EXTERNAL_PORT: int(external) if external else internal_port,
            CONF_PROTOCOL: user_input[CONF_PROTOCOL],
        }
        errors: dict[str, str] = {}
        try:
            validate_rule_name(name)
        except RouterError:
            errors[CONF_NAME] = "invalid_name"
        else:
            if any(
                sub.data.get(CONF_NAME) == name and sub.subentry_id != editing
                for sub in self._get_entry().subentries.values()
            ):
                errors[CONF_NAME] = "name_exists"
        try:
            PortForward(
                internal_ip=data[CONF_INTERNAL_IP],
                internal_port=data[CONF_INTERNAL_PORT],
                external_port=data[CONF_EXTERNAL_PORT],
            )
        except RouterError:
            errors[CONF_INTERNAL_IP] = "invalid_ip"
        return data, errors


def _forward_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    external = defaults.get(CONF_EXTERNAL_PORT)
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, vol.UNDEFINED)): str,
            vol.Required(
                CONF_INTERNAL_IP, default=defaults.get(CONF_INTERNAL_IP, vol.UNDEFINED)
            ): str,
            vol.Required(
                CONF_INTERNAL_PORT, default=defaults.get(CONF_INTERNAL_PORT, vol.UNDEFINED)
            ): PORT_SELECTOR,
            vol.Optional(
                CONF_EXTERNAL_PORT,
                description={"suggested_value": external} if external else None,
            ): PORT_SELECTOR,
            vol.Required(
                CONF_PROTOCOL, default=defaults.get(CONF_PROTOCOL, Protocol.TCP.value)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[p.value for p in Protocol],
                    translation_key=CONF_PROTOCOL,
                    mode=SelectSelectorMode.LIST,
                )
            ),
        }
    )
