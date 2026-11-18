"""cluster shutdown / reboot orchestration: Ceph flags, guests, node order, interruption.

The HA phase on its own is covered by test_ha_arm_health.py.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from tests.fake_api import Call, FakeAPI, fixture, upid

FLAGS = ["noout", "nobackfill", "norecover", "norebalance"]
DISARM = "/cluster/ha/status/disarm-ha"


def setup(api: FakeAPI, ceph: Any = None, running: dict[str, int] | None = None) -> None:
    """Three nodes, the profile host h1 is pve2's address: pve2 goes last."""
    api.route("GET", "/nodes", [{"node": n, "status": "online"} for n in ("pve3", "pve1", "pve2")]
              + [{"node": "pve4", "status": "offline"}])
    api.route("GET", "/cluster/status", [
        {"type": "cluster", "name": "c", "quorate": 1},
        {"type": "node", "name": "pve1", "ip": "10.0.0.1", "online": 1},
        {"type": "node", "name": "pve2", "ip": "h1", "online": 1},
        {"type": "node", "name": "pve3", "ip": "10.0.0.3", "online": 1},
        {"type": "node", "name": "pve4", "ip": "10.0.0.4", "online": 0},
    ])
    guests = []
    for node, count in (running or {"pve1": 2, "pve2": 1}).items():
        guests += [{"type": "qemu", "vmid": 100 + i + 10 * int(node[-1]), "node": node, "status": "running"}
                   for i in range(count)]
    guests.append({"type": "qemu", "vmid": 999, "node": "pve3", "status": "stopped"})
    api.route("GET", "/cluster/resources", guests)
    api.route("GET", "/cluster/ha/resources", [])
    api.route("GET", "/nodes/{node}/ceph/status", ceph if ceph is not None else fixture("pve-9.1/ceph_status"))
    api.route("PUT", "/cluster/ceph/flags/{flag}", None)
    api.route("POST", "/nodes/{node}/stopall", lambda c: upid(c.match["node"], "stopall"))
    api.route("POST", "/nodes/{node}/status", None)


def ceph_absent(api: FakeAPI) -> Any:
    data = fixture("pve-9.2/ceph_status")  # recorded: no Ceph on this cluster
    return api.error(data["__status__"], data["__error__"])


def writes(api: FakeAPI) -> list[tuple[str, str]]:
    return [(w.method, w.path) for w in api.writes()]


@pytest.mark.parametrize("command", ["shutdown", "reboot"])
def test_full_sequence_flags_then_guests_then_nodes_connected_last(cli, api, command):
    setup(api)
    r = cli("cluster", command, "-y", "-t", "120", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == (
        [("PUT", f"/cluster/ceph/flags/{f}") for f in FLAGS]
        # stopall only where guests run, in the node order
        + [("POST", "/nodes/pve1/stopall"), ("POST", "/nodes/pve2/stopall")]
        # workers by name, the node behind the profile host last, offline pve4 untouched
        + [("POST", "/nodes/pve1/status"), ("POST", "/nodes/pve3/status"), ("POST", "/nodes/pve2/status")]
    )
    assert all(c.data == {"value": 1} for c in api.called("PUT", "/cluster/ceph/flags/{flag}"))
    assert all(c.data == {"timeout": 120} for c in api.called("POST", "/nodes/{node}/stopall"))
    assert all(c.data == {"command": command} for c in api.called("POST", "/nodes/{node}/status"))
    # Ceph status asked to the connected node
    assert api.called("GET", "/nodes/pve2/ceph/status")
    # Flags are recalled for the way back up, one unset per flag
    for f in FLAGS:
        assert f"ceph osd unset {f}" in r.output
    assert f"Cluster {command} complete" in r.output


def test_stopall_waits_for_the_task(cli, api):
    setup(api, running={"pve1": 1})
    status_calls: list[str] = []

    def task_status(call: Call) -> dict[str, Any]:
        status_calls.append(call.path)
        # Still running the first time it is asked
        return {"status": "running"} if len(status_calls) == 1 else {"status": "stopped", "exitstatus": "OK"}

    api.route("GET", "/nodes/{node}/tasks/{upid}/status", task_status)
    r = cli("cluster", "shutdown", "-y", "--skip-ceph", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert len(status_calls) >= 2
    assert "All guests stopped on 'pve1'" in r.output


def test_ceph_absent_sets_no_flag(cli, api):
    setup(api, ceph=ceph_absent(api))
    r = cli("cluster", "reboot", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Ceph not detected" in r.output
    assert not api.called("PUT", "/cluster/ceph/flags/{flag}")
    assert "ceph osd unset" not in r.output


def test_skip_ceph_does_not_even_look(cli, api):
    setup(api)
    r = cli("cluster", "reboot", "-y", "--skip-ceph", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert not api.called("GET", "/nodes/{node}/ceph/status")
    assert not api.called("PUT", "/cluster/ceph/flags/{flag}")


def test_ceph_unknown_state_aborts_before_any_node_goes_down(cli, api):
    setup(api, ceph=api.error(500, "got timeout"))
    r = cli("cluster", "shutdown", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Could not determine whether Ceph is present" in r.output
    assert api.writes() == []


def test_flag_failure_aborts_before_any_guest_or_node(cli, api):
    setup(api)

    def flag(call: Call) -> Any:
        return api.forbidden("/", "Sys.Modify") if call.match["flag"] == "norecover" else None

    api.route("PUT", "/cluster/ceph/flags/{flag}", flag)
    r = cli("cluster", "shutdown", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Failed to set Ceph maintenance flags" in r.output
    assert not api.called("POST", "/nodes/{node}/stopall")
    assert not api.called("POST", "/nodes/{node}/status")


def test_flag_failure_names_the_flags_already_set(cli, api):
    setup(api)

    def flag(call: Call) -> Any:
        return api.forbidden("/", "Sys.Modify") if call.match["flag"] == "norecover" else None

    api.route("PUT", "/cluster/ceph/flags/{flag}", flag)
    r = cli("cluster", "shutdown", "-y", "-p", "p1")
    assert r.exit_code == 1
    # noout and nobackfill really are set on the cluster now
    assert "ceph osd unset noout" in r.output
    assert "ceph osd unset nobackfill" in r.output


def test_abort_after_ha_disarm_says_to_rearm(cli, api):
    setup(api, ceph=api.error(500, "got timeout"))
    api.route("GET", "/cluster/ha/resources", [{"sid": "vm:110", "state": "started"}])
    api.route("POST", DISARM, None)
    r = cli("cluster", "shutdown", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.called("POST", DISARM)
    assert not api.called("POST", "/nodes/{node}/status")
    assert "pvecli cluster ha arm" in r.output


def test_ha_disarm_failure_falls_back_on_disabling_resources(cli, api):
    setup(api, ceph=ceph_absent(api))
    api.route("GET", "/cluster/ha/resources", [
        {"sid": "vm:110", "state": "started"}, {"sid": "vm:111", "state": "stopped"},
        {"sid": "ct:120", "state": "enabled"}])
    api.route("POST", DISARM, api.error(500, "cannot disarm while a migration runs"))
    api.route("PUT", "/cluster/ha/resources/{sid}", None)
    r = cli("cluster", "reboot", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Could not disarm HA" in r.output
    puts = api.called("PUT", "/cluster/ha/resources/{sid}")
    assert [p.path for p in puts] == ["/cluster/ha/resources/vm:110", "/cluster/ha/resources/ct:120"]
    assert all(p.data == {"state": "disabled"} for p in puts)
    assert "Re-enable HA resources: vm:110, ct:120" in r.output
    first_node_write = next(i for i, w in enumerate(api.writes()) if w.path.startswith("/nodes/"))
    assert all("/cluster/ha/" not in w.path for w in api.writes()[first_node_write:])


def test_no_online_node(cli, api):
    setup(api)
    api.route("GET", "/nodes", [{"node": "pve1", "status": "offline"}])
    r = cli("cluster", "shutdown", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "No online nodes" in r.output
    assert api.writes() == []


# ── confirmations ────────────────────────────────────────────────────────

def test_first_confirmation_declined_writes_nothing(cli, api, ui):
    setup(api)
    ui.confirm(False)
    r = cli("cluster", "shutdown", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    assert ui.left() == {}


def test_wrong_typed_word_writes_nothing(cli, api, ui):
    setup(api)
    ui.confirm(True).text("shutdown")  # lowercase: not the word asked for
    r = cli("cluster", "shutdown", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_typed_word_confirms_reboot(cli, api, ui):
    setup(api, ceph=ceph_absent(api))
    ui.confirm(True).text("REBOOT")
    r = cli("cluster", "reboot", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.called("POST", "/nodes/{node}/status")][-1] == "/nodes/pve2/status"
    assert "Type 'REBOOT' to confirm" in ui.asked[1][1]


def test_plan_lists_nodes_and_running_guests(cli, api, ui):
    setup(api)
    ui.confirm(False)
    r = cli("cluster", "shutdown", "-p", "p1")
    assert "affect 3 node(s) and 3 running guest(s)" in r.output
    assert "connected (last)" in next(x for x in r.output.splitlines() if "pve2" in x)
    assert "will NOT come back" in r.output


# ── interruption ─────────────────────────────────────────────────────────

def test_interrupt_stops_sending_and_lists_what_to_undo(cli, api):
    setup(api)
    api.route("GET", "/cluster/ha/resources", [{"sid": "vm:110", "state": "started"}])
    api.route("POST", DISARM, None)

    def node_status(call: Call) -> Any:
        return KeyboardInterrupt() if call.match["node"] == "pve3" else None

    api.route("POST", "/nodes/{node}/status", node_status)
    r = cli("cluster", "shutdown", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Interrupted" in r.output
    assert "Ceph flags were set" in r.output
    assert "Re-arm it with: pvecli cluster ha arm" in r.output
    # The connected node never received the command
    assert not [c for c in api.called("POST", "/nodes/{node}/status") if c.path == "/nodes/pve2/status"]


def test_interrupt_before_92_lists_disabled_resources(cli, api):
    setup(api, ceph=ceph_absent(api))
    api.route("GET", "/cluster/ha/resources", [{"sid": "vm:110", "state": "started"}])
    api.route("POST", DISARM, api.not_implemented("POST", DISARM))
    api.route("PUT", "/cluster/ha/resources/{sid}", None)
    api.route("POST", "/nodes/{node}/stopall", KeyboardInterrupt())
    r = cli("cluster", "shutdown", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "HA resources were disabled: vm:110" in r.output
    assert not api.called("POST", "/nodes/{node}/status")


def test_connected_node_unknown_sends_each_node_once(cli, api, monkeypatch):
    def no_dns(name: str) -> str:
        raise socket.gaierror(name)

    monkeypatch.setattr(socket, "gethostbyname", no_dns)
    setup(api, ceph=ceph_absent(api))
    # Profile host matches no node ip nor name: no DNS lookup can match either
    api.route("GET", "/cluster/status", [
        {"type": "node", "name": "pve1", "ip": "10.0.0.1", "online": 1},
        {"type": "node", "name": "pve2", "ip": "10.0.0.2", "online": 1},
        {"type": "node", "name": "pve3", "ip": "10.0.0.3", "online": 1},
    ])
    api.route("GET", "/nodes", [{"node": n, "status": "online"} for n in ("pve1", "pve2", "pve3")])
    r = cli("cluster", "shutdown", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    paths = [c.path for c in api.called("POST", "/nodes/{node}/status")]
    # Every node exactly once
    assert sorted(paths) == ["/nodes/pve1/status", "/nodes/pve2/status", "/nodes/pve3/status"]
