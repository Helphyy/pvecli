"""Helpers of src/cli/find.py: query classification, size and age parsing,
net line parsing and IP matching.

Protects what `pvecli find` thinks a query is, and the rule that an address
or network matches exactly (a neighbour address must not match).
"""

from __future__ import annotations

import ipaddress

import pytest
import typer

from src.cli.find import _classify, _ip_in_cidr, _ip_matches, _kv, _nic_mac, _parse_age, _parse_size
from tests.fake_api import fixture

# ── _classify ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("query, kind, value", [
    ("10.0.0.5", "ip", ipaddress.ip_address("10.0.0.5")),
    (" 10.0.0.5 ", "ip", ipaddress.ip_address("10.0.0.5")),
    ("2001:db8::1", "ip", ipaddress.ip_address("2001:db8::1")),
    ("10.0.0.0/24", "net", ipaddress.ip_network("10.0.0.0/24")),
    # A host address with a prefix is taken as its network
    ("10.0.0.5/24", "net", ipaddress.ip_network("10.0.0.0/24")),
    ("2001:db8::/64", "net", ipaddress.ip_network("2001:db8::/64")),
    ("bc:24:11:f8:dd:82", "mac", "BC:24:11:F8:DD:82"),
    ("BC-24-11-F8-DD-82", "mac", "BC:24:11:F8:DD:82"),
    ("105", "vmid", 105),
    ("vm-105-disk-1", "volume", "vm-105-disk-1"),
    ("ceph:vm-105-disk-1", "volume", "ceph:vm-105-disk-1"),
    ("local:105/vm-105-disk-0.qcow2", "volume", "local:105/vm-105-disk-0.qcow2"),
    ("local-zfs:subvol-200-disk-0", "volume", "local-zfs:subvol-200-disk-0"),
    ("local-lvm:base-9000-disk-0", "volume", "local-lvm:base-9000-disk-0"),
    ("WebServer", "text", "webserver"),
])
def test_classify(query, kind, value):
    assert _classify(query) == (kind, value)


@pytest.mark.parametrize("query", [
    "10.0.0",            # not an IP: text
    "bc:24:11:f8:dd",    # 5 octets: not a MAC
    "bc:24:11:f8:dd:8",  # last octet too short
    "my-vm-105-disk",    # 'vm-' not at a volume boundary
    "prod/web",          # a slash alone does not make a network
])
def test_classify_near_misses_are_text(query):
    assert _classify(query)[0] == "text"


# ── _parse_size / _parse_age ─────────────────────────────────────────────


@pytest.mark.parametrize("text, expected", [
    ("1024", 1024),
    ("8G", 8 * 1024**3),
    ("8g", 8 * 1024**3),
    ("8GB", 8 * 1024**3),
    ("8GiB", 8 * 1024**3),
    ("512M", 512 * 1024**2),
    ("1.5G", 1536 * 1024**2),
    ("1T", 1024**4),
    ("4K", 4096),
])
def test_parse_size(text, expected):
    assert _parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "G", "8X", "-1G", "8 GB extra", "1P"])
def test_parse_size_rejects(text):
    with pytest.raises(typer.BadParameter):
        _parse_size(text)


@pytest.mark.parametrize("text, expected", [
    ("12h", 12 * 3600),
    ("30d", 30 * 86400),
    ("30D", 30 * 86400),
    ("2w", 14 * 86400),
])
def test_parse_age(text, expected):
    assert _parse_age(text) == expected


@pytest.mark.parametrize("text", ["30", "1m", "1.5d", "d", "", "-2d"])
def test_parse_age_rejects(text):
    with pytest.raises(typer.BadParameter):
        _parse_age(text)


# ── _kv / _nic_mac ───────────────────────────────────────────────────────


def test_kv_qemu_net_line():
    assert _kv("virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,tag=42,firewall=1") == {
        "virtio": "BC:24:11:AA:BB:CC", "bridge": "vmbr0", "tag": "42", "firewall": "1",
    }


def test_kv_first_bare_word():
    assert _kv("local-lvm:vm-100-disk-0,size=8G") == {"": "local-lvm:vm-100-disk-0", "size": "8G"}
    # Only the first bare word is kept
    assert _kv("a,b,c=1")[""] == "a"


def test_nic_mac_qemu():
    assert _nic_mac(_kv("virtio=bc:24:11:aa:bb:cc,bridge=vmbr0")) == "BC:24:11:AA:BB:CC"
    assert _nic_mac(_kv("e1000=BC:24:11:AA:BB:01,bridge=vmbr0")) == "BC:24:11:AA:BB:01"
    assert _nic_mac(_kv("vmxnet3=BC:24:11:AA:BB:02,bridge=vmbr0")) == "BC:24:11:AA:BB:02"


def test_nic_mac_lxc_from_real_config():
    net0 = fixture("pve-9.2/lxc_config")["net0"]
    assert _nic_mac(_kv(net0)) == "BC:24:11:F8:DD:82"


def test_nic_mac_absent():
    assert _nic_mac(_kv("bridge=vmbr0,tag=42")) is None
    assert _nic_mac({}) is None


def test_mac_query_matches_config_mac_whatever_the_case_or_separator():
    _, target = _classify("bc-24-11-f8-dd-82")
    assert _nic_mac(_kv("name=eth0,hwaddr=BC:24:11:F8:DD:82")) == target
    _, other = _classify("bc-24-11-f8-dd-83")
    assert _nic_mac(_kv("name=eth0,hwaddr=BC:24:11:F8:DD:82")) != other


# ── _ip_matches / _ip_in_cidr ────────────────────────────────────────────


def ip(v):
    return ipaddress.ip_address(v)


def net(v):
    return ipaddress.ip_network(v, strict=False)


def test_ip_matches_an_exact_address():
    assert _ip_matches("10.0.0.5", "ip", ip("10.0.0.5"))
    # An ipconfig value carries its prefix
    assert _ip_matches("10.0.0.5/24", "ip", ip("10.0.0.5"))
    assert not _ip_matches("10.0.0.50", "ip", ip("10.0.0.5"))
    assert not _ip_matches("10.0.0.6", "ip", ip("10.0.0.5"))
    assert _ip_matches("2001:db8::1", "ip", ip("2001:db8::1"))
    assert not _ip_matches("2001:db8::10", "ip", ip("2001:db8::1"))


def test_ip_matches_inside_a_network():
    assert _ip_matches("10.0.0.255", "net", net("10.0.0.0/24"))
    assert not _ip_matches("10.0.1.0", "net", net("10.0.0.0/24"))
    # Another IP version never matches
    assert not _ip_matches("::ffff:10.0.0.5", "net", net("10.0.0.0/24"))


@pytest.mark.parametrize("value", ["dhcp", "manual", "", "not-an-ip", None])
def test_ip_matches_ignores_non_addresses(value):
    assert not _ip_matches(value, "ip", ip("10.0.0.5"))


def test_ip_in_cidr_for_an_address():
    assert _ip_in_cidr("10.0.0.0/24", "ip", ip("10.0.0.5"))
    assert not _ip_in_cidr("10.0.1.0/24", "ip", ip("10.0.0.5"))
    # A firewall alias holding a single host
    assert _ip_in_cidr("10.0.0.5", "ip", ip("10.0.0.5"))
    assert not _ip_in_cidr("10.0.0.6", "ip", ip("10.0.0.5"))


def test_ip_in_cidr_for_a_network_is_overlap():
    assert _ip_in_cidr("10.0.0.0/16", "net", net("10.0.5.0/24"))
    assert _ip_in_cidr("10.0.5.0/28", "net", net("10.0.5.0/24"))
    assert not _ip_in_cidr("10.1.0.0/16", "net", net("10.0.5.0/24"))
    assert not _ip_in_cidr("2001:db8::/32", "net", net("10.0.5.0/24"))


@pytest.mark.parametrize("cidr", ["", "garbage", "10.0.0.0/33"])
def test_ip_in_cidr_ignores_invalid(cidr):
    assert not _ip_in_cidr(cidr, "ip", ip("10.0.0.5"))
