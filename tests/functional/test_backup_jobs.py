"""pvecli backup jobs: CRUD on /cluster/backup, and running a job now."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.fake_api import FakeAPI
from tests.functional._backup_cluster import NOW, cluster, log, recorded, vol

JOB = {
    "id": "backup-daily", "type": "vzdump", "enabled": 1, "schedule": "02:00", "storage": "pbs",
    "vmid": "100,101", "mode": "snapshot", "compress": "zstd", "comment": "nightly",
    "next-run": NOW + 3600, "notes-template": "{{guestname}}", "prune-backups": {"keep-last": "3"},
    "fleecing": {"enabled": 1, "storage": "local-lvm"}, "repeat-missed": 0,
}


@pytest.fixture
def lab(api):
    cluster(api)
    api.route("GET", "/cluster/backup", [JOB, {"id": "weekly", "type": "vzdump", "enabled": 0, "all": 1,
                                              "exclude": "9000", "schedule": "sun 01:00", "storage": "local"}])
    api.route("GET", "/cluster/backup/{id}", lambda c: JOB if c.match["id"] == "backup-daily"
              else api.error(500, f"Job '{c.match['id']}' does not exist"))
    api.route("POST", "/cluster/backup", None)
    api.route("PUT", "/cluster/backup/{id}", None)
    api.route("DELETE", "/cluster/backup/{id}", None)
    return api


# ── list ────────────────────────────────────────────────────────────────


def test_list_table(cli, api, lab):
    r = cli("backup", "jobs", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    daily = next(line for line in r.stdout.splitlines() if "backup-daily" in line)
    assert "02:00" in daily and "100,101" in daily and "pbs" in daily and "yes" in daily
    weekly = next(line for line in r.stdout.splitlines() if "weekly" in line)
    assert "all except 9000" in weekly and "no" in weekly


def test_list_json(cli, api, lab):
    r = cli("backup", "jobs", "list", "-p", "p1", "--json")
    doc = json.loads(r.stdout)
    assert [j["id"] for j in doc] == ["backup-daily", "weekly"]


def test_list_disabled_job_shows_no_next_run(cli, api, lab):
    # Seen on PVE 9 (lab1): a disabled job still carries a next-run, but the
    # scheduler skips it, so the date must not be shown as if it would run
    when = 1795312800
    api.route("GET", "/cluster/backup", [{
        "id": "agenta-test", "type": "vzdump", "enabled": 0, "schedule": "sun 03:00", "storage": "local",
        "vmid": "950", "mode": "snapshot", "compress": "zstd", "next-run": when,
        "prune-backups": {"keep-last": "3"}, "comment": "agentA test",
    }])
    r = cli("backup", "jobs", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    import time
    assert time.strftime("%Y-%m-%d", time.localtime(when)) not in r.stdout
    row = next(line for line in r.stdout.splitlines() if "agenta-test" in line)
    assert "disabled" in row


def test_list_recorded_empty(cli, api, lab):
    api.route("GET", "/cluster/backup", recorded("backup_jobs"))
    r = cli("backup", "jobs", "list", "-p", "p1")
    assert r.exit_code == 0 and "No backup job" in r.output


# ── add / edit ──────────────────────────────────────────────────────────


def test_add_sends_selection_and_sane_defaults(cli, api, lab):
    r = cli("backup", "jobs", "add", "--schedule", "03:30", "--vmid", "100,200", "--storage", "pbs",
            "--keep-last", "7", "--comment", "test", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert post.path == "/cluster/backup"
    assert post.data == {"schedule": "03:30", "vmid": "100,200", "storage": "pbs", "mode": "snapshot",
                         "compress": "zstd", "enabled": 1, "prune-backups": "keep-last=7", "comment": "test"}


def test_add_all_with_exclude_disabled(cli, api, lab):
    r = cli("backup", "jobs", "add", "--schedule", "sat 02:00", "--all", "--exclude", "9000",
            "--storage", "local", "--disabled", "--id", "sat", "-p", "p1")
    assert r.exit_code == 0, r.output
    data = api.writes()[0].data
    assert data["all"] == 1 and data["exclude"] == "9000" and data["enabled"] == 0 and data["id"] == "sat"
    assert "vmid" not in data


@pytest.mark.parametrize("args", [[], ["--vmid", "100", "--all"], ["--pool", "P", "--vmid", "1"]])
def test_add_needs_exactly_one_selection(cli, api, lab, args):
    r = cli("backup", "jobs", "add", "--schedule", "02:00", "--storage", "pbs", *args, "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_add_needs_a_storage(cli, api, lab):
    r = cli("backup", "jobs", "add", "--schedule", "02:00", "--all", "-p", "p1")
    assert r.exit_code != 0 and api.writes() == []


def test_edit_only_sends_what_changes(cli, api, lab):
    r = cli("backup", "jobs", "edit", "backup-daily", "--schedule", "04:00", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.path == "/cluster/backup/backup-daily" and put.data == {"schedule": "04:00"}


def test_edit_switching_selection_clears_the_other_ones(cli, api, lab):
    r = cli("backup", "jobs", "edit", "backup-daily", "--pool", "P", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes()[0].data == {"pool": "P", "all": 0, "delete": "vmid,exclude"}
    api.calls.clear()
    r = cli("backup", "jobs", "edit", "backup-daily", "--vmid", "100", "-p", "p1")
    assert api.writes()[0].data == {"vmid": "100", "all": 0, "delete": "pool,exclude"}


def test_edit_nothing_given(cli, api, lab):
    r = cli("backup", "jobs", "edit", "backup-daily", "-p", "p1")
    assert r.exit_code == 1 and api.writes() == []


def test_edit_unknown_job(cli, api, lab):
    r = cli("backup", "jobs", "edit", "nope", "--schedule", "01:00", "-p", "p1")
    assert r.exit_code == 1 and api.writes() == []
    assert "nope" in r.output


# ── enable / disable / remove ──────────────────────────────────────────


def test_enable_disable(cli, api, lab):
    assert cli("backup", "jobs", "disable", "backup-daily", "-p", "p1").exit_code == 0
    assert cli("backup", "jobs", "enable", "backup-daily", "-p", "p1").exit_code == 0
    assert [c.data for c in api.writes()] == [{"enabled": 0}, {"enabled": 1}]


def test_remove_asks(cli, api, lab, ui):
    ui.confirm(False)
    r = cli("backup", "jobs", "remove", "backup-daily", "-p", "p1")
    assert api.writes() == [] and "Cancelled" in r.output
    ui.confirm(True)
    r = cli("backup", "jobs", "remove", "backup-daily", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == ["/cluster/backup/backup-daily"]


def test_remove_unknown_job(cli, api, lab):
    r = cli("backup", "jobs", "remove", "nope", "-y", "-p", "p1")
    assert r.exit_code == 1 and api.writes() == []


# ── run ─────────────────────────────────────────────────────────────────


class Runner:
    def __init__(self, api: FakeAPI) -> None:
        self.content: dict = {}
        self.refuse_job_id: Any = None
        api.route("GET", "/cluster/backup/backup-daily/included_volumes", {"children": [
            {"id": 100, "type": "qemu", "children": []}, {"id": 101, "type": "qemu", "children": []}]})
        api.route("POST", "/nodes/{node}/vzdump", self.vzdump)
        api.route("GET", "/nodes/{node}/tasks/{upid}/log", self.log)
        cluster(api, self.content)

    def vzdump(self, call: Any) -> Any:
        if self.refuse_job_id is not None and "job-id" in call.data:
            return self.refuse_job_id
        node = call.match["node"]
        for v in str(call.data["vmid"]).split(","):
            self.content.setdefault(("*", "pbs"), []).append(vol("pbs", int(v), "qemu", NOW + 1))
        return f"UPID:{node}:0000AAAA:0000BBBB:{NOW:08X}:vzdump::root@pam:{call.data['vmid']}"

    def log(self, call: Any) -> list[dict]:
        vmids = call.match["upid"].rsplit(":", 1)[1].split(",")
        return log(*[f"INFO: Finished Backup of VM {v} (00:00:01)" for v in vmids])


@pytest.fixture
def runner(lab, api):
    return Runner(api)


def test_run_sends_the_job_options_per_node_with_job_id(cli, api, runner):
    r = cli("backup", "jobs", "run", "backup-daily", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    posts = {c.match["node"]: c.data for c in api.called("POST", "/nodes/{node}/vzdump")}
    assert set(posts) == {"pve1", "pve2"}
    assert posts["pve1"]["vmid"] == "100" and posts["pve2"]["vmid"] == "101"
    for d in posts.values():
        assert d["job-id"] == "backup-daily"
        assert d["storage"] == "pbs" and d["mode"] == "snapshot" and d["compress"] == "zstd"
        # Objects of the job come back as property strings
        assert d["prune-backups"] == "keep-last=3"
        assert d["fleecing"] == "enabled=1,storage=local-lvm"
        assert d["notes-template"] == "{{guestname}}"
        # Job-only keys never reach vzdump
        for key in ("id", "type", "schedule", "enabled", "comment", "next-run", "repeat-missed"):
            assert key not in d
    assert "2/2 guest(s) backed up" in r.output


def test_run_falls_back_without_job_id_when_refused(cli, api, runner):
    # Only root@pam may set job-id
    runner.refuse_job_id = api.forbidden("/", "user != root@pam")
    r = cli("backup", "jobs", "run", "backup-daily", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    posts = api.called("POST", "/nodes/{node}/vzdump")
    assert len(posts) == 4
    assert ["job-id" in c.data for c in posts].count(False) == 2


def test_run_asks_with_the_count(cli, api, runner, ui):
    ui.confirm(False)
    r = cli("backup", "jobs", "run", "backup-daily", "-p", "p1")
    assert api.writes() == []
    assert "2 guest(s)" in r.output


def test_run_unknown_job(cli, api, runner):
    r = cli("backup", "jobs", "run", "nope", "-y", "-p", "p1")
    assert r.exit_code == 1 and api.writes() == []
