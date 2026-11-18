"""Migration of one VM or container (vm migrate, ct migrate) against recorded PVE answers."""

from __future__ import annotations

from typing import Any

from tests.fake_api import fixture, upid

# Recorded on PVE 9.1.1: VM 300 is stopped on pve-lab2, CT 311 runs on pve-lab1
VM_NODE = "pve-lab2"
CT_NODE = "pve-lab1"
TARGET = "pve-lab3"


def _cluster(api: Any, vm_status: str = "stopped", ct_status: str = "running",
             resources_status: str | None = None) -> None:
    """A 5 node 9.1 cluster. resources_status overrides what the lagging
    /cluster/resources says, the live state stays in status/current."""
    resources = fixture("pve-9.1/cluster_resources_vm")
    if resources_status is not None:
        for r in resources:
            r["status"] = resources_status
    api.route("GET", "/cluster/resources", resources)
    api.route("GET", "/nodes", fixture("pve-9.1/nodes"))
    vm_current = fixture("pve-9.1/qemu_status_current") | {"status": vm_status, "qmpstatus": vm_status}
    api.route("GET", f"/nodes/{VM_NODE}/qemu/300/status/current", vm_current)
    api.route("GET", f"/nodes/{CT_NODE}/lxc/311/status/current", {"status": ct_status, "name": "ct311", "vmid": 311})
    api.route("GET", f"/nodes/{VM_NODE}/qemu/300/migrate", fixture("pve-9.1/qemu_migrate_preconditions"))
    api.route("GET", f"/nodes/{CT_NODE}/lxc/311/migrate", fixture("pve-9.1/lxc_migrate_preconditions"))
    api.route("GET", "/cluster/ha/resources", fixture("pve-9.1/ha_resources"))
    api.route("POST", f"/nodes/{VM_NODE}/qemu/300/migrate", upid(VM_NODE, "qmigrate", 300))
    api.route("POST", f"/nodes/{CT_NODE}/lxc/311/migrate", upid(CT_NODE, "vzmigrate", 311))


def _migrate_posts(api: Any) -> list[Any]:
    return [c for c in api.writes() if c.path.endswith("/migrate")]


# ── preconditions ──────────────────────────────────────────────────────


def test_empty_not_allowed_entries_do_not_block(cli, api):
    """PVE 9.1 lists every node in not_allowed_nodes with an empty entry while
    also naming it in allowed_nodes: that is no block at all."""
    _cluster(api)
    r = cli("vm", "migrate", "300", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    posts = _migrate_posts(api)
    assert len(posts) == 1
    assert posts[0].path == f"/nodes/{VM_NODE}/qemu/300/migrate"
    assert posts[0].data == {"target": TARGET}


def test_not_allowed_entry_with_a_reason_blocks(cli, api):
    _cluster(api)
    pre = fixture("pve-9.1/qemu_migrate_preconditions")
    pre["not_allowed_nodes"][TARGET] = {"unavailable_storages": ["local-zfs"]}
    api.route("GET", f"/nodes/{VM_NODE}/qemu/300/migrate", pre)
    r = cli("vm", "migrate", "300", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "local-zfs" in r.output
    assert api.writes() == []


def test_node_missing_from_allowed_nodes_blocks(cli, api):
    _cluster(api)
    pre = fixture("pve-9.1/qemu_migrate_preconditions")
    pre["allowed_nodes"].remove(TARGET)
    api.route("GET", f"/nodes/{VM_NODE}/qemu/300/migrate", pre)
    r = cli("vm", "migrate", "300", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert TARGET in r.output
    assert api.writes() == []


def test_ct_preconditions_with_dashed_keys_allow_listed_node(cli, api):
    """The lxc endpoint spells allowed-nodes with dashes."""
    _cluster(api, ct_status="stopped")
    r = cli("ct", "migrate", "311", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.data for c in _migrate_posts(api)] == [{"target": TARGET}]


# ── live, offline, restart ─────────────────────────────────────────────


def test_running_vm_migrates_live(cli, api):
    _cluster(api, vm_status="running")
    r = cli("vm", "migrate", "300", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.data for c in _migrate_posts(api)] == [{"target": TARGET, "online": 1}]


def test_offline_flag_on_running_vm_is_refused(cli, api):
    _cluster(api, vm_status="running")
    r = cli("vm", "migrate", "300", TARGET, "--offline", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "running" in r.output
    assert api.writes() == []


def test_running_ct_without_restart_is_refused_with_yes(cli, api):
    _cluster(api, ct_status="running")
    r = cli("ct", "migrate", "311", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "--restart" in r.output
    assert api.writes() == []


def test_running_ct_with_restart_sends_restart(cli, api):
    _cluster(api, ct_status="running")
    r = cli("ct", "migrate", "311", TARGET, "--restart", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.data for c in _migrate_posts(api)] == [{"target": TARGET, "restart": 1}]


# ── live state over the lagging /cluster/resources ─────────────────────


def test_fresh_ct_shown_unknown_is_treated_as_running(cli, api):
    """/cluster/resources says 'unknown' for a fresh CT, status/current says
    running: the migration must need --restart."""
    _cluster(api, ct_status="running", resources_status="unknown")
    r = cli("ct", "migrate", "311", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "--restart" in r.output
    assert api.writes() == []


def test_vm_shown_unknown_but_running_migrates_live(cli, api):
    _cluster(api, vm_status="running", resources_status="unknown")
    r = cli("vm", "migrate", "300", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _migrate_posts(api)[0].data == {"target": TARGET, "online": 1}


def test_status_current_wins_over_resources_saying_running(cli, api):
    """The other way round: resources still says running, the VM is stopped."""
    _cluster(api, vm_status="stopped", resources_status="running")
    r = cli("vm", "migrate", "300", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _migrate_posts(api)[0].data == {"target": TARGET}


# ── HA managed guests ──────────────────────────────────────────────────


def _ha_entry(state: str, node: str) -> dict[str, Any]:
    return {"id": "service:vm:300", "sid": "vm:300", "type": "service", "state": state,
            "node": node, "status": f"{state}"}


def _ha_cluster(api: Any) -> None:
    _cluster(api, vm_status="running")
    api.route("GET", "/cluster/ha/resources", [{"sid": "vm:300", "state": "started", "type": "vm"}])
    api.route("POST", f"/nodes/{VM_NODE}/qemu/300/migrate", upid(VM_NODE, "hamigrate", 300))


def test_ha_migration_waits_for_the_guest_on_the_target(cli, api, clock):
    """The hamigrate task ends before the move: pvecli follows the HA status
    (matched by sid) until the guest is started on the target."""
    _ha_cluster(api)
    base = fixture("pve-9.1/ha_status_current")
    states = iter([
        _ha_entry("started", VM_NODE),
        _ha_entry("migrate", VM_NODE),
        _ha_entry("migrate", VM_NODE),
    ])

    def status(_call: Any) -> list[dict[str, Any]]:
        return base + [next(states, _ha_entry("started", TARGET))]

    api.route("GET", "/cluster/ha/status/current", status)
    r = cli("vm", "migrate", "300", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "migrated" in r.output
    assert len(api.called("GET", "/cluster/ha/status/current")) >= 4
    assert len(_migrate_posts(api)) == 1


def test_ha_request_ignored_fails_fast(cli, api, clock):
    """The HA manager never moves to 'migrate': clear error within the 60 s
    grace instead of waiting for the hour long timeout."""
    _ha_cluster(api)
    base = fixture("pve-9.1/ha_status_current")
    api.route("GET", "/cluster/ha/status/current", base + [_ha_entry("started", VM_NODE)])
    start = clock.now
    r = cli("vm", "migrate", "300", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "did not act" in r.output
    assert clock.now - start < 120


def test_hamigrate_task_followed_even_if_ha_resources_unreadable(cli, api, clock):
    """The UPID kind alone shows the request went through the HA manager."""
    _ha_cluster(api)
    api.route("GET", "/cluster/ha/resources", api.forbidden("/", "Sys.Audit"))
    seq = iter([_ha_entry("migrate", VM_NODE)])
    api.route("GET", "/cluster/ha/status/current",
              lambda _c: [next(seq, _ha_entry("started", TARGET))])
    r = cli("vm", "migrate", "300", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert len(api.called("GET", "/cluster/ha/status/current")) >= 2


def test_migrating_to_the_current_node_is_refused(cli, api):
    _cluster(api)
    r = cli("vm", "migrate", "300", VM_NODE, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []
