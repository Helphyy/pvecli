"""pvecli backup restore: a VM by archive, a CT by restore=1, never over a guest by surprise."""

from __future__ import annotations

import pytest

from tests.fake_api import upid
from tests.functional._backup_cluster import CONTENT, DAY, NOW, cluster, recorded

VM_VOL = CONTENT[("pve1", "local")][0]["volid"]   # backup of VM 100, on local@pve1
CT_VOL = CONTENT[("pve1", "local")][2]["volid"]   # backup of CT 200, on local@pve1
PBS_VOL = CONTENT[("*", "pbs")][0]["volid"]        # backup of VM 100, on the shared PBS

CT_CONFIG = "arch: amd64\nhostname: dns\nmemory: 512\nrootfs: local-lvm:vm-200-disk-0,size=8G\nmp0: fast:vm-200-disk-1,mp=/srv,size=2G\n"


@pytest.fixture
def lab(api):
    cluster(api)
    api.route("GET", "/cluster/nextid", lambda c: (
        api.error(400, f"VM {c.params['vmid']} already exists")
        if c.params and int(c.params["vmid"]) in (100, 101, 200, 9000) else
        str(c.params["vmid"]) if c.params else "900"
    ))
    api.route("POST", "/nodes/{node}/qemu", lambda c: upid(c.match["node"], "qmrestore", c.data["vmid"]))
    api.route("POST", "/nodes/{node}/lxc", lambda c: upid(c.match["node"], "vzrestore", c.data["vmid"]))
    api.route("GET", "/nodes/{node}/vzdump/extractconfig", CT_CONFIG)
    api.route("GET", "/nodes/{node}/qemu/{vmid}/status/current", lambda c: {"status": "stopped", "vmid": c.match["vmid"]})
    api.route("GET", "/nodes/{node}/lxc/{vmid}/status/current", lambda c: {"status": "stopped", "vmid": c.match["vmid"]})
    return api


def test_vm_restore_to_next_free_id(cli, api, lab):
    r = cli("backup", "restore", VM_VOL, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.called("POST", "/nodes/{node}/qemu")
    assert post.path == "/nodes/pve1/qemu"  # local storage: the node holding the file
    assert post.data == {"vmid": 900, "archive": VM_VOL}
    assert [c.method for c in api.writes()] == ["POST"]
    assert "900" in r.output


def test_ct_restore_keeps_the_original_rootfs_storage(cli, api, lab):
    # PVE puts a restored CT on 'local' when storage is omitted: not where it was
    r = cli("backup", "restore", CT_VOL, "--vmid", "905", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.called("POST", "/nodes/{node}/lxc")
    assert post.data == {"vmid": 905, "ostemplate": CT_VOL, "restore": 1, "storage": "local-lvm"}
    assert api.called("GET", "/nodes/pve1/vzdump/extractconfig")[0].params == {"volume": CT_VOL}


def test_ct_restore_with_storage_given(cli, api, lab):
    r = cli("backup", "restore", CT_VOL, "--vmid", "905", "--storage", "fast", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("POST", "/nodes/{node}/lxc")[0].data["storage"] == "fast"
    assert not api.called("GET", "/nodes/{node}/vzdump/extractconfig")


def test_start_unique_storage_sent(cli, api, lab):
    r = cli("backup", "restore", VM_VOL, "--vmid", "901", "--storage", "local-lvm", "--start", "--unique",
            "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("POST", "/nodes/{node}/qemu")[0].data == {
        "vmid": 901, "archive": VM_VOL, "storage": "local-lvm", "start": 1, "unique": 1}


def test_shared_backup_restores_on_the_original_guest_node(cli, api, lab):
    r = cli("backup", "restore", PBS_VOL, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("POST", "/nodes/{node}/qemu")[0].path == "/nodes/pve1/qemu"


def test_shared_backup_on_a_chosen_node(cli, api, lab):
    r = cli("backup", "restore", PBS_VOL, "--node", "pve2", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("POST", "/nodes/{node}/qemu")[0].path == "/nodes/pve2/qemu"


def test_local_backup_absent_from_the_chosen_node(cli, api, lab):
    r = cli("backup", "restore", VM_VOL, "--node", "pve2", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "pve2" in r.output and api.writes() == []


def test_unknown_volid(cli, api, lab):
    r = cli("backup", "restore", "local:backup/vzdump-qemu-100-1.vma.zst", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "not found" in r.output and api.writes() == []


def test_existing_vmid_is_refused_without_force(cli, api, lab):
    r = cli("backup", "restore", VM_VOL, "--vmid", "101", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "101" in r.output and "--force" in r.output
    assert api.writes() == []


def test_force_on_running_guest_is_refused(cli, api, lab):
    # /cluster/resources says stopped; the authoritative status says running
    api.route("GET", "/nodes/{node}/qemu/{vmid}/status/current", {"status": "running"})
    r = cli("backup", "restore", VM_VOL, "--vmid", "101", "--force", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "running" in r.output and "stop" in r.output.lower()
    assert api.writes() == []
    assert api.called("GET", "/nodes/pve2/qemu/101/status/current")


def test_force_needs_the_vmid_typed_again(cli, api, lab, ui):
    ui.text("100")
    r = cli("backup", "restore", VM_VOL, "--vmid", "100", "--force", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.called("POST", "/nodes/{node}/qemu")
    assert post.path == "/nodes/pve1/qemu"
    assert post.data == {"vmid": 100, "archive": VM_VOL, "force": 1}
    assert any("100" in q for _, q in ui.asked)


def test_force_wrong_vmid_typed_cancels(cli, api, lab, ui):
    ui.text("10")
    r = cli("backup", "restore", PBS_VOL, "--vmid", "101", "--force", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_force_restores_on_the_node_of_the_existing_guest(cli, api, lab, ui):
    ui.text("101")
    r = cli("backup", "restore", PBS_VOL, "--vmid", "101", "--force", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.called("POST", "/nodes/{node}/qemu")
    assert post.path == "/nodes/pve2/qemu"
    assert post.data == {"vmid": 101, "archive": PBS_VOL, "force": 1}


def test_force_with_a_local_archive_on_another_node_is_refused(cli, api, lab):
    # Guest 101 is on pve2, the file on local@pve1: PVE could not read it
    r = cli("backup", "restore", VM_VOL, "--vmid", "101", "--force", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "pve2" in r.output and api.writes() == []


def test_force_over_a_guest_of_the_other_type_is_refused(cli, api, lab):
    r = cli("backup", "restore", PBS_VOL, "--vmid", "200", "--force", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "container" in r.output.lower() and api.writes() == []


def test_restore_asks_before_writing(cli, api, lab, ui):
    ui.confirm(False)
    r = cli("backup", "restore", VM_VOL, "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    assert "Cancelled" in r.output


def test_failed_restore_task_exits_1(cli, api, lab):
    api.route("GET", "/nodes/{node}/tasks/{upid}/status", {"status": "stopped", "exitstatus": "unable to restore"})
    r = cli("backup", "restore", VM_VOL, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "unable to restore" in r.output


def test_nextid_taken_message_is_the_recorded_one(cli, api, lab):
    # lab1 answered 400 "VM 300 already exists" to /cluster/nextid?vmid=300
    err = recorded("backup_nextid_taken")
    api.route("GET", "/cluster/nextid", err)
    r = cli("backup", "restore", VM_VOL, "--vmid", "300", "-y", "-p", "p1")
    assert r.exit_code == 1 and api.writes() == []


def test_age_is_not_used_to_pick(cli, api, lab):
    # Sanity: the restore picks the volid given, not the newest backup
    r = cli("backup", "restore", CONTENT[("pve1", "local")][1]["volid"], "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("POST", "/nodes/{node}/qemu")[0].data["archive"].endswith(f"-{NOW - 8 * DAY}.vma.zst")
