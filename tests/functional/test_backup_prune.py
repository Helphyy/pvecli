"""pvecli backup prune: preview from PVE first, delete only once confirmed."""

from __future__ import annotations

import pytest

from tests.fake_api import upid
from tests.functional._backup_cluster import NOW, cluster, recorded


def _marks(node: str) -> list[dict]:
    v = 100 if node == "pve1" else 101
    return [
        {"volid": f"local:backup/vzdump-qemu-{v}-new.vma.zst", "vmid": v, "type": "qemu", "ctime": NOW, "mark": "keep"},
        {"volid": f"local:backup/vzdump-qemu-{v}-old.vma.zst", "vmid": v, "type": "qemu", "ctime": NOW - 9, "mark": "remove"},
        {"volid": f"local:backup/vzdump-qemu-{v}-pin.vma.zst", "vmid": v, "type": "qemu", "ctime": NOW - 99, "mark": "protected"},
    ]


@pytest.fixture
def lab(api):
    cluster(api)
    api.route("GET", "/nodes/{node}/storage/{storage}/prunebackups", lambda c: _marks(c.match["node"]))
    api.route("DELETE", "/nodes/{node}/storage/{storage}/prunebackups",
              lambda c: upid(c.match["node"], "prunebackups", ""))
    return api


def _deletes(api):
    return api.called("DELETE", "/nodes/{node}/storage/{storage}/prunebackups")


def test_preview_then_prune_each_node_of_a_local_storage(cli, api, lab, ui):
    ui.confirm(True)
    r = cli("backup", "prune", "local", "--keep-last", "1", "--keep-daily", "7", "-p", "p1")
    assert r.exit_code == 0, r.output
    previews = api.called("GET", "/nodes/{node}/storage/local/prunebackups")
    assert sorted(c.path for c in previews) == ["/nodes/pve1/storage/local/prunebackups",
                                                "/nodes/pve2/storage/local/prunebackups"]
    assert all(c.params == {"prune-backups": "keep-last=1,keep-daily=7"} for c in previews)
    # The preview lists what goes and what stays
    assert "vzdump-qemu-100-old" in r.stdout and "vzdump-qemu-101-old" in r.stdout
    row = next(line for line in r.stdout.splitlines() if "vzdump-qemu-101-old" in line)
    assert "pve2" in row
    assert "2 backup(s) to remove" in r.output
    deletes = _deletes(api)
    assert sorted(c.path for c in deletes) == ["/nodes/pve1/storage/local/prunebackups",
                                               "/nodes/pve2/storage/local/prunebackups"]
    assert all(c.params == {"prune-backups": "keep-last=1,keep-daily=7"} for c in deletes)


def test_dry_run_never_deletes_nor_asks(cli, api, lab):
    r = cli("backup", "prune", "local", "--keep-last", "1", "--dry-run", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes() == []
    assert "vzdump-qemu-100-old" in r.stdout


def test_cancel_deletes_nothing(cli, api, lab, ui):
    ui.confirm(False)
    r = cli("backup", "prune", "local", "--keep-last", "1", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == [] and "Cancelled" in r.output


def test_vmid_and_type_are_forwarded(cli, api, lab):
    r = cli("backup", "prune", "pbs", "--keep-last", "2", "--vmid", "100", "--type", "qemu", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    # Shared storage: one preview and one prune, through one node
    [preview] = api.called("GET", "/nodes/{node}/storage/pbs/prunebackups")
    [delete] = _deletes(api)
    want = {"prune-backups": "keep-last=2", "vmid": 100, "type": "qemu"}
    assert preview.params == want and delete.params == want


def test_nothing_to_remove(cli, api, lab):
    api.route("GET", "/nodes/{node}/storage/{storage}/prunebackups",
              [{"volid": "local:backup/x", "vmid": 1, "type": "qemu", "ctime": NOW, "mark": "keep"}])
    r = cli("backup", "prune", "local", "--keep-last", "5", "-y", "-p", "p1")
    assert r.exit_code == 0
    assert "Nothing to prune" in r.output and api.writes() == []


def test_storage_retention_used_when_no_keep_option(cli, api, lab):
    r = cli("backup", "prune", "local", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert all(c.params is None or "prune-backups" not in c.params
               for c in api.called("GET", "/nodes/{node}/storage/{storage}/prunebackups"))
    assert "retention of the storage" in r.output


def test_no_retention_anywhere_explains(cli, api, lab):
    api.route("GET", "/nodes/{node}/storage/{storage}/prunebackups", recorded("backup_prune_unconfigured"))
    r = cli("backup", "prune", "local", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "--keep-last" in r.output and api.writes() == []


def test_not_a_backup_storage(cli, api, lab):
    r = cli("backup", "prune", "local-lvm", "--keep-last", "1", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "local-lvm" in r.output and api.writes() == []


def test_failed_prune_task_exits_1(cli, api, lab):
    api.route("GET", "/nodes/{node}/tasks/{upid}/status", {"status": "stopped", "exitstatus": "prune failed"})
    r = cli("backup", "prune", "pbs", "--keep-last", "1", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "prune failed" in r.output


def test_recorded_preview_of_lab1(cli, api, lab):
    # keep-last=1 on VM 300 with a single backup: it is kept
    api.route("GET", "/nodes/{node}/storage/{storage}/prunebackups", recorded("backup_prune_preview"))
    r = cli("backup", "prune", "local", "--keep-last", "1", "--vmid", "300", "-y", "-p", "p1")
    assert r.exit_code == 0
    assert "Nothing to prune" in r.output and api.writes() == []
