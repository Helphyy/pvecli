"""DHCP range parsing: IPv6 addresses contain the IPv4 separator."""

import pytest

from src.cli.sdn import _split_ip_range


@pytest.mark.parametrize("text, expected", [
    ("10.0.0.10:10.0.0.20", ("10.0.0.10", "10.0.0.20")),
    ("10.0.0.10-10.0.0.20", ("10.0.0.10", "10.0.0.20")),
    ("fd00::10:fd00::20", ("fd00::10", "fd00::20")),
    ("fd00::10-fd00::20", ("fd00::10", "fd00::20")),
    ("fd00::1:2-fd00::3", ("fd00::1:2", "fd00::3")),
])
def test_range_is_split_into_two_addresses(text, expected):
    assert _split_ip_range(text) == expected


@pytest.mark.parametrize("text", ["10.0.0.1:fd00::1", "10.0.0.1", "a:b", ""])
def test_invalid_range_is_refused(text):
    assert _split_ip_range(text) is None
