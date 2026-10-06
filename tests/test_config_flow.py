"""Config flow, reauth, options and the port-forward subentry flow."""

from homeassistant import config_entries
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_router.const import (
    CONF_EXTERNAL_PORT,
    CONF_INTERNAL_IP,
    CONF_INTERNAL_PORT,
    CONF_PROTOCOL,
    DOMAIN,
    SUBENTRY_PORT_FORWARD,
)
from custom_components.home_router.router import PortForward

from .conftest import ENTRY_DATA

SSH = {
    CONF_NAME: "ssh",
    CONF_INTERNAL_IP: "192.168.1.50",
    CONF_INTERNAL_PORT: 22,
    CONF_EXTERNAL_PORT: 2222,
    CONF_PROTOCOL: "tcp",
}


async def start_user_flow(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_creates_entry(hass: HomeAssistant, router) -> None:
    result = await start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Quantum Fiber C5500XK"
    assert result["data"] == ENTRY_DATA
    assert result["result"].unique_id == "192.168.1.1"
    assert router.closed


async def test_user_flow_bad_password(hass: HomeAssistant, router) -> None:
    router.password_ok = False
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    router.password_ok = True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_unreachable(hass: HomeAssistant, router) -> None:
    router.reachable = False
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_rejects_duplicate_host(hass: HomeAssistant, router, entry) -> None:
    entry.add_to_hass(hass)
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(hass: HomeAssistant, router, entry) -> None:
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    router.password_ok = False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    router.password_ok = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-secret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"


async def test_options_flow_sets_poll_interval(hass: HomeAssistant, router, entry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SCAN_INTERVAL: 120}


# -- port-forward subentries --------------------------------------------------


async def start_subentry_flow(hass: HomeAssistant, entry: MockConfigEntry):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_PORT_FORWARD), context={"source": config_entries.SOURCE_USER}
    )


async def test_add_port_forward(hass: HomeAssistant, router, entry) -> None:
    entry.add_to_hass(hass)
    result = await start_subentry_flow(hass, entry)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], SSH)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert subentry.title == "ssh"
    assert dict(subentry.data) == SSH


async def test_external_port_defaults_to_internal(hass: HomeAssistant, router, entry) -> None:
    entry.add_to_hass(hass)
    result = await start_subentry_flow(hass, entry)
    without_external = {k: v for k, v in SSH.items() if k != CONF_EXTERNAL_PORT}
    await hass.config_entries.subentries.async_configure(result["flow_id"], without_external)
    (subentry,) = entry.subentries.values()
    assert subentry.data[CONF_EXTERNAL_PORT] == 22


async def test_port_forward_validation(hass: HomeAssistant, router, entry) -> None:
    entry.add_to_hass(hass)
    result = await start_subentry_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**SSH, CONF_NAME: "my rule", CONF_INTERNAL_IP: "nas.local"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NAME: "invalid_name", CONF_INTERNAL_IP: "invalid_ip"}


async def test_port_forward_names_are_unique(hass: HomeAssistant, router) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        subentries_data=[
            ConfigSubentryData(
                data=SSH, subentry_type=SUBENTRY_PORT_FORWARD, title="ssh", unique_id="ssh"
            )
        ],
    )
    entry.add_to_hass(hass)
    result = await start_subentry_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], SSH)
    assert result["errors"] == {CONF_NAME: "name_exists"}


async def reconfigure(hass: HomeAssistant, entry: MockConfigEntry):
    (subentry,) = entry.subentries.values()
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_PORT_FORWARD),
        context={"source": config_entries.SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )


def entry_with_ssh() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        subentries_data=[
            ConfigSubentryData(
                data=SSH, subentry_type=SUBENTRY_PORT_FORWARD, title="ssh", unique_id="ssh"
            )
        ],
    )


async def test_reconfigure_port_forward(hass: HomeAssistant, router) -> None:
    entry = entry_with_ssh()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await reconfigure(hass, entry)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**SSH, CONF_EXTERNAL_PORT: 2200}
    )
    assert result["reason"] == "reconfigure_successful"
    (subentry,) = entry.subentries.values()
    assert subentry.data[CONF_EXTERNAL_PORT] == 2200


async def test_reconfigure_refused_while_rule_is_live(hass: HomeAssistant, router) -> None:
    router.forwards = [
        PortForward(
            internal_ip="192.168.1.50", internal_port=22, external_port=2222, name="ssh", key="k1"
        )
    ]
    entry = entry_with_ssh()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await reconfigure(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "rule_active"
