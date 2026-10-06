"""Setup, polling, the entities and the clear action, against the fake router."""

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.const import CONF_NAME, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.home_router.const import (
    CONF_EXTERNAL_PORT,
    CONF_INTERNAL_IP,
    CONF_INTERNAL_PORT,
    CONF_PROTOCOL,
    DOMAIN,
    EVENT_REBOOT_FINISHED,
    SUBENTRY_PORT_FORWARD,
)
from custom_components.home_router.router import PortForward, Protocol, RebootWait

from .conftest import ENTRY_DATA

SWITCH = "switch.ssh"
BUTTON = "button.quantum_fiber_c5500xk_restart"
SENSOR = "sensor.quantum_fiber_c5500xk_port_forwards"

SSH = {
    CONF_NAME: "ssh",
    CONF_INTERNAL_IP: "192.168.1.50",
    CONF_INTERNAL_PORT: 22,
    CONF_EXTERNAL_PORT: 2222,
    CONF_PROTOCOL: "both",
}


def live(name="ssh", external=2222, protocol=Protocol.TCP, key="k1", internal_port=22):
    return PortForward(
        internal_ip="192.168.1.50",
        internal_port=internal_port,
        external_port=external,
        protocol=protocol,
        name=name,
        key=key,
    )


@pytest.fixture
async def loaded(hass: HomeAssistant, router) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Quantum Fiber C5500XK",
        unique_id="192.168.1.1",
        data=ENTRY_DATA,
        subentries_data=[
            ConfigSubentryData(
                data=SSH, subentry_type=SUBENTRY_PORT_FORWARD, title="ssh", unique_id="ssh"
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def poll(hass: HomeAssistant, seconds: int = 61) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


# -- setup -------------------------------------------------------------------


async def test_setup_and_unload(hass: HomeAssistant, router, loaded) -> None:
    assert loaded.state is ConfigEntryState.LOADED
    assert hass.states.get(SWITCH).state == STATE_OFF
    assert hass.states.get(BUTTON) is not None
    assert hass.states.get(SENSOR).state == "0"

    assert await hass.config_entries.async_unload(loaded.entry_id)
    assert router.closed


async def test_setup_retries_when_unreachable(hass: HomeAssistant, router, entry) -> None:
    router.reachable = False
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_bad_password_starts_reauth(hass: HomeAssistant, router, entry) -> None:
    router.password_ok = False
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    (flow,) = hass.config_entries.flow.async_progress()
    assert flow["context"]["source"] == "reauth"


# -- polling -----------------------------------------------------------------


async def test_expired_session_logs_in_again(hass: HomeAssistant, router, loaded) -> None:
    router.session_valid = False
    router.forwards = [live()]
    await poll(hass)
    assert router.logins == 2
    assert hass.states.get(SWITCH).state == STATE_ON


async def test_unreachable_router_makes_entities_unavailable(
    hass: HomeAssistant, router, loaded
) -> None:
    router.reachable = False
    await poll(hass)
    assert hass.states.get(SWITCH).state == STATE_UNAVAILABLE
    router.reachable = True
    await poll(hass)
    assert hass.states.get(SWITCH).state == STATE_OFF


async def test_password_changed_on_router_starts_reauth(
    hass: HomeAssistant, router, loaded
) -> None:
    router.session_valid = False
    router.password_ok = False
    await poll(hass)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


# -- switch ------------------------------------------------------------------


async def test_switch_on_creates_the_rule(hass: HomeAssistant, router, loaded) -> None:
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    await hass.async_block_till_done()
    assert [(f.name, f.protocol, f.external_port) for f in router.forwards] == [
        ("ssh", Protocol.TCP, 2222),
        ("ssh", Protocol.UDP, 2222),
    ]
    state = hass.states.get(SWITCH)
    assert state.state == STATE_ON
    assert state.attributes["mismatch"] is False
    assert hass.states.get(SENSOR).state == "2"


async def test_switch_off_removes_the_rule(hass: HomeAssistant, router, loaded) -> None:
    router.forwards = [live(), live(protocol=Protocol.UDP, key="k2"), live("web", 80, key="k3")]
    await poll(hass)
    assert hass.states.get(SWITCH).state == STATE_ON

    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    await hass.async_block_till_done()
    assert [f.name for f in router.forwards] == ["web"]
    assert hass.states.get(SWITCH).state == STATE_OFF


async def test_switch_on_refuses_a_taken_port(hass: HomeAssistant, router, loaded) -> None:
    router.forwards = [live("other", 2222)]
    with pytest.raises(HomeAssistantError, match="already forwarded"):
        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    assert len(router.forwards) == 1


async def test_switch_flags_a_rule_edited_elsewhere(hass: HomeAssistant, router, loaded) -> None:
    router.forwards = [live(internal_port=2200)]
    await poll(hass)
    state = hass.states.get(SWITCH)
    assert state.state == STATE_ON
    assert state.attributes["mismatch"] is True


async def test_removing_the_subentry_removes_the_switch_and_the_rule(
    hass: HomeAssistant, router, loaded
) -> None:
    router.forwards = [live(), live(protocol=Protocol.UDP, key="k2"), live("web", 80, key="k3")]
    await poll(hass)
    (subentry,) = loaded.subentries.values()
    hass.config_entries.async_remove_subentry(loaded, subentry.subentry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH) is None
    assert [f.name for f in router.forwards] == ["web"]
    assert loaded.state is ConfigEntryState.LOADED


async def test_removing_the_subentry_still_works_when_router_is_unreachable(
    hass: HomeAssistant, router, loaded, caplog
) -> None:
    router.forwards = [live()]
    await poll(hass)
    router.reachable = False
    (subentry,) = loaded.subentries.values()
    hass.config_entries.async_remove_subentry(loaded, subentry.subentry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH) is None
    assert "Could not remove port forward ssh" in caplog.text


async def test_port_forwards_get_their_own_device(hass: HomeAssistant, router, loaded) -> None:
    """The router device must stay off the subentries, or the UI hides it under them."""
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    (subentry,) = loaded.subentries.values()
    router_device = devices.async_get_device(identifiers={(DOMAIN, loaded.entry_id)})
    assert router_device.config_entries_subentries == {loaded.entry_id: {None}}

    switch = entities.async_get(SWITCH)
    assert switch.config_subentry_id == subentry.subentry_id
    forward_device = devices.async_get(switch.device_id)
    assert forward_device.id != router_device.id
    assert forward_device.via_device_id == router_device.id
    assert forward_device.name == "ssh"
    assert entities.async_get(BUTTON).device_id == router_device.id


async def test_router_device_is_detached_from_subentries_on_setup(
    hass: HomeAssistant, router
) -> None:
    """Installs from before the fix have the router device linked to each subentry."""
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
    (subentry,) = entry.subentries.values()
    devices = dr.async_get(hass)
    for subentry_id in (None, subentry.subentry_id):
        devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=subentry_id,
            identifiers={(DOMAIN, entry.entry_id)},
        )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    router_device = devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert router_device.config_entries_subentries == {entry.entry_id: {None}}


# -- reboot ------------------------------------------------------------------


async def test_reboot_button(hass: HomeAssistant, router, loaded) -> None:
    events = async_capture_events(hass, EVENT_REBOOT_FINISHED)
    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    await hass.async_block_till_done()
    assert router.reboots == 1
    assert [e.data["result"] for e in events] == [RebootWait.RECOVERED.value]
    assert events[0].data["config_entry_id"] == loaded.entry_id
    # The reboot ended the session; the refresh after it logged in again.
    assert router.logins == 2
    assert hass.states.get(BUTTON).state != STATE_UNAVAILABLE


async def test_reboot_reports_a_router_that_never_went_down(
    hass: HomeAssistant, router, loaded
) -> None:
    router.reboot_outcome = RebootWait.NEVER_WENT_DOWN
    events = async_capture_events(hass, EVENT_REBOOT_FINISHED)
    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    await hass.async_block_till_done()
    assert [e.data["result"] for e in events] == ["never_went_down"]


# -- clear action ------------------------------------------------------------


async def test_clear_port_forwards(hass: HomeAssistant, router, loaded) -> None:
    router.forwards = [live(), live("web", 80, key="k2")]
    response = await hass.services.async_call(
        DOMAIN, "clear_port_forwards", {}, blocking=True, return_response=True
    )
    assert response == {"removed": 2}
    assert router.forwards == []
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "0"


async def test_clear_port_forwards_with_unknown_entry(hass: HomeAssistant, router, loaded) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "clear_port_forwards", {"config_entry_id": "nope"}, blocking=True
        )
