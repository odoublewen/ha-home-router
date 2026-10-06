"""C5500XK device logic, against a stubbed CGI API."""

import ssl

import httpx
import pytest

from custom_components.home_router.router.errors import AuthError, DeviceError, RouterError
from custom_components.home_router.router.models import PortForward, Protocol, RouterConfig
from custom_components.home_router.router.quantum_c5500xk import QuantumC5500XK, validate_rule_name


def port_mapping_payload(*instances):
    """Build a cgi_get reply for the given (index, params) pairs."""
    objects = [
        {
            "ObjName": f"Device.NAT.PortMapping.{index}.",
            "Param": [{"ParamName": k, "ParamValue": v} for k, v in params.items()],
        }
        for index, params in instances
    ]
    return {"Objects": objects}


RULE_ONE = {
    "Protocol": "TCP",
    "ExternalPort": "8123",
    "ExternalPortEndRange": "8123",
    "InternalPort": "8123",
    "InternalClient": "192.168.1.50",
    "Description": "PortMapping_1",
    "RemoteHost": "",
    "Enable": "1",
}
RULE_TWO = {**RULE_ONE, "Protocol": "UDP", "Description": "PortMapping_2"}


class FakeRouterAPI:
    """Minimal stand-in for the device's CGI endpoints."""

    def __init__(self, mappings=(), login_ok=True):
        self.mappings = list(mappings)
        self.login_ok = login_ok
        self.requests: list[tuple[str, str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        self.requests.append((request.method, request.url.path, body or str(request.url.query)))

        if request.url.path == "/cgi/cgi_get":
            query = request.url.query.decode()
            if "Device.UserInterface" in query:
                if not self.login_ok:
                    return httpx.Response(444)
                return httpx.Response(
                    200,
                    json={
                        "Objects": [
                            {
                                "ObjName": "Device.UserInterface.",
                                "Param": [{"ParamName": "PasswordRequired", "ParamValue": "1"}],
                            }
                        ]
                    },
                )
            return httpx.Response(200, json=port_mapping_payload(*self.mappings))

        if request.url.path == "/cgi/cgi_action":
            return httpx.Response(200 if self.login_ok else 401)

        if request.url.path == "/cgi/cgi_set":
            if "Operation=Del" in body:
                self.mappings = []
            return httpx.Response(200, text="")

        return httpx.Response(404)


def new_router() -> QuantumC5500XK:
    return QuantumC5500XK(
        RouterConfig(host="192.168.1.1", username="admin", password="secret"),
        ssl_context=ssl.create_default_context(),
    )


def build_router(api: FakeRouterAPI) -> QuantumC5500XK:
    router = new_router()
    router._client = httpx.AsyncClient(
        base_url="https://192.168.1.1",
        transport=httpx.MockTransport(api.handler),
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    return router


def sets(api: FakeRouterAPI) -> list[str]:
    """Bodies of every cgi_set request made."""
    return [body for method, path, body in api.requests if path == "/cgi/cgi_set"]


async def test_login_succeeds():
    api = FakeRouterAPI()
    router = build_router(api)
    await router.login()
    method, path, body = api.requests[0]
    assert (method, path) == ("POST", "/cgi/cgi_action")
    assert body == "username=admin&password=secret"


async def test_login_failure_raises():
    router = build_router(FakeRouterAPI(login_ok=False))
    with pytest.raises(AuthError):
        await router.login()


async def test_list_forwards_parses_instances():
    api = FakeRouterAPI([(1, RULE_ONE), (2, RULE_TWO)])
    forwards = await build_router(api).list_forwards()
    assert [f.name for f in forwards] == ["PortMapping_1", "PortMapping_2"]
    assert forwards[0].protocol is Protocol.TCP
    assert forwards[1].protocol is Protocol.UDP
    assert forwards[0].internal_ip == "192.168.1.50"
    assert forwards[0].external_port == 8123
    assert forwards[0].key == "Device.NAT.PortMapping.1."


async def test_list_forwards_sorts_numerically_not_lexically():
    api = FakeRouterAPI(
        [(10, {**RULE_ONE, "Description": "ten"}), (2, {**RULE_ONE, "Description": "two"})]
    )
    assert [f.name for f in await build_router(api).list_forwards()] == ["two", "ten"]


async def test_list_forwards_skips_the_container_object():
    api = FakeRouterAPI([(1, RULE_ONE)])
    api.mappings.append((0, {"SomeOtherParam": "x"}))
    assert len(await build_router(api).list_forwards()) == 1


async def test_add_forward_builds_the_expected_operation():
    api = FakeRouterAPI()
    router = build_router(api)
    await router.add_forward(
        PortForward(
            internal_ip="192.168.1.50", internal_port=8123, external_port=8123, name="PortMapping_1"
        )
    )
    (operation,) = sets(api)
    assert "Object=Device.NAT.PortMapping&Operation=Add" in operation
    assert "&RemoteHost=&X_AXON_INTERFACE=wan" in operation  # All IP Addresses
    assert "&ExternalPort=8123&ExternalPortEndRange=8123" in operation
    assert "&InternalPort=8123&InternalClient=192.168.1.50" in operation
    assert "&Description=PortMapping_1" in operation
    assert operation.endswith("&Protocol=TCP")


async def test_add_forward_with_both_protocols_sends_two_operations():
    api = FakeRouterAPI()
    router = build_router(api)
    await router.add_forward(
        PortForward(
            internal_ip="192.168.1.50",
            internal_port=22,
            external_port=2222,
            protocol=Protocol.BOTH,
            name="ssh",
        )
    )
    (operation,) = sets(api)
    first, second = operation.split(",")
    assert first.endswith("&Protocol=TCP")
    assert second.endswith("&Protocol=UDP")
    assert second.startswith("Object=Device.NAT.PortMapping&Operation=Add")


async def test_add_forward_continues_the_web_ui_numbering():
    api = FakeRouterAPI([(1, RULE_ONE), (2, RULE_TWO)])
    router = build_router(api)
    await router.add_forward(
        PortForward(internal_ip="192.168.1.60", internal_port=80, external_port=80)
    )
    assert "&Description=PortMapping_3" in sets(api)[0]


async def test_add_forward_rejects_unsafe_names():
    router = build_router(FakeRouterAPI())
    with pytest.raises(RouterError, match="may only contain"):
        await router.add_forward(
            PortForward(
                internal_ip="192.168.1.50",
                internal_port=80,
                external_port=80,
                name="my rule&Protocol=UDP",
            )
        )


async def test_clear_forwards_deletes_every_instance():
    api = FakeRouterAPI([(1, RULE_ONE), (2, RULE_TWO)])
    router = build_router(api)
    assert await router.clear_forwards() == 2
    (operation,) = sets(api)
    assert operation == (
        "Object=Device.NAT.PortMapping.1.&Operation=Del&,"
        "Object=Device.NAT.PortMapping.2.&Operation=Del&"
    )


async def test_clear_forwards_with_nothing_configured():
    api = FakeRouterAPI()
    assert await build_router(api).clear_forwards() == 0
    assert sets(api) == []


async def test_clear_forwards_raises_if_the_router_ignores_the_delete():
    api = FakeRouterAPI([(1, RULE_ONE)])

    def handler(request):
        if request.url.path == "/cgi/cgi_set":
            return httpx.Response(200, text="")  # accepted but nothing removed
        return api.handler(request)

    router = new_router()
    router._client = httpx.AsyncClient(
        base_url="https://192.168.1.1", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(DeviceError, match="did not remove"):
        await router.clear_forwards()


async def test_remove_forward_needs_a_key():
    router = build_router(FakeRouterAPI())
    with pytest.raises(DeviceError, match="no device key"):
        await router.remove_forward(
            PortForward(internal_ip="192.168.1.50", internal_port=80, external_port=80)
        )


async def test_reboot_posts_the_action():
    api = FakeRouterAPI()
    await build_router(api).reboot()
    assert ("POST", "/cgi/cgi_action", "Action=Reboot") in api.requests


async def test_expired_session_is_reported_as_auth_error():
    def handler(request):
        return httpx.Response(444)

    router = new_router()
    router._client = httpx.AsyncClient(
        base_url="https://192.168.1.1", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(AuthError):
        await router.list_forwards()


async def test_unreachable_router_is_reported_as_device_error():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    router = new_router()
    router._client = httpx.AsyncClient(
        base_url="https://192.168.1.1", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(DeviceError, match="Could not reach"):
        await router.list_forwards()


@pytest.mark.parametrize("name", ["PortMapping_1", "home-assistant", "ssh.box"])
async def test_accepted_rule_names(name):
    assert validate_rule_name(name) == name


@pytest.mark.parametrize("name", ["has space", "amp&ersand", "comma,rule", ""])
async def test_rejected_rule_names(name):
    with pytest.raises(RouterError):
        validate_rule_name(name)


# -- conflict detection ------------------------------------------------------


async def test_add_rejects_a_port_already_forwarded():
    api = FakeRouterAPI([(1, RULE_ONE)])  # tcp/8123
    router = build_router(api)
    with pytest.raises(DeviceError, match="already forwarded"):
        await router.add_forward(
            PortForward(internal_ip="192.168.1.60", internal_port=8123, external_port=8123)
        )
    assert sets(api) == []


async def test_add_conflict_message_names_the_offending_rule():
    api = FakeRouterAPI([(1, RULE_ONE)])
    with pytest.raises(DeviceError, match="by rule 'PortMapping_1'"):
        await build_router(api).add_forward(
            PortForward(internal_ip="192.168.1.60", internal_port=8123, external_port=8123)
        )


async def test_add_allows_the_same_port_on_a_different_protocol():
    api = FakeRouterAPI([(1, RULE_ONE)])  # tcp/8123
    router = build_router(api)
    await router.add_forward(
        PortForward(
            internal_ip="192.168.1.60",
            internal_port=8123,
            external_port=8123,
            protocol=Protocol.UDP,
        )
    )
    assert sets(api)[0].endswith("&Protocol=UDP")


async def test_add_both_conflicts_when_either_protocol_is_taken():
    api = FakeRouterAPI([(1, RULE_ONE)])  # tcp/8123 only
    with pytest.raises(DeviceError, match="already forwarded"):
        await build_router(api).add_forward(
            PortForward(
                internal_ip="192.168.1.60",
                internal_port=8123,
                external_port=8123,
                protocol=Protocol.BOTH,
            )
        )


async def test_add_allows_a_free_port():
    api = FakeRouterAPI([(1, RULE_ONE)])
    router = build_router(api)
    await router.add_forward(
        PortForward(internal_ip="192.168.1.60", internal_port=80, external_port=9090)
    )
    assert "&ExternalPort=9090" in sets(api)[0]


# -- readiness probe ---------------------------------------------------------
#
# During a reboot lighttpd serves "/" and static pages well before the management
# API exists. Verified against the device: "/" -> 302, "/login.html" -> 200, and
# "/cgi/cgi_get?..." -> 444 only once the CGI layer is alive.


async def probe_result(handler) -> bool:
    router = new_router()
    client = httpx.AsyncClient(
        base_url="https://192.168.1.1", transport=httpx.MockTransport(handler)
    )
    try:
        return await router._probe(client)
    finally:
        await client.aclose()


async def test_probe_is_ready_when_cgi_rejects_us():
    """444 comes from the auth layer, so the app is alive."""
    assert await probe_result(lambda request: httpx.Response(444)) is True


async def test_probe_is_ready_when_cgi_answers():
    assert await probe_result(lambda request: httpx.Response(200, json={"Objects": []})) is True


@pytest.mark.parametrize("status", [302, 404, 500, 503])
async def test_probe_is_not_ready_when_only_the_web_server_is_up(status):
    """The regression: a bare redirect or error page is not a usable router."""
    assert await probe_result(lambda request: httpx.Response(status)) is False


async def test_probe_is_not_ready_when_unreachable():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    assert await probe_result(handler) is False


async def test_probe_asks_the_cgi_layer_with_the_required_header():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        seen["header"] = request.headers.get("X-Requested-With")
        return httpx.Response(444)

    await probe_result(handler)
    assert seen["path"] == "/cgi/cgi_get"
    assert "Device.UserInterface" in seen["query"]
    assert seen["header"] == "XMLHttpRequest"


# -- remove by name ----------------------------------------------------------


async def test_remove_by_name_deletes_both_halves_of_a_tcp_udp_rule():
    api = FakeRouterAPI(
        [
            (1, {**RULE_ONE, "Description": "ssh"}),
            (2, {**RULE_TWO, "Description": "ssh"}),
            (3, {**RULE_ONE, "ExternalPort": "80", "Description": "web"}),
        ]
    )
    assert await build_router(api).remove_by_name("ssh") == 2
    (operation,) = sets(api)
    assert operation == (
        "Object=Device.NAT.PortMapping.1.&Operation=Del&,"
        "Object=Device.NAT.PortMapping.2.&Operation=Del&"
    )


async def test_remove_by_name_with_no_match_sends_nothing():
    api = FakeRouterAPI([(1, RULE_ONE)])
    assert await build_router(api).remove_by_name("ssh") == 0
    assert sets(api) == []
