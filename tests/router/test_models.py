"""PortForward validation."""

import pytest

from custom_components.tidy_home_router.router.errors import RouterError
from custom_components.tidy_home_router.router.models import PortForward, Protocol, validate_port


async def test_valid_forward():
    forward = PortForward(internal_ip="192.168.1.50", internal_port=8123, external_port=8123)
    assert forward.protocol is Protocol.TCP
    assert forward.summary == "tcp/8123 -> 192.168.1.50:8123"


async def test_rejects_bad_ip():
    with pytest.raises(RouterError, match="not a valid IPv4 address"):
        PortForward(internal_ip="192.168.1.999", internal_port=80, external_port=80)


async def test_rejects_hostname():
    with pytest.raises(RouterError, match="not a valid IPv4 address"):
        PortForward(internal_ip="nas.local", internal_port=80, external_port=80)


@pytest.mark.parametrize("port", [0, -1, 65536])
async def test_rejects_out_of_range_ports(port):
    with pytest.raises(RouterError, match="out of range"):
        PortForward(internal_ip="192.168.1.50", internal_port=port, external_port=80)
    with pytest.raises(RouterError, match="out of range"):
        validate_port(port)


async def test_external_port_may_differ():
    forward = PortForward(
        internal_ip="192.168.1.50",
        internal_port=22,
        external_port=2222,
        protocol=Protocol.BOTH,
    )
    assert forward.summary == "both/2222 -> 192.168.1.50:22"
