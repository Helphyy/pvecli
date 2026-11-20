"""vm rdp: the user sent to the RDP client (no client is ever started)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.fake_api import fixture

GUESTS = [dict(g) for g in fixture("pve-9.1/cluster_resources_vm")]


class Stop(Exception):
    """Raised by the fake build_rdp_command once the user is known."""


@pytest.fixture
def sent(monkeypatch) -> dict[str, Any]:
    import getpass

    import src.rdp as rdp
    import src.utils.network as network

    seen: dict[str, Any] = {}

    def build(client_type, host, port, user, *args, **kwargs):
        seen["user"] = user
        raise Stop

    monkeypatch.setattr(rdp, "detect_rdp_client", lambda: ("xfreerdp3", "/usr/bin/xfreerdp3"))
    monkeypatch.setattr(rdp, "build_rdp_command", build)
    monkeypatch.setattr(network, "resolve_vm_ip", _ip)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "secret")
    return seen


async def _ip(client, node, vmid):
    return "10.0.0.5"


def running_vm(api) -> int:
    guests = [dict(g) for g in GUESTS]
    vm = next(g for g in guests if g["type"] == "qemu")
    vm["status"] = "running"
    api.route("GET", "/cluster/resources", guests)
    api.route("GET", "/nodes/{node}/qemu/{vmid}/agent/get-osinfo", {"result": {"id": "mswindows"}})
    return vm["vmid"]


def test_rdp_sends_administrator_when_the_profile_has_no_user(cli, api, sent):
    vmid = running_vm(api)
    with pytest.raises(Stop):
        cli("vm", "rdp", str(vmid), "-d", "", "-p", "p1")
    assert sent["user"] == "Administrator"


def test_rdp_user_option_wins(cli, api, sent):
    vmid = running_vm(api)
    with pytest.raises(Stop):
        cli("vm", "rdp", str(vmid), "-u", "alice", "-d", "", "-p", "p1")
    assert sent["user"] == "alice"
