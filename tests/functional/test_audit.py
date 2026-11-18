"""pvecli audit: read-only health checks against recorded PVE 9.1/9.2 answers."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.fake_api import FakeAPI, fixture

NOW = 1_800_000_000.0  # FakeClock start
ROOT_PRIVS = ("Sys.Audit", "User.Modify", "VM.Audit", "VM.Config.Disk", "VM.Backup", "Datastore.Audit",
              "Datastore.Allocate", "Datastore.AllocateSpace")
DAY = 86400


def _load(version: str, name: str) -> Any:
    """A recorded answer; a recorded error becomes the same APIError."""
    data = fixture(f"{version}/{name}")
    if isinstance(data, dict) and "__error__" in data:
        return FakeAPI.error(data["__status__"], data["__error__"])
    return data


def lab(api: FakeAPI, version: str = "pve-9.1") -> list[dict]:
    """Route every source the audit reads with the lab's recorded answers.

    Each guest has its own disk on local-lvm of its node, so nothing is an
    orphan. Returns the guests.
    """
    guests = _load(version, "cluster_resources_vm")
    api.route("GET", "/nodes", _load(version, "nodes"))
    api.route("GET", "/cluster/resources", guests)
    api.route("GET", "/nodes/{node}/certificates/info", _load(version, "node_certificates"))
    api.route("GET", "/access/users", _load(version, "access_users_full"))
    api.route("GET", "/access/acl", _load(version, "access_acl"))
    api.route("GET", "/cluster/firewall/options", _load(version, "firewall_options"))
    api.route("GET", "/nodes/{node}/version", _load(version, "node_version"))
    api.route("GET", "/nodes/{node}/subscription", _load(version, "node_subscription"))
    api.route("GET", "/nodes/{node}/apt/update", _load(version, "node_apt_update"))
    api.route("GET", "/cluster/backup-info/not-backed-up", _load(version, "backup_not_backed_up"))
    api.route("GET", "/nodes/{node}/qemu/{vmid}/snapshot", _load(version, "qemu_snapshot"))
    api.route("GET", "/nodes/{node}/lxc/{vmid}/snapshot", _load(version, "qemu_snapshot"))
    api.route("GET", "/nodes/{node}/qemu/{vmid}/config", _load(version, "qemu_config"))
    api.route("GET", "/nodes/{node}/lxc/{vmid}/config", _load(version, "lxc_config"))
    api.route("GET", "/cluster/ha/status/current", _load(version, "ha_status_current"))
    api.route("GET", "/cluster/ceph/status", _load(version, "ceph_status"))
    api.route("GET", "/cluster/ceph/flags", _load(version, "ceph_flags"))
    api.route("GET", "/storage", _load(version, "storage"))

    def content(call):
        node, storage = call.match["node"], call.match["storage"]
        if storage != "local-lvm":
            return []
        return [
            {"volid": f"local-lvm:vm-{g['vmid']}-disk-0", "vmid": g["vmid"],
             "content": "rootdir" if g["type"] == "lxc" else "images", "format": "raw", "size": 1}
            for g in guests if g["node"] == node
        ]

    api.route("GET", "/nodes/{node}/storage/{storage}/content", content)
    # root@pam: every privilege everywhere
    api.route("GET", "/access/permissions", lambda c: {c.params["path"]: dict.fromkeys(ROOT_PRIVS, 1)})
    return guests


def run_json(cli, *extra: str) -> tuple[Any, dict]:
    r = cli("audit", "-p", "p1", "--json", *extra)
    return r, json.loads(r.stdout)


def lines(doc: dict, check: str) -> list[dict]:
    return [f for f in doc["findings"] if f["check"] == check]


def levels(doc: dict, check: str) -> set[str]:
    return {f["level"] for f in lines(doc, check)}


# ── healthy cluster ────────────────────────────────────────────────────


@pytest.mark.parametrize("version", ["pve-9.1", "pve-9.2"])
def test_healthy_lab_exits_zero_and_writes_nothing(cli, api, version):
    lab(api, version)
    r, doc = run_json(cli)
    assert r.exit_code == 0, r.output
    assert "FAIL" not in doc["counts"]
    assert levels(doc, "Certificates") == {"OK"}
    assert levels(doc, "Users and tokens") == {"OK"}
    assert levels(doc, "Orphan disks") == {"OK"}
    assert levels(doc, "Snapshots") == {"OK"}
    # The lab firewall is off and some guests are in no backup job: warnings, not failures
    assert levels(doc, "Firewall") == {"WARN"}
    assert levels(doc, "Backups") == {"WARN"}
    assert api.writes() == []


def test_pve92_without_ceph_reports_no_ceph_line(cli, api):
    # 9.2 lab: /cluster/ceph/status answers 500 "binary not installed"
    lab(api, "pve-9.2")
    r, doc = run_json(cli)
    assert r.exit_code == 0, r.output
    assert lines(doc, "Ceph") == [] and lines(doc, "Ceph flags") == []


def test_counts_match_findings_and_table_output(cli, api):
    lab(api)
    r, doc = run_json(cli)
    total = {}
    for f in doc["findings"]:
        total[f["level"]] = total.get(f["level"], 0) + 1
    assert doc["counts"] == total
    r = cli("audit", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Certificates" in r.stdout and "no certificate expires within 30 days" in r.stdout


def test_problems_hides_ok_lines(cli, api):
    lab(api)
    r, doc = run_json(cli, "--problems")
    assert r.exit_code == 0, r.output
    assert {f["level"] for f in doc["findings"]} <= {"WARN", "FAIL"}
    assert doc["findings"]


# ── failures and warnings ──────────────────────────────────────────────


def test_expired_certificate_fails_with_exit_2(cli, api):
    lab(api)
    certs = fixture("pve-9.1/node_certificates")
    certs[1]["notafter"] = int(NOW - 3 * DAY)
    api.route("GET", "/nodes/pve-lab3/certificates/info", certs)
    r, doc = run_json(cli)
    assert r.exit_code == 2, r.output
    fails = [f for f in lines(doc, "Certificates") if f["level"] == "FAIL"]
    assert len(fails) == 1
    assert "pve-lab3:pve-ssl.pem" in fails[0]["detail"] and "expired" in fails[0]["detail"]
    assert not [f for f in lines(doc, "Certificates") if f["level"] == "OK"]
    assert doc["counts"]["FAIL"] == 1


def test_expired_certificate_exit_2_in_table_mode(cli, api):
    lab(api)
    certs = fixture("pve-9.1/node_certificates")
    certs[1]["notafter"] = int(NOW - 3 * DAY)
    api.route("GET", "/nodes/{node}/certificates/info", certs)
    r = cli("audit", "-p", "p1")
    assert r.exit_code == 2, r.output
    assert "FAIL" in r.stdout and "expired" in r.stdout


def test_certificate_expiring_soon_warns(cli, api):
    lab(api)
    certs = fixture("pve-9.1/node_certificates")
    certs[1]["notafter"] = int(NOW + 20 * DAY)
    api.route("GET", "/nodes/{node}/certificates/info", certs)
    r, doc = run_json(cli)
    assert r.exit_code == 0, r.output
    assert levels(doc, "Certificates") == {"WARN"}


def test_root_token_without_privilege_separation_warns(cli, api):
    lab(api)
    users = fixture("pve-9.1/access_users_full")
    users[0]["tokens"] = [{"tokenid": "ci", "privsep": 0, "expire": 0, "comment": ""}]
    api.route("GET", "/access/users", users)
    r, doc = run_json(cli)
    assert r.exit_code == 0, r.output
    warn = [f for f in lines(doc, "API tokens") if f["level"] == "WARN"]
    assert len(warn) == 1 and "root@pam!ci" in warn[0]["detail"]
    assert lines(doc, "Users and tokens") == []  # no OK line next to the warning


def test_token_with_privilege_separation_is_fine(cli, api):
    lab(api)
    users = fixture("pve-9.1/access_users_full")
    users[0]["tokens"] = [{"tokenid": "ci", "privsep": 1, "expire": 0}]
    api.route("GET", "/access/users", users)
    r, doc = run_json(cli)
    assert lines(doc, "API tokens") == []
    assert levels(doc, "Users and tokens") == {"OK"}


def test_ceph_noout_left_set_warns(cli, api):
    lab(api)
    flags = fixture("pve-9.1/ceph_flags")
    for f in flags:
        if f["name"] == "noout":
            f["value"] = 1
    api.route("GET", "/cluster/ceph/flags", flags)
    r, doc = run_json(cli)
    assert r.exit_code == 0, r.output
    warn = lines(doc, "Ceph flags")
    assert len(warn) == 1 and warn[0]["level"] == "WARN"
    assert "noout" in warn[0]["detail"] and "nobackfill" not in warn[0]["detail"]
    assert levels(doc, "Ceph") == {"OK"}


def test_ceph_health_err_fails(cli, api):
    lab(api)
    status = fixture("pve-9.1/ceph_status")
    status["health"] = {"status": "HEALTH_ERR", "checks": {"OSD_DOWN": {}}, "mutes": []}
    api.route("GET", "/cluster/ceph/status", status)
    r, doc = run_json(cli)
    assert r.exit_code == 2, r.output
    assert lines(doc, "Ceph")[0]["level"] == "FAIL"
    assert "OSD_DOWN" in lines(doc, "Ceph")[0]["detail"]


def test_orphan_disk_warns(cli, api):
    guests = lab(api)
    assert 999 not in {g["vmid"] for g in guests}
    orphan = {"volid": "local-lvm:vm-999-disk-0", "vmid": 999, "content": "images", "format": "raw", "size": 1}
    owned = {"volid": "local-lvm:vm-301-disk-0", "vmid": 301, "content": "images", "format": "raw", "size": 1}
    api.route("GET", "/nodes/pve-lab1/storage/local-lvm/content", [owned, orphan])
    r, doc = run_json(cli)
    assert r.exit_code == 0, r.output
    found = lines(doc, "Orphan disks")
    assert len(found) == 1 and found[0]["level"] == "WARN"
    assert "local-lvm:vm-999-disk-0" in found[0]["detail"]
    assert "vm-301" not in found[0]["detail"]


def test_shared_storage_is_listed_once(cli, api):
    lab(api)
    run_json(cli)
    # labpool is a shared rbd storage: one node is enough
    assert len(api.called("GET", "/nodes/{node}/storage/labpool/content")) == 1
    # local-lvm is per node: every online node is listed
    assert len(api.called("GET", "/nodes/{node}/storage/local-lvm/content")) == 5


def test_storage_restricted_to_some_nodes_is_only_listed_there(cli, api):
    # Seen on the lab: a dir storage limited to pve-lab1 was listed on the
    # four other nodes, PVE refused, and the whole check read "not checked"
    lab(api)
    restricted = {"storage": "localdir", "type": "dir", "content": "images,rootdir",
                  "nodes": "pve-lab1", "path": "/var/lib/vz/localdir", "digest": "x"}
    api.route("GET", "/storage", _load("pve-9.1", "storage") + [restricted])

    def answer(call):
        if call.match["node"] != "pve-lab1":
            raise api.error(500, f"storage 'localdir' is not available on node '{call.match['node']}'")
        return []

    api.route("GET", "/nodes/{node}/storage/localdir/content", answer)
    r, doc = run_json(cli)
    assert r.exit_code == 0, r.output
    assert levels(doc, "Orphan disks") == {"OK"}
    assert [c.match["node"] for c in api.called("GET", "/nodes/{node}/storage/localdir/content")] == ["pve-lab1"]


def test_orphans_hidden_from_an_auditor_are_not_reported_ok(cli, api):
    # PVE lists an images/rootdir volume only to a user holding VM.Config.Disk
    # on its guest (or Datastore.Allocate on the storage), and drops the others
    # without an error: a PVEAuditor got "OK" from a scan that saw no volume
    lab(api)
    auditor = {"Datastore.Audit": 1, "VM.Audit": 1, "Sys.Audit": 1, "Pool.Audit": 1}
    api.route("GET", "/access/permissions", lambda c: {c.params["path"]: auditor})
    api.route("GET", "/nodes/{node}/storage/{storage}/content", [])
    r, doc = run_json(cli)
    assert r.exit_code == 0, r.output
    orphans = lines(doc, "Orphan disks")
    assert [f["level"] for f in orphans] == ["INFO"], orphans
    assert "not checked" in orphans[0]["detail"] and "VM.Config.Disk" in orphans[0]["detail"]


def test_orphans_found_elsewhere_still_name_the_unreadable_storages(cli, api):
    lab(api)
    orphan = {"volid": "local-lvm:vm-999-disk-0", "vmid": 999, "content": "images", "format": "raw", "size": 1}
    api.route("GET", "/nodes/pve-lab1/storage/local-lvm/content", [orphan])
    api.route("GET", "/nodes/{node}/storage/labpool/content",
              lambda c: api.forbidden("/storage/labpool", "Datastore.Audit"))
    r, doc = run_json(cli)
    assert {f["level"] for f in lines(doc, "Orphan disks")} == {"WARN", "INFO"}
    assert any("labpool" in f["detail"] for f in lines(doc, "Orphan disks") if f["level"] == "INFO")


def test_pool_limited_auditor_gets_no_ok_from_filtered_lists(cli, api):
    # Seen on the lab with PVEAuditor on /pool/x only: PVE silently filters
    # the ACL, user, guest and storage lists, and the audit read the empty
    # remainder as "0 ACL entries", "0 user(s)" and "OK" everywhere
    guests = lab(api)
    pool = [g for g in guests if g["vmid"] == guests[0]["vmid"]]
    pool_privs = {"VM.Audit": 1, "Datastore.Audit": 1, "Pool.Audit": 1}
    api.route("GET", "/access/permissions",
              lambda c: {c.params["path"]: pool_privs if c.params["path"].startswith("/pool") else {}})
    api.route("GET", "/cluster/resources", pool)
    api.route("GET", "/access/acl", [])
    api.route("GET", "/access/users", [])
    api.route("GET", "/storage", [])
    r, doc = run_json(cli)
    assert r.exit_code == 0, r.output
    expected = {
        "ACL": "Sys.Audit on /access",
        "Users and tokens": "Sys.Audit or User.Modify on /access/groups",
        "Snapshots": "VM.Audit on /vms",
        "Orphan disks": "VM.Audit on /vms",
    }
    for check, missing in expected.items():
        found = lines(doc, check)
        assert "OK" not in {f["level"] for f in found}, (check, found)
        assert any("not checked" in f["detail"] and missing in f["detail"] for f in found), (check, found)


def test_auditor_on_root_keeps_its_ok_lines(cli, api):
    # PVEAuditor on / reads the full lists: no needless "not checked"
    lab(api)
    auditor = {"Sys.Audit": 1, "VM.Audit": 1, "Datastore.Audit": 1, "Pool.Audit": 1}
    api.route("GET", "/access/permissions", lambda c: {c.params["path"]: auditor})
    r, doc = run_json(cli)
    for check in ("ACL", "Users and tokens", "Snapshots"):
        assert levels(doc, check) == {"OK"}, (check, lines(doc, check))


def test_skip_orphans_lists_no_storage(cli, api):
    lab(api)
    r, doc = run_json(cli, "--skip-orphans")
    assert r.exit_code == 0, r.output
    assert lines(doc, "Orphan disks") == []
    # The backup check reads backup content only; the orphan scan lists whole storages
    assert not [c for c in api.called("GET", "/nodes/{node}/storage/{storage}/content")
                if (c.params or {}).get("content") != "backup"]


def test_old_snapshot_warns(cli, api):
    lab(api)
    snaps = fixture("pve-9.1/qemu_snapshot") + [
        {"name": "before-upgrade", "snaptime": int(NOW - 40 * DAY), "description": ""}
    ]
    api.route("GET", "/nodes/pve-lab1/qemu/301/snapshot", snaps)
    r, doc = run_json(cli)
    warn = lines(doc, "Snapshots")
    assert len(warn) == 1 and warn[0]["level"] == "WARN"
    assert "301 (1)" in warn[0]["detail"]


# ── sources this user may not read ─────────────────────────────────────


def test_apt_update_denied_is_not_checked_never_ok(cli, api):
    lab(api)
    api.route("GET", "/nodes/pve-lab1/apt/update", api.forbidden("/nodes/pve-lab1", "Sys.Modify"))
    r, doc = run_json(cli)
    assert r.exit_code == 0, r.output
    updates = lines(doc, "Updates")
    assert len(updates) == 1
    assert updates[0]["level"] != "OK"
    assert "not checked" in updates[0]["detail"]
    assert "pve-lab1" in updates[0]["detail"]
    assert "missing privilege /nodes/pve-lab1, Sys.Modify" in updates[0]["detail"]


def test_apt_update_denied_everywhere_table_has_no_ok_line(cli, api):
    lab(api)
    api.route("GET", "/nodes/{node}/apt/update",
              lambda c: api.forbidden(f"/nodes/{c.match['node']}", "Sys.Modify"))
    r = cli("audit", "-p", "p1")
    assert r.exit_code == 0, r.output
    row = next(line for line in r.stdout.splitlines() if "Updates" in line)
    assert " OK " not in row and "not checked" in row and "Sys.Modify" in row


def test_certificates_denied_everywhere_never_ok(cli, api):
    lab(api)
    api.route("GET", "/nodes/{node}/certificates/info",
              lambda c: api.forbidden(f"/nodes/{c.match['node']}", "Sys.Audit"))
    r, doc = run_json(cli)
    assert r.exit_code == 0, r.output
    certs = lines(doc, "Certificates")
    assert [f["level"] for f in certs] == ["INFO"]
    assert "not checked" in certs[0]["detail"] and "Sys.Audit" in certs[0]["detail"]


def test_certificates_denied_on_one_node_never_ok(cli, api):
    lab(api)
    api.route("GET", "/nodes/pve-lab3/certificates/info", api.forbidden("/nodes/pve-lab3", "Sys.Audit"))
    r, doc = run_json(cli)
    certs = lines(doc, "Certificates")
    assert any("not checked" in f["detail"] for f in certs)
    assert "OK" not in {f["level"] for f in certs}


def test_users_denied_is_not_checked(cli, api):
    lab(api)
    api.route("GET", "/access/users", api.forbidden("/access", "User.Modify"))
    r, doc = run_json(cli)
    found = lines(doc, "Users and tokens")
    assert [f["level"] for f in found] == ["INFO"]
    assert "missing privilege /access, User.Modify" in found[0]["detail"]


def test_snapshots_unreadable_is_not_checked(cli, api):
    lab(api)
    deny = lambda c: api.forbidden(f"/vms/{c.match['vmid']}", "VM.Audit")  # noqa: E731
    api.route("GET", "/nodes/{node}/qemu/{vmid}/snapshot", deny)
    api.route("GET", "/nodes/{node}/lxc/{vmid}/snapshot", deny)
    r, doc = run_json(cli)
    snaps = lines(doc, "Snapshots")
    assert [f["level"] for f in snaps] == ["INFO"] and "not checked" in snaps[0]["detail"]


def test_storage_content_unreadable_is_not_checked(cli, api):
    lab(api)
    api.route("GET", "/nodes/{node}/storage/{storage}/content",
              lambda c: api.forbidden(f"/storage/{c.match['storage']}", "Datastore.Audit"))
    r, doc = run_json(cli)
    orphans = lines(doc, "Orphan disks")
    assert [f["level"] for f in orphans] == ["INFO"] and "not checked" in orphans[0]["detail"]


# ── the audit itself cannot run ────────────────────────────────────────


def test_unreachable_cluster_exits_1(cli, api):
    api.route("GET", "/nodes", api.error(595, "Connection refused"))
    r = cli("audit", "-p", "p1")
    assert r.exit_code == 1
    assert "Connection refused" in r.output
    assert api.writes() == []
