"""Bugs found by checking the code against the real API on the lab.

Each test was written first and seen failing before the fix.
"""

import json

from tests.fake_api import fixture, upid

GUESTS = [
    {"vmid": 100, "type": "qemu", "node": "pve1", "status": "running", "name": "web", "tags": "web"},
    {"vmid": 101, "type": "lxc", "node": "pve1", "status": "running", "name": "ct", "tags": "web"},
]


def _guests(api, guests=GUESTS):
    api.route("GET", "/cluster/resources", guests)
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}])


# ── shutdown must never turn into a hard stop by itself ──────────────────
# PVE defaults force-stop to 1 on both /cluster/bulk-action/guest/shutdown
# and /nodes/{node}/stopall (schema: default 1). Not sending it means a
# hard stop when the timeout runs out, the opposite of a clean shutdown.


def test_bulk_shutdown_sends_force_stop_0_unless_asked(cli, api):
    _guests(api)
    api.route("POST", "/cluster/bulk-action/guest/shutdown", upid("pve1", "bulk-shutdown"))
    api.route("GET", "/nodes/{node}/{kind}/{vmid}/status/current", {"status": "stopped"})
    r = cli("cluster", "guests", "shutdown", "--tag", "web", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    (call,) = api.called("POST", "/cluster/bulk-action/guest/shutdown")
    assert call.data["force-stop"] == 0


def test_bulk_shutdown_force_stop_when_asked(cli, api):
    _guests(api)
    api.route("POST", "/cluster/bulk-action/guest/shutdown", upid("pve1", "bulk-shutdown"))
    api.route("GET", "/nodes/{node}/{kind}/{vmid}/status/current", {"status": "stopped"})
    assert cli("cluster", "guests", "shutdown", "--tag", "web", "--force-stop", "-y", "-p", "p1").exit_code == 0
    (call,) = api.called("POST", "/cluster/bulk-action/guest/shutdown")
    assert call.data["force-stop"] == 1


def test_fallback_stopall_sends_force_stop_0(cli, api):
    _guests(api)
    api.route("POST", "/cluster/bulk-action/guest/shutdown", api.not_implemented("POST", "/cluster/bulk-action/guest/shutdown"))
    api.route("POST", "/nodes/{node}/stopall", upid("pve1", "stopall"))
    api.route("GET", "/nodes/{node}/{kind}/{vmid}/status/current", {"status": "stopped"})
    assert cli("cluster", "guests", "shutdown", "--tag", "web", "-y", "-p", "p1").exit_code == 0
    (call,) = api.called("POST", "/nodes/pve1/stopall")
    assert call.data["force-stop"] == 0


# ── vm exec decides on the live state ────────────────────────────────────


def test_exec_runs_on_a_vm_the_cluster_list_still_shows_stopped(cli, api):
    # /cluster/resources lags behind: a VM just started still reads "stopped"
    _guests(api, [{"vmid": 100, "type": "qemu", "node": "pve1", "status": "stopped", "name": "web"}])
    api.route("GET", "/nodes/{node}/qemu/{vmid}/status/current", {"status": "running"})
    api.route("GET", "/nodes/{node}/qemu/{vmid}/config", {"ostype": "l26"})
    api.route("POST", "/nodes/{node}/qemu/{vmid}/agent/exec", {"pid": 7})
    api.route("GET", "/nodes/{node}/qemu/{vmid}/agent/exec-status", {"exited": 1, "exitcode": 0, "out-data": "up\n"})
    r = cli("vm", "exec", "100", "-p", "p1", "--", "uptime")
    assert r.exit_code == 0, r.output
    assert api.called("POST", "/nodes/pve1/qemu/100/agent/exec")


# ── find --vlan 0 is a filter, not "no filter" ───────────────────────────


def test_find_vlan_zero_is_a_real_filter(cli, api):
    _guests(api, [
        {"vmid": 100, "type": "qemu", "node": "pve1", "status": "running", "name": "tagged0"},
        {"vmid": 102, "type": "qemu", "node": "pve1", "status": "running", "name": "untagged"},
    ])
    api.route("GET", "/nodes/{node}/qemu/100/config", {"net0": "virtio=AA:BB:CC:00:00:01,bridge=vmbr0,tag=0"})
    api.route("GET", "/nodes/{node}/qemu/102/config", {"net0": "virtio=AA:BB:CC:00:00:02,bridge=vmbr0"})
    r = cli("find", "--vlan", "0", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    assert [g["vmid"] for g in json.loads(r.stdout)] == [100]


# ── the real SDN lock refusal is explained ───────────────────────────────


def test_sdn_apply_with_lock_held_uses_the_real_pve_message(cli, api):
    held = fixture("pve-9.2/sdn_lock_held")
    api.route("GET", "/cluster/sdn/zones", [{"zone": "z1", "type": "simple", "state": "new"}])
    api.route("GET", "/cluster/sdn/vnets", [])
    api.route("GET", "/cluster/config/totem", {"cluster_name": "sdnlab"})
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}])
    api.route("GET", "/cluster/sdn/dry-run", {})
    api.route("POST", "/cluster/sdn/lock", api.error(held["__status__"], held["__error__"]))
    r = cli("sdn", "apply", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "pvecli sdn unlock" in r.output
    assert api.called("PUT", "/cluster/sdn") == []


# ── snapshot: an unreadable guest is said, not silently empty ───────────


def test_snapshot_list_reports_an_unreadable_guest(cli, api):
    _guests(api, [{"vmid": 100, "type": "qemu", "node": "pve1", "status": "running", "name": "web"}])
    api.route("GET", "/nodes/{node}/qemu/{vmid}/snapshot", api.forbidden("/vms/100", "VM.Audit"))
    r = cli("snapshot", "list", "-p", "p1")
    assert "100" in r.output and "VM.Audit" in r.output


# ── multi-target commands decide on the live state ──────────────────────


def test_remove_right_after_stop_is_not_refused(cli, api):
    # Seen on the lab: ct stop then ct remove was refused "is running" for
    # 5 to 10 seconds, /cluster/resources still showing the old state
    _guests(api, [{"vmid": 310, "type": "lxc", "node": "pve1", "status": "running", "name": "ct"}])
    api.route("GET", "/nodes/{node}/lxc/{vmid}/status/current", {"status": "stopped"})
    api.route("DELETE", "/nodes/{node}/lxc/{vmid}", upid("pve1", "vzdestroy", 310))
    r = cli("ct", "remove", "310", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("DELETE", "/nodes/pve1/lxc/310")


def test_failed_clean_shutdown_suggests_force_stop(cli, api):
    # Seen on the lab: a guest ignoring ACPI stays up, the task fails with
    # "Some guests failed to shutdown"; the way out must be said
    _guests(api)
    api.route("POST", "/cluster/bulk-action/guest/shutdown", upid("pve1", "bulk-shutdown"))
    api.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "Some guests failed to shutdown 100"})
    r = cli("cluster", "guests", "shutdown", "--tag", "web", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "--force-stop" in r.output
