"""pvecli backup run: one vzdump per node, then a per guest result checked twice."""

from __future__ import annotations

from typing import Any

import pytest

from tests.fake_api import FakeAPI
from tests.functional._backup_cluster import NOW, cluster, log, recorded, vol


class Lab:
    """vzdump that really 'writes' archives, unless told otherwise per guest."""

    def __init__(self, api: FakeAPI) -> None:
        self.api = api
        self.content: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.outcome: dict[int, str] = {}   # vmid -> "ok" (default), "fail:<why>", "skip", "ghost"
        self.status: dict[str, str] = {}    # node -> task exit status
        cluster(api, self.content)
        api.route("GET", "/nodes/{node}/vzdump/defaults", {"storage": "local", "mode": "snapshot", "compress": "0"})
        api.route("POST", "/nodes/{node}/vzdump", self.vzdump)
        api.route("GET", "/nodes/{node}/tasks/{upid}/status", self.task_status)
        api.route("GET", "/nodes/{node}/tasks/{upid}/log", self.task_log)
        self.logs: dict[str, list[dict]] = {}

    def vzdump(self, call: Any) -> str:
        node = call.match["node"]
        upid = f"UPID:{node}:0000AAAA:0000BBBB:{NOW:08X}:vzdump::root@pam:"
        storage = call.data["storage"]
        lines = [f"INFO: starting new backup job: vzdump {call.data['vmid']}"]
        failed = False
        for v in (int(x) for x in str(call.data["vmid"]).split(",")):
            what = self.outcome.get(v, "ok")
            kind = "lxc" if v == 200 else "qemu"
            if what == "skip":
                continue  # what PVE really does: the guest is in no line of the log
            lines.append(f"INFO: Starting Backup of VM {v} ({kind})")
            if what.startswith("fail:"):
                lines.append(f"ERROR: Backup of VM {v} failed - {what[5:]}")
                failed = True
                continue
            lines.append(f"INFO: Finished Backup of VM {v} (00:00:02)")
            if what != "ghost":
                key = ("*", storage) if storage == "pbs" else (node, storage)
                self.content.setdefault(key, []).append(vol(storage, v, kind, NOW + 1))
        lines.append("TASK ERROR: job errors" if failed else "TASK OK")
        self.logs[upid] = log(*lines)
        self.status[upid] = self.status.get(node, "job errors" if failed else "OK")
        return upid

    def task_status(self, call: Any) -> dict:
        return {"status": "stopped", "exitstatus": self.status[call.match["upid"]]}

    def task_log(self, call: Any) -> list[dict]:
        return self.logs[call.match["upid"]]


@pytest.fixture
def lab(api):
    return Lab(api)


def _posts(api) -> dict[str, dict]:
    return {c.match["node"]: c.data for c in api.called("POST", "/nodes/{node}/vzdump")}


def test_one_vzdump_per_node_with_explicit_safe_defaults(cli, api, lab):
    r = cli("backup", "run", "100,101,200", "--storage", "pbs", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    posts = _posts(api)
    assert set(posts) == {"pve1", "pve2"}
    assert posts["pve1"]["vmid"] == "100,200" and posts["pve2"]["vmid"] == "101"
    for data in posts.values():
        assert data["storage"] == "pbs"
        assert data["mode"] == "snapshot"
        # PVE defaults: compress=0 (uncompressed) and remove=1 (prune old
        # backups by the storage retention): both against a one-off backup
        assert data["compress"] == "zstd"
        assert data["remove"] == 0
        assert "protected" not in data and "notes-template" not in data
    assert "3/3 guest(s) backed up" in r.output


def test_report_names_each_new_archive(cli, api, lab):
    r = cli("backup", "run", "100", "--storage", "local", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert f"local:backup/vzdump-qemu-100-{NOW + 1}.vma.zst" in r.stdout


def test_confirmation_shows_the_count_and_cancel_writes_nothing(cli, api, lab, ui):
    ui.confirm(False)
    r = cli("backup", "run", "--tag", "web", "--storage", "pbs", "-p", "p1")
    assert r.exit_code == 0
    assert "2 guest(s)" in ui.asked[0][1] or "2 guest(s)" in r.output
    assert api.writes() == []
    assert "Cancelled" in r.output


def test_task_ok_but_guest_skipped_is_a_failure(cli, api, lab):
    lab.outcome[101] = "skip"
    r = cli("backup", "run", "100,101", "--storage", "pbs", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "101" in r.output and "absent from the task log" in r.output
    assert "1/2 guest(s) backed up" in r.output


def test_vzdump_answering_ok_without_a_task_is_a_failure(cli, api, lab):
    # Recorded: every guest asked had moved to another node; PVE answers
    # "OK" and starts nothing
    api.route("POST", "/nodes/{node}/vzdump", recorded("backup_vzdump_all_guests_external"))
    r = cli("backup", "run", "100", "--storage", "local", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "started no task" in r.output
    assert not api.called("GET", "/nodes/{node}/tasks/{upid}/status")
    assert "0/1 guest(s) backed up" in r.output


def test_failed_guest_reason_and_exit_1(cli, api, lab):
    lab.outcome[200] = "fail:unable to create temporary directory"
    r = cli("backup", "run", "100,200", "--storage", "local", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "unable to create temporary directory" in r.output
    assert "1/2 guest(s) backed up" in r.output


def test_finished_in_log_but_no_archive_on_storage_is_a_failure(cli, api, lab):
    lab.outcome[100] = "ghost"
    r = cli("backup", "run", "100", "--storage", "local", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "no new archive" in r.output


def test_without_storage_uses_the_node_default_explicitly(cli, api, lab):
    r = cli("backup", "run", "100", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _posts(api)["pve1"]["storage"] == "local"
    assert api.called("GET", "/nodes/pve1/vzdump/defaults")


def test_notes_protected_mode_compress(cli, api, lab):
    r = cli("backup", "run", "100", "--storage", "local", "--notes", "before upgrade {{guestname}}",
            "--protected", "--mode", "stop", "--compress", "gzip", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    data = _posts(api)["pve1"]
    assert data["notes-template"] == "before upgrade {{guestname}}"
    assert data["protected"] == 1 and data["mode"] == "stop" and data["compress"] == "gzip"


@pytest.mark.parametrize("opt,value", [("--mode", "hot"), ("--compress", "xz")])
def test_invalid_mode_or_compress_is_refused_before_anything(cli, api, lab, opt, value):
    r = cli("backup", "run", "100", opt, value, "-y", "-p", "p1")
    assert r.exit_code != 0
    assert api.writes() == []


def test_unknown_storage_refused(cli, api, lab):
    r = cli("backup", "run", "100", "--storage", "local-lvm", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "local-lvm" in r.output and api.writes() == []


def test_unknown_guest_id_is_refused_before_anything(cli, api, lab):
    r = cli("backup", "run", "100,777", "--storage", "pbs", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "777" in r.output and api.writes() == []


def test_no_target_is_refused(cli, api, lab):
    r = cli("backup", "run", "--storage", "pbs", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "--all" in r.output and api.writes() == []


def test_all_skips_templates_but_an_explicit_template_id_is_kept(cli, api, lab):
    r = cli("backup", "run", "--all", "--storage", "pbs", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    sent = {v for d in _posts(api).values() for v in d["vmid"].split(",")}
    assert sent == {"100", "101", "200"}
    api.calls.clear()
    r = cli("backup", "run", "9000", "--storage", "pbs", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _posts(api)["pve2"]["vmid"] == "9000"


def test_node_and_pool_filters(cli, api, lab):
    r = cli("backup", "run", "--pool", "P", "--node", "pve1", "--storage", "pbs", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _posts(api) == {"pve1": _posts(api)["pve1"]}
    assert _posts(api)["pve1"]["vmid"] == "100,200"


def test_refused_vzdump_on_one_node_does_not_stop_the_other(cli, api, lab):
    api.route("POST", "/nodes/pve2/vzdump", api.forbidden("/storage/pbs", "Datastore.AllocateSpace"))
    r = cli("backup", "run", "100,101", "--storage", "pbs", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Datastore.AllocateSpace" in r.output
    assert "1/2 guest(s) backed up" in r.output


@pytest.mark.parametrize("listed", ["", "VM 100"])
def test_a_guest_created_seconds_ago_is_named_from_its_config(cli, api, lab, ui, listed):
    # The cluster list has no name yet for a brand new guest, or the
    # placeholder "VM 100" (seen on lab1, PVE 9.1.1, right after a clone)
    from tests.functional._backup_cluster import GUESTS
    api.route("GET", "/cluster/resources", [{**g, "name": listed} if g["vmid"] == 100 else g for g in GUESTS])
    api.route("GET", "/nodes/{node}/qemu/{vmid}/config", {"name": "fresh-vm"})
    ui.confirm(False)
    r = cli("backup", "run", "100", "--storage", "local", "-p", "p1")
    assert "100 (fresh-vm)" in r.output
