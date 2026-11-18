"""pvecli backup list: every backup of every guest, on every backup storage."""

from __future__ import annotations

import json

from tests.functional._backup_cluster import CONTENT, DAY, NOW, cluster, recorded, vol


def _reads(api, storage: str) -> list[str]:
    return [c.path.split("/")[2] for c in api.called("GET", f"/nodes/{{node}}/storage/{storage}/content")]


def test_lists_every_backup_storage_once_per_node_and_shared_once(cli, api):
    cluster(api)
    r = cli("backup", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    # local is per node: read on each online node; pbs is shared: read once;
    # the disabled NFS and the image storage are never read
    assert sorted(_reads(api, "local")) == ["pve1", "pve2"]
    assert len(_reads(api, "pbs")) == 1
    assert _reads(api, "old-nfs") == [] and _reads(api, "local-lvm") == []
    # content=backup asked, not the whole storage
    assert all(c.params == {"content": "backup"}
               for c in api.called("GET", "/nodes/{node}/storage/{storage}/content"))
    assert api.writes() == []
    for items in CONTENT.values():
        for it in items:
            assert it["volid"] in r.stdout


def test_table_shows_name_age_size_protection_notes(cli, api):
    cluster(api)
    r = cli("backup", "list", "-p", "p1")
    row = next(line for line in r.stdout.splitlines() if "nightly web1" in line)
    assert "web1" in row and "1d" in row and "1.0 GB" in row
    protected = next(line for line in r.stdout.splitlines() if "-" + str(NOW - 8 * DAY) in line)
    assert "yes" in protected
    # A backup whose guest is gone still shows, with its notes
    gone = next(line for line in r.stdout.splitlines() if "guest removed since" in line)
    assert "555" in gone


def test_json_has_one_entry_per_backup(cli, api):
    cluster(api)
    r = cli("backup", "list", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert len(doc) == sum(len(v) for v in CONTENT.values())
    first = next(d for d in doc if d["notes"] == "nightly web1")
    assert first["vmid"] == 100 and first["name"] == "web1" and first["type"] == "qemu"
    assert first["storage"] == "local" and first["node"] == "pve1"
    assert first["ctime"] == NOW - DAY and first["size"] == 1_000_000_000
    assert first["protected"] is False
    # A shared storage belongs to no node
    assert {d["node"] for d in doc if d["storage"] == "pbs"} == {None}
    assert any(d["protected"] is True for d in doc)


def test_filter_by_vmid(cli, api):
    cluster(api)
    r = cli("backup", "list", "--vmid", "100,200", "-p", "p1", "--json")
    assert {d["vmid"] for d in json.loads(r.stdout)} == {100, 200}


def test_filter_by_tag_and_pool(cli, api):
    cluster(api)
    r = cli("backup", "list", "--tag", "db", "-p", "p1", "--json")
    assert {d["vmid"] for d in json.loads(r.stdout)} == {101}
    r = cli("backup", "list", "--pool", "P", "-p", "p1", "--json")
    assert {d["vmid"] for d in json.loads(r.stdout)} == {100, 200}


def test_filter_by_storage_reads_only_that_storage(cli, api):
    cluster(api)
    r = cli("backup", "list", "--storage", "pbs", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    assert {d["storage"] for d in json.loads(r.stdout)} == {"pbs"}
    assert _reads(api, "local") == []


def test_unknown_storage_is_an_error(cli, api):
    cluster(api)
    r = cli("backup", "list", "--storage", "nope", "-p", "p1")
    assert r.exit_code == 1
    assert "nope" in r.output and "local" in r.output  # names the backup storages


def test_unreadable_storage_is_said_never_shown_empty(cli, api):
    cluster(api)
    # What lab1 answered while its PBS was down
    api.route("GET", "/nodes/{node}/storage/pbs/content",
              api.error(500, "pbs-lab: error fetching datastores - 500 Can't connect to 10.199.32.70:8007"))
    r = cli("backup", "list", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)  # stdout stays parseable
    assert {d["storage"] for d in doc} == {"local"}
    assert "pbs" in r.stderr and "Can't connect" in r.stderr


def test_nothing_readable_exits_1(cli, api):
    cluster(api)
    api.route("GET", "/nodes/{node}/storage/{storage}/content", api.forbidden("/storage/x", "Datastore.Audit"))
    r = cli("backup", "list", "-p", "p1")
    assert r.exit_code == 1
    assert "Datastore.Audit" in r.output


def test_no_backup_at_all(cli, api):
    cluster(api, content={})
    r = cli("backup", "list", "-p", "p1")
    assert r.exit_code == 0
    assert "No backup found" in r.output


def test_recorded_lab_answers(cli, api):
    # lab1: one diskless backup of VM 300 on local@pve-lab2, one on the PBS
    local, pbs = recorded("backup_content_local"), recorded("backup_content_pbs")
    api.route("GET", "/nodes", [{"node": "pve-lab1", "status": "online"}, {"node": "pve-lab2", "status": "online"}])
    api.route("GET", "/cluster/resources", recorded_guests())
    api.route("GET", "/storage", [
        {"storage": "local", "type": "dir", "content": "backup,import,vztmpl,iso"},
        {"storage": "pbs-lab", "type": "pbs", "content": "backup", "shared": 1},
    ])
    api.route("GET", "/nodes/{node}/storage/local/content", lambda c: local if c.match["node"] == "pve-lab2" else [])
    api.route("GET", "/nodes/{node}/storage/pbs-lab/content", pbs)
    r = cli("backup", "list", "-p", "p1", "--json")
    doc = json.loads(r.stdout)
    assert {d["volid"] for d in doc} == {x["volid"] for x in local + pbs}
    assert all(d["name"] == "guest-sdn" and d["type"] == "qemu" for d in doc)


def recorded_guests() -> list[dict]:
    return [g for g in recorded_resources() if g.get("vmid") == 300]


def recorded_resources() -> list[dict]:
    from tests.fake_api import fixture

    return fixture("pve-9.1/cluster_resources_vm")


def test_fleet_json_gathers_profiles(cli, api):
    cluster(api)
    api.route("GET", "/cluster/status", [{"type": "cluster", "name": "c1"}, {"type": "node", "name": "pve1"}], host="h1")
    api.route("GET", "/cluster/status", [{"type": "cluster", "name": "c2"}, {"type": "node", "name": "x1"}], host="h2")
    api.route("GET", "/nodes/{node}/storage/{storage}/content", [vol("local", 7, "qemu", NOW)], host="h2")
    r = cli("backup", "list", "-p", "p1,p2", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert set(doc) == {"p1", "p2"}
    assert {d["vmid"] for d in doc["p2"]} == {7}
