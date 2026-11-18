"""Helpers of src/cli/node.py used by node maintenance: the key that names
a cluster and the privilege check run before anything moves.

Protects maintenance from starting with a user that cannot finish it, and
the maintenance record from mixing two clusters or two standalone nodes.
"""

from __future__ import annotations

import pytest

from src.api.client import ProxmoxClient
from src.cli.node import _cluster_key, _missing_privileges
from src.models.config import ProfileConfig
from tests.fake_api import fixture


@pytest.fixture
def client(api) -> ProxmoxClient:
    return ProxmoxClient(ProfileConfig(
        host="h1", verify_ssl=False,
        auth={"type": "password", "user": "ops@pve", "password": "x"},
    ))


# ── _cluster_key ─────────────────────────────────────────────────────────


async def test_cluster_key_of_a_cluster(client, api):
    api.route("GET", "/cluster/status", fixture("pve-9.1/cluster_status"))
    assert await _cluster_key(client) == "sdnlab"


async def test_cluster_key_of_a_standalone_node(client, api):
    api.route("GET", "/cluster/status", fixture("pve-9.2/cluster_status"))
    key = await _cluster_key(client)
    assert key.startswith("standalone-") and "pve-lab9" in key


async def test_cluster_key_standalone_never_equals_a_cluster_of_the_same_name(client, api):
    api.route("GET", "/cluster/status", [{"type": "node", "name": "sdnlab", "local": 1}])
    standalone = await _cluster_key(client)
    api.route("GET", "/cluster/status", fixture("pve-9.1/cluster_status"))
    assert standalone != await _cluster_key(client)


# ── _missing_privileges ──────────────────────────────────────────────────

GUESTS = [{"vmid": 100}, {"vmid": 101}, {"vmid": 1000}]


def permissions(table: dict[str, dict]):
    """GET /access/permissions?path=X answers {X: privileges} like PVE."""

    def respond(call):
        path = (call.params or {}).get("path")
        return {path: table.get(path, {})} if path else table

    return respond


async def test_nothing_missing(client, api):
    api.route("GET", "/access/permissions", permissions({
        "/": {"Sys.Modify": 1, "Sys.Console": 1, "Sys.Audit": 1},
        "/vms/100": {"VM.Migrate": 1}, "/vms/101": {"VM.Migrate": 1}, "/vms/1000": {"VM.Migrate": 1},
    }))
    assert await _missing_privileges(client, GUESTS, ceph_flag=True, ha_failback=True) == []


async def test_each_missing_privilege_is_named(client, api):
    api.route("GET", "/access/permissions", permissions({
        "/": {"Sys.Audit": 1},
        "/vms/100": {"VM.Migrate": 1},
        # /vms/101 is not granted, /vms/1000 only has power management
        "/vms/1000": {"VM.PowerMgmt": 1},
    }))
    missing = await _missing_privileges(client, GUESTS, ceph_flag=True, ha_failback=True)
    assert len(missing) == 3
    assert missing[0].startswith("Sys.Modify on /")
    assert missing[1].startswith("Sys.Console on /")
    assert missing[2] == "VM.Migrate on 101, 1000"


async def test_options_not_used_need_no_privilege(client, api):
    api.route("GET", "/access/permissions", permissions({
        "/vms/100": {"VM.Migrate": 1}, "/vms/101": {"VM.Migrate": 1}, "/vms/1000": {"VM.Migrate": 1},
    }))
    assert await _missing_privileges(client, GUESTS, ceph_flag=False, ha_failback=False) == []


async def test_privileges_are_read_per_guest_path(client, api):
    # A right on /vms/100 says nothing about /vms/1000 or /vms/10
    api.route("GET", "/access/permissions", permissions({"/": {}, "/vms/100": {"VM.Migrate": 1}}))
    guests = [{"vmid": 100}, {"vmid": 10}, {"vmid": 1000}]
    missing = await _missing_privileges(client, guests, ceph_flag=False, ha_failback=False)
    assert missing == ["VM.Migrate on 10, 1000"]
    asked = sorted(c.params["path"] for c in api.called("GET", "/access/permissions"))
    assert asked == ["/", "/vms/10", "/vms/100", "/vms/1000"]


async def test_no_guest_only_checks_root(client, api):
    api.route("GET", "/access/permissions", permissions({"/": {"Sys.Modify": 1}}))
    assert await _missing_privileges(client, [], ceph_flag=True, ha_failback=False) == []


async def test_cluster_key_two_standalone_hosts_with_the_same_name(api):
    def standalone(host):
        return ProxmoxClient(ProfileConfig(
            host=host, verify_ssl=False,
            auth={"type": "password", "user": "root@pam", "password": "x"},
        ))

    for host, ip in (("h1", "192.168.1.10"), ("h2", "10.20.0.10")):
        api.route("GET", "/cluster/status",
                  [{"id": "node/pve", "ip": ip, "local": 1, "name": "pve", "nodeid": 0,
                    "online": 1, "type": "node"}], host=host)
    assert await _cluster_key(standalone("h1")) != await _cluster_key(standalone("h2"))
