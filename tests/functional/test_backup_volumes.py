"""pvecli backup remove / protect / unprotect / notes / files: one backup volume at a time."""

from __future__ import annotations

import json

import pytest

from tests.fake_api import upid
from tests.functional._backup_cluster import CONTENT, cluster, enc, log, recorded

VM_VOL = CONTENT[("pve1", "local")][0]["volid"]
PROTECTED_VOL = CONTENT[("pve1", "local")][1]["volid"]
CT_VOL = CONTENT[("pve1", "local")][2]["volid"]
PBS_VOL = CONTENT[("*", "pbs")][0]["volid"]


@pytest.fixture
def lab(api):
    cluster(api)
    protected = {PROTECTED_VOL}

    def attrs(call):
        from urllib.parse import unquote

        volid = unquote(call.match["volume"])
        return {"format": "raw", "notes": "", "protected": int(volid in protected), "size": 512, "used": 4096}

    api.route("GET", "/nodes/{node}/storage/{storage}/content/{volume}", attrs)
    api.route("DELETE", "/nodes/{node}/storage/{storage}/content/{volume}",
              lambda c: upid(c.match["node"], "imgdel", ""))
    api.route("PUT", "/nodes/{node}/storage/{storage}/content/{volume}", None)
    return api


def _deleted(api) -> list[str]:
    return [c.path for c in api.called("DELETE", "/nodes/{node}/storage/{storage}/content/{volume}")]


# ── remove ──────────────────────────────────────────────────────────────


def test_remove_asks_then_deletes_where_the_file_is(cli, api, lab, ui):
    ui.confirm(True)
    r = cli("backup", "remove", VM_VOL, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _deleted(api) == [f"/nodes/pve1/storage/local/content/{enc(VM_VOL)}"]
    assert len(ui.asked) == 1


def test_remove_cancelled_writes_nothing(cli, api, lab, ui):
    ui.confirm(False)
    r = cli("backup", "remove", VM_VOL, "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == [] and "Cancelled" in r.output


def test_remove_protected_is_refused_and_says_why(cli, api, lab):
    r = cli("backup", "remove", PROTECTED_VOL, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "protected" in r.output and "backup unprotect" in r.output
    assert api.writes() == []


def test_remove_several_one_protected(cli, api, lab):
    r = cli("backup", "remove", VM_VOL, PROTECTED_VOL, CT_VOL, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert sorted(_deleted(api)) == sorted(
        f"/nodes/pve1/storage/local/content/{enc(v)}" for v in (VM_VOL, CT_VOL))


def test_remove_unknown_volume(cli, api, lab):
    r = cli("backup", "remove", "local:backup/vzdump-qemu-1-1.vma.zst", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "not found" in r.output and api.writes() == []


def test_remove_on_shared_storage_goes_through_one_node(cli, api, lab):
    r = cli("backup", "remove", PBS_VOL, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [path] = _deleted(api)
    assert path.endswith(f"/storage/pbs/content/{enc(PBS_VOL)}")


def test_remove_waits_for_the_delete_task(cli, api, lab):
    api.route("GET", "/nodes/{node}/tasks/{upid}/status", {"status": "stopped", "exitstatus": "rm failed"})
    r = cli("backup", "remove", VM_VOL, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "rm failed" in r.output


def test_remove_of_a_locked_pbs_snapshot_says_why(cli, api, lab):
    # Seen on lab1 (PVE 9 + PBS 4) right after `backup files` on a VM backup:
    # the file-restore VM keeps the snapshot locked for a while. The task ends
    # with a truncated status, the cause is only in its log.
    api.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "proxmox-backup-client failed: }"})
    api.route("GET", "/nodes/{node}/tasks/{upid}/log", log(
        "Error: failed to delete snapshot - Error {",
        '    context: "while destroying snapshot \'BackupDir { store: \\"labds\\" }\'",',
        "    source: Error {",
        '        context: "unable to acquire snapshot lock \\"/run/proxmox-backup/locks/labds/vm-953-x\\"",',
        '        source: "Unable to acquire lock - Resource temporarily unavailable (os error 11)",',
        "    },",
        "TASK ERROR: proxmox-backup-client failed: }",
    ))
    r = cli("backup", "remove", PBS_VOL, "-y", "-p", "p1")
    assert r.exit_code == 1
    out = " ".join(r.output.split())
    assert "in use" in out and "retry" in out
    # nothing was deleted: not a success line
    assert "\u2713 0/1" not in out


def test_remove_failure_shows_the_error_line_of_the_task_log(cli, api, lab):
    api.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "proxmox-backup-client failed: }"})
    api.route("GET", "/nodes/{node}/tasks/{upid}/log", log(
        "Error: failed to delete snapshot - permission denied on datastore",
        "TASK ERROR: proxmox-backup-client failed: }",
    ))
    r = cli("backup", "remove", PBS_VOL, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "permission denied on datastore" in " ".join(r.output.split())


def test_not_a_backup_volume_is_refused(cli, api, lab):
    r = cli("backup", "remove", "local:iso/debian.iso", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "not a backup" in r.output and api.writes() == []


# ── protect, unprotect, notes ──────────────────────────────────────────


def test_protect_and_unprotect(cli, api, lab):
    r = cli("backup", "protect", VM_VOL, PBS_VOL, "-p", "p1")
    assert r.exit_code == 0, r.output
    puts = api.called("PUT", "/nodes/{node}/storage/{storage}/content/{volume}")
    assert [c.data for c in puts] == [{"protected": 1}, {"protected": 1}]
    assert puts[0].path == f"/nodes/pve1/storage/local/content/{enc(VM_VOL)}"
    api.calls.clear()
    r = cli("backup", "unprotect", PROTECTED_VOL, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.data for c in api.writes()] == [{"protected": 0}]


def test_notes(cli, api, lab):
    r = cli("backup", "notes", VM_VOL, "before the 9.2 upgrade", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.data == {"notes": "before the 9.2 upgrade"}


def test_protect_refused_by_pve_exits_1(cli, api, lab):
    api.route("PUT", "/nodes/{node}/storage/{storage}/content/{volume}",
              api.forbidden("/storage/local", "Datastore.Allocate"))
    r = cli("backup", "protect", VM_VOL, "-p", "p1")
    assert r.exit_code == 1
    assert "Datastore.Allocate" in r.output


# ── files (PBS only, read only) ─────────────────────────────────────────

ROOT = [
    {"filepath": "L3Jvb3QucHhhci5kaWR4", "leaf": 0, "text": "root.pxar.didx", "type": "v"},
    {"filepath": "L2luZGV4Lmpzb24uYmxvYg==", "leaf": 1, "text": "index.json.blob", "size": 512, "type": "f"},
]
ROOT_PXAR = [{"filepath": "L3Jvb3QucHhhci5kaWR4L2V0Yw==", "leaf": 0, "text": "etc", "type": "d", "mtime": 1795000000}]
ETC = [{"filepath": "L3Jvb3QucHhhci5kaWR4L2V0Yy9ob3N0bmFtZQ==", "leaf": 1, "text": "hostname", "type": "f",
        "size": 4, "mtime": 1795000000}]


@pytest.fixture
def pbs(lab, api):
    tree = {"/": ROOT, ROOT[0]["filepath"]: ROOT_PXAR, ROOT_PXAR[0]["filepath"]: ETC}
    api.route("GET", "/nodes/{node}/storage/{storage}/file-restore/list", lambda c: tree[c.params["filepath"]])
    return api


def test_files_root(cli, api, pbs):
    r = cli("backup", "files", PBS_VOL, "-p", "p1")
    assert r.exit_code == 0, r.output
    [call] = api.called("GET", "/nodes/{node}/storage/pbs/file-restore/list")
    assert call.params == {"volume": PBS_VOL, "filepath": "/"}
    assert "root.pxar.didx" in r.stdout and "index.json.blob" in r.stdout
    assert api.writes() == []


def test_files_walks_the_path_with_the_ids_the_api_gives(cli, api, pbs):
    r = cli("backup", "files", PBS_VOL, "/root.pxar.didx/etc", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    asked = [c.params["filepath"] for c in api.called("GET", "/nodes/{node}/storage/pbs/file-restore/list")]
    assert asked == ["/", ROOT[0]["filepath"], ROOT_PXAR[0]["filepath"]]
    doc = json.loads(r.stdout)
    assert doc == [{"name": "hostname", "type": "file", "size": 4, "mtime": 1795000000}]


def test_files_unknown_path(cli, api, pbs):
    r = cli("backup", "files", PBS_VOL, "root.pxar.didx/nope", "-p", "p1")
    assert r.exit_code == 1
    assert "nope" in r.output and "etc" in r.output


def test_files_on_a_non_pbs_storage_says_so(cli, api, pbs):
    r = cli("backup", "files", VM_VOL, "-p", "p1")
    assert r.exit_code == 1
    assert "Proxmox Backup Server" in r.output and "dir" in r.output
    assert not api.called("GET", "/nodes/{node}/storage/{storage}/file-restore/list")


def test_recorded_answers_of_lab1(cli, api, lab):
    # Diskless VM 300 on the lab PBS: an empty root
    api.route("GET", "/nodes/{node}/storage/{storage}/file-restore/list", recorded("backup_file_restore_pbs_root"))
    r = cli("backup", "files", PBS_VOL, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "empty" in r.output.lower()
