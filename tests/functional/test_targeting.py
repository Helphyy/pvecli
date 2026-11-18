"""Targeting VMs by --tag/--pool/--node: shown selection, confirmation, exec arguments."""

from __future__ import annotations

import pytest

from tests.fake_api import upid

RESOURCES = [
    {"vmid": 100, "type": "qemu", "node": "pve1", "status": "stopped", "name": "web1", "tags": "x;linux", "pool": "P"},
    {"vmid": 101, "type": "qemu", "node": "pve2", "status": "stopped", "name": "web2", "tags": "x", "pool": "Q"},
    {"vmid": 102, "type": "qemu", "node": "pve1", "status": "stopped", "name": "db", "tags": "y", "pool": "P"},
    {"vmid": 103, "type": "qemu", "node": "pve1", "status": "stopped", "name": "tpl", "tags": "x", "template": 1},
    {"vmid": 200, "type": "lxc", "node": "pve1", "status": "stopped", "name": "ct", "tags": "x"},
]


def _running(resources: list[dict]) -> list[dict]:
    return [{**r, "status": "running"} for r in resources]


@pytest.fixture
def lab(api):
    api.route("GET", "/cluster/resources", RESOURCES)
    api.route("GET", "/nodes/{node}/qemu/{vmid}/status/current", {"status": "stopped"})
    api.route("POST", "/nodes/{node}/qemu/{vmid}/status/start",
              lambda c: upid(c.match["node"], "qmstart", c.match["vmid"]))
    return api


def _started(api) -> list[str]:
    return [c.path for c in api.writes()]


def test_start_by_tag_shows_selection_and_starts_only_matching_vms(cli, lab):
    r = cli("vm", "start", "--tag", "x", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    # Selection printed: VMs only, template excluded, container excluded
    assert "2 VM(s) matching tag=x" in r.output
    assert "100 (web1)" in r.output and "101 (web2)" in r.output
    assert _started(lab) == [
        "/nodes/pve1/qemu/100/status/start",
        "/nodes/pve2/qemu/101/status/start",
    ]


def test_start_by_pool_and_node_combines_filters(cli, lab):
    r = cli("vm", "start", "--pool", "P", "--node", "pve1", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "pool=P" in r.output and "node=pve1" in r.output
    assert _started(lab) == [
        "/nodes/pve1/qemu/100/status/start",
        "/nodes/pve1/qemu/102/status/start",
    ]


def test_ids_with_a_filter_narrow_the_selection(cli, lab):
    r = cli("vm", "start", "100,102", "--tag", "x", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _started(lab) == ["/nodes/pve1/qemu/100/status/start"]


def test_filter_without_match_fails_without_writing(cli, lab):
    r = cli("vm", "start", "--tag", "nope", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "No VM matches tag=nope" in r.output
    assert lab.writes() == []


def test_start_by_tag_without_yes_off_terminal_refuses(cli, lab):
    # No terminal and no -y: the confirmation cannot be answered, nothing starts
    r = cli("vm", "start", "--tag", "x", "-p", "p1", input="")
    assert r.exit_code != 0
    assert "2 VM(s) matching tag=x" in r.output
    assert lab.writes() == []


def test_start_by_tag_declined_confirmation_writes_nothing(cli, lab):
    r = cli("vm", "start", "--tag", "x", "-p", "p1", input="n\n")
    assert lab.writes() == []
    assert "Cancel" in r.output or "cancel" in r.output


def test_exec_by_tag_does_not_take_the_command_for_a_vmid(cli, api):
    api.route("GET", "/cluster/resources", _running(RESOURCES))
    api.route("GET", "/nodes/{node}/qemu/{vmid}/config", {"ostype": "l26"})
    api.route("POST", "/nodes/{node}/qemu/{vmid}/agent/exec", {"pid": 42})
    api.route("GET", "/nodes/{node}/qemu/{vmid}/agent/exec-status",
              {"exited": 1, "exitcode": 0, "out-data": "up 3 days\n"})
    r = cli("vm", "exec", "--tag", "x", "-y", "-p", "p1", "--", "uptime")
    assert r.exit_code == 0, r.output
    assert "Invalid" not in r.output
    execs = api.called("POST", "/nodes/{node}/qemu/{vmid}/agent/exec")
    assert [c.path for c in execs] == [
        "/nodes/pve1/qemu/100/agent/exec",
        "/nodes/pve2/qemu/101/agent/exec",
    ]
    assert all(c.data["command"] == ["sh", "-c", "uptime"] for c in execs)


def test_exec_by_tag_keeps_command_arguments(cli, api):
    api.route("GET", "/cluster/resources", _running(RESOURCES))
    api.route("GET", "/nodes/{node}/qemu/{vmid}/config", {"ostype": "l26"})
    api.route("POST", "/nodes/{node}/qemu/{vmid}/agent/exec", {"pid": 42})
    api.route("GET", "/nodes/{node}/qemu/{vmid}/agent/exec-status", {"exited": 1, "exitcode": 0})
    r = cli("vm", "exec", "--tag", "linux", "-y", "-p", "p1", "--", "ls", "-la", "/tmp")
    assert r.exit_code == 0, r.output
    execs = api.called("POST", "/nodes/{node}/qemu/{vmid}/agent/exec")
    assert [c.path for c in execs] == ["/nodes/pve1/qemu/100/agent/exec"]
    assert execs[0].data["command"] == ["sh", "-c", "ls -la /tmp"]


def test_exec_by_tag_without_yes_off_terminal_runs_nothing(cli, api):
    api.route("GET", "/cluster/resources", _running(RESOURCES))
    api.route("POST", "/nodes/{node}/qemu/{vmid}/agent/exec", {"pid": 42})
    r = cli("vm", "exec", "--tag", "x", "-p", "p1", "--", "reboot", input="")
    assert r.exit_code != 0
    assert api.writes() == []
