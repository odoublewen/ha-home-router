"""Waiting through a reboot.

Two regressions these guard, both seen on real hardware:

* The router keeps serving for 5-10 seconds after it accepts the reboot command, so
  polling only for "is it up again" reports success having never seen it leave.
* Its web server answers long before the device is usable, so a probe that accepts
  any HTTP reply calls the router recovered while the UI is still dead.
"""

import ssl

import httpx
import pytest

from custom_components.tidy_home_router.router import base
from custom_components.tidy_home_router.router.base import Router
from custom_components.tidy_home_router.router.models import RebootWait, RouterConfig


class FakeClock:
    """Stands in for time.monotonic/asyncio.sleep so timeouts are deterministic."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class ScriptedRouter(Router):
    """Answers probes from a script of booleans; the last value repeats forever."""

    model = "scripted"
    display_name = "Scripted Router"

    def __init__(self, script):
        super().__init__(
            RouterConfig(host="192.168.1.1", username="admin", password="secret"),
            ssl_context=ssl.create_default_context(),
        )
        self.script = list(script)
        self.probes = 0

    async def _probe(self, client):
        self.probes += 1
        return self.script[min(self.probes - 1, len(self.script) - 1)]

    async def login(self): ...
    async def list_forwards(self):
        return []

    async def add_forward(self, forward): ...
    async def remove_forward(self, forward): ...
    async def remove_by_name(self, name):
        return 0

    async def clear_forwards(self):
        return 0

    async def reboot(self): ...


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(base.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(base.asyncio, "sleep", clock.sleep)
    return clock


async def test_lingering_router_is_not_mistaken_for_a_recovery():
    """Up for the first polls, then down, then back."""
    router = ScriptedRouter([True] * 2 + [False] * 3 + [True] * 3)
    assert await router.wait_for_reboot(interval=5.0) is RebootWait.RECOVERED
    # It cannot have settled during the opening "still up" replies.
    assert router.probes == 8


async def test_one_failed_probe_is_not_proof_the_router_went_down():
    """A transient blip while the router is still serving must not count as offline."""
    router = ScriptedRouter([True, False, True])
    assert await router.wait_for_reboot(down_timeout=60.0, interval=5.0) is (
        RebootWait.NEVER_WENT_DOWN
    )


async def test_one_successful_probe_is_not_proof_the_router_is_back():
    """The web server can answer mid-boot before the device is usable."""
    router = ScriptedRouter([False] * 3 + [True, False] + [True] * 3)
    assert await router.wait_for_reboot(interval=5.0) is RebootWait.RECOVERED
    assert router.probes == 8


async def test_router_that_never_goes_down_is_reported():
    router = ScriptedRouter([True])
    assert await router.wait_for_reboot(down_timeout=30.0, interval=5.0) is (
        RebootWait.NEVER_WENT_DOWN
    )


async def test_router_that_never_comes_back_is_reported():
    router = ScriptedRouter([False])
    assert await router.wait_for_reboot(up_timeout=30.0, interval=5.0) is RebootWait.STILL_DOWN


async def test_streaks_are_configurable():
    router = ScriptedRouter([False, True])
    outcome = await router.wait_for_reboot(interval=5.0, down_streak=1, up_streak=1)
    assert outcome is RebootWait.RECOVERED
    assert router.probes == 2


async def test_down_timeout_is_honoured(fake_clock):
    router = ScriptedRouter([True])
    await router.wait_for_reboot(down_timeout=30.0, interval=5.0)
    assert fake_clock.now == pytest.approx(30.0, abs=5.0)


async def test_up_timeout_is_honoured(fake_clock):
    router = ScriptedRouter([False])
    await router.wait_for_reboot(down_timeout=30.0, up_timeout=60.0, interval=5.0)
    # The down phase settles immediately, so only the up phase burns its budget.
    assert fake_clock.now == pytest.approx(75.0, abs=10.0)


# -- the default probe -------------------------------------------------------


def client_returning(handler):
    return httpx.AsyncClient(base_url="https://192.168.1.1", transport=httpx.MockTransport(handler))


async def default_probe(handler):
    router = ScriptedRouter([])
    return await Router._probe(router, client_returning(handler))


async def test_default_probe_reports_unreachable_as_offline():
    def handler(request):
        raise httpx.ConnectError("network is unreachable")

    assert await default_probe(handler) is False


async def test_default_probe_counts_any_reply_as_online():
    assert await default_probe(lambda request: httpx.Response(302)) is True
