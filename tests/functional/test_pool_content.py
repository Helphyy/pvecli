"""pool content add / remove and pool usage against the fake API.

PUT /pools/{poolid} (PVE::API2::Pool::update_pool):
- vms=<list> adds guests; a guest already in another pool is refused with
  "VM <id> belongs already to pool '<p>' and 'allow-move' is not set" unless
  allow-move=1; a guest already in this pool: "VM <id> is already a pool member".
- vms=<list>, delete=1 removes them from the pool (the guests are untouched).
"""

from __future__ import annotations

import json

RESOURCES = [
    {"id": "qemu/100", "type": "qemu", "vmid": 100, "name": "web", "node": "pve1", "status": "running"},
    {"id": "qemu/101", "type": "qemu", "vmid": 101, "name": "db", "node": "pve1", "status": "running", "pool": "prod"},
    {"id": "lxc/200", "type": "lxc", "vmid": 200, "name": "dns", "node": "pve2", "status": "stopped"},
    {"id": "lxc/201", "type": "lxc", "vmid": 201, "node": "pve2", "status": "stopped", "pool": "dev"},
]

DEV = {
    "members": [
        {"id": "lxc/201", "type": "lxc", "vmid": 201, "node": "pve2"},
        {"id": "qemu/102", "type": "qemu", "vmid": 102, "name": "ci", "node": "pve1"},
        {"id": "storage/pve1/local", "type": "storage", "storage": "local", "node": "pve1"},
    ]
}


def _routes(api):
    api.route("GET", "/pools", [{"poolid": "dev"}, {"poolid": "prod"}])
    api.route("GET", "/cluster/resources", RESOURCES)
    api.route("GET", "/pools/dev", DEV)
    api.route("PUT", "/pools/{poolid}", None)


# ── content add ──────────────────────────────────────────────────────────


def test_add_vm_and_ct(cli, api):
    _routes(api)
    r = cli("pool", "content", "add", "dev", "100,200", "-p", "p1")
    assert r.exit_code == 0, r.output
    (put,) = api.called("PUT", "/pools/dev")
    assert put.data == {"vms": "100,200"}
    assert "VM 100 (web)" in r.output and "Container 200 (dns)" in r.output


def test_add_force_sends_allow_move(cli, api):
    _routes(api)
    r = cli("pool", "content", "add", "dev", "101", "--force", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("PUT", "/pools/dev")[0].data == {"vms": "101", "allow-move": 1}


def test_add_guest_of_another_pool_without_force(cli, api):
    _routes(api)
    api.route("PUT", "/pools/dev", api.error(
        500, "update pools failed: VM 101 belongs already to pool 'prod' and 'allow-move' is not set\n"))
    r = cli("pool", "content", "add", "dev", "101", "-p", "p1")
    assert r.exit_code == 1
    assert "already in another pool" in r.output
    assert "--force" in r.output


def test_add_guest_already_in_this_pool_does_not_suggest_force(cli, api):
    _routes(api)
    api.route("PUT", "/pools/dev", api.error(500, "update pools failed: VM 201 is already a pool member\n"))
    r = cli("pool", "content", "add", "dev", "201", "-p", "p1")
    # --force cannot help here: PVE refuses allow-move the same way
    assert r.exit_code == 1
    assert "already a pool member" in r.output
    assert "--force" not in r.output
    assert "another pool" not in r.output


def test_add_other_api_error_is_reported(cli, api):
    _routes(api)
    api.route("PUT", "/pools/dev", api.forbidden("/vms/100", "VM.Allocate"))
    r = cli("pool", "content", "add", "dev", "100", "-p", "p1")
    assert r.exit_code == 1
    assert "VM.Allocate" in r.output


def test_add_unknown_ids_are_dropped_with_warning(cli, api):
    _routes(api)
    r = cli("pool", "content", "add", "dev", "100,999", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("PUT", "/pools/dev")[0].data == {"vms": "100"}
    assert "999 not found" in r.output


def test_add_only_unknown_ids_writes_nothing(cli, api):
    _routes(api)
    r = cli("pool", "content", "add", "dev", "999", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_add_invalid_vmid_writes_nothing(cli, api):
    _routes(api)
    r = cli("pool", "content", "add", "dev", "web", "-p", "p1")
    assert r.exit_code == 1
    assert "Invalid VMID" in r.output
    assert api.writes() == []


def test_add_interactive_offers_only_guests_without_pool(cli, api, ui):
    _routes(api)
    ui.menu("dev").multi(["CT 200"])
    r = cli("pool", "content", "add", "-p", "p1")
    assert r.exit_code == 0, r.output
    offered = ui.asked[1][1]
    assert "VM 100 (web)" in offered and "CT 200 (dns)" in offered
    assert "101" not in offered and "201" not in offered
    assert api.called("PUT", "/pools/dev")[0].data == {"vms": "200"}


def test_add_interactive_cancel_pool_menu(cli, api, ui):
    _routes(api)
    ui.menu(None)
    r = cli("pool", "content", "add", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_add_interactive_cancel_guest_menu(cli, api, ui):
    _routes(api)
    ui.multi(None)
    r = cli("pool", "content", "add", "dev", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_add_interactive_nothing_available(cli, api, ui):
    _routes(api)
    api.route("GET", "/cluster/resources", [r for r in RESOURCES if r.get("pool")])
    r = cli("pool", "content", "add", "dev", "-p", "p1")
    assert r.exit_code == 0
    assert "No VMs/CTs available" in r.output
    assert api.writes() == []


def test_add_no_pool_to_pick(cli, api, ui):
    api.route("GET", "/pools", [])
    r = cli("pool", "content", "add", "-p", "p1")
    assert r.exit_code == 0
    assert "No pools found" in r.output
    assert api.writes() == []


# ── content remove ───────────────────────────────────────────────────────


def test_remove_with_yes(cli, api):
    _routes(api)
    r = cli("pool", "content", "remove", "dev", "201,102", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    (put,) = api.writes()
    assert put.path == "/pools/dev"
    assert put.data == {"vms": "201,102", "delete": 1}
    # Only detached from the pool: no guest deletion
    assert not api.called("DELETE", "/nodes/{node}/qemu/{vmid}")


def test_remove_confirm_refused_writes_nothing(cli, api, ui):
    _routes(api)
    ui.confirm(False)
    r = cli("pool", "content", "remove", "dev", "102", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    assert "VM 102 (ci)" in ui.asked[0][1]


def test_remove_ignores_ids_not_in_pool(cli, api):
    _routes(api)
    r = cli("pool", "content", "remove", "dev", "100,102", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes()[0].data == {"vms": "102", "delete": 1}
    assert "100 not found in pool 'dev'" in r.output


def test_remove_only_foreign_ids_writes_nothing(cli, api):
    _routes(api)
    r = cli("pool", "content", "remove", "dev", "100", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_remove_invalid_vmid(cli, api):
    _routes(api)
    r = cli("pool", "content", "remove", "dev", "1x", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_remove_interactive_lists_guests_only(cli, api, ui):
    _routes(api)
    ui.menu("dev").multi(["VM 102"]).confirm(True)
    r = cli("pool", "content", "remove", "-p", "p1")
    assert r.exit_code == 0, r.output
    offered = ui.asked[1][1]
    assert "CT 201" in offered and "VM 102 (ci)" in offered
    assert "local" not in offered  # storages are not guests
    assert api.writes()[0].data == {"vms": "102", "delete": 1}


def test_remove_interactive_empty_selection(cli, api, ui):
    _routes(api)
    ui.multi([])
    r = cli("pool", "content", "remove", "dev", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_remove_pool_without_guests(cli, api):
    _routes(api)
    api.route("GET", "/pools/dev", {"members": [DEV["members"][2]]})
    r = cli("pool", "content", "remove", "dev", "102", "-y", "-p", "p1")
    assert r.exit_code == 0
    assert "has no VMs/CTs" in r.output
    assert api.writes() == []


# ── pool usage ───────────────────────────────────────────────────────────

USAGE_RESOURCES = [
    {"id": "node/pve1", "type": "node", "node": "pve1", "status": "online",
     "maxcpu": 16, "cpu": 0.1, "maxmem": 64 * 2**30, "mem": 8 * 2**30},
    {"id": "qemu/100", "type": "qemu", "vmid": 100, "node": "pve1", "status": "running",
     "pool": "dev", "maxcpu": 4, "cpu": 0.5, "maxmem": 4 * 2**30, "mem": 2 * 2**30, "maxdisk": 32 * 2**30},
    {"id": "qemu/101", "type": "qemu", "vmid": 101, "node": "pve1", "status": "stopped",
     "pool": "dev", "maxcpu": 2, "cpu": 0, "maxmem": 2 * 2**30, "mem": 0, "maxdisk": 10 * 2**30},
    {"id": "qemu/102", "type": "qemu", "vmid": 102, "node": "pve1", "status": "running",
     "pool": "prod", "maxcpu": 8, "cpu": 0.2, "maxmem": 16 * 2**30, "mem": 8 * 2**30, "maxdisk": 50 * 2**30},
]


def _usage_routes(api):
    api.route("GET", "/pools", [{"poolid": "dev"}, {"poolid": "prod"}])
    api.route("GET", "/cluster/resources", USAGE_RESOURCES)
    api.route("GET", "/storage", [])
    api.route("GET", "/pools/dev", {"comment": "team", "members": []})


def test_usage_json_counts_only_pool_guests(cli, api):
    _usage_routes(api)
    r = cli("pool", "usage", "dev", "--no-storage", "--json", "-p", "p1")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["scope"] == "pool"
    assert doc["poolid"] == "dev"
    assert doc["comment"] == "team"
    text = json.dumps(doc)
    # 4 + 2 vCPU of the two dev guests, never the 8 of the prod one
    assert '"cpu_allocated": 6' in text
    assert api.writes() == []


def test_usage_text(cli, api):
    _usage_routes(api)
    r = cli("pool", "usage", "dev", "--no-storage", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "dev" in r.stdout


def test_usage_menu(cli, api, ui):
    _usage_routes(api)
    ui.menu("dev")
    r = cli("pool", "usage", "--no-storage", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("GET", "/pools/dev")


def test_usage_json_without_id_refused(cli, api):
    _usage_routes(api)
    r = cli("pool", "usage", "--json", "-p", "p1")
    assert r.exit_code == 1


def test_usage_unknown_pool(cli, api):
    _usage_routes(api)
    api.route("GET", "/pools/nope", api.error(500, ""))
    r = cli("pool", "usage", "nope", "--no-storage", "-p", "p1")
    assert r.exit_code == 1
    assert "nope" in r.output
