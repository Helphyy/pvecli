"""pvecli backup status: which job covers each guest, its last backup, its last run."""

from __future__ import annotations

import json

from tests.fake_api import FakeAPI
from tests.functional._backup_cluster import DAY, NOW, cluster, log

U1 = f"UPID:pve1:0000AAAA:0000BBBB:{NOW - 3700:08X}:vzdump::root@pam:"
U2 = f"UPID:pve2:0000AAAA:0000BBBB:{NOW - 2 * DAY:08X}:vzdump:101:root@pam:"
U_OLD = f"UPID:pve2:0000AAAA:0000BBBB:{NOW - 9 * DAY:08X}:vzdump:101:root@pam:"

JOBS = [
    {"id": "backup-daily", "type": "vzdump", "enabled": 1, "schedule": "02:00", "storage": "pbs",
     "vmid": "100,101", "mode": "snapshot"},
    {"id": "backup-off", "type": "vzdump", "enabled": 0, "schedule": "sun 03:00", "storage": "local", "pool": "P"},
]


def _included(*vmids: int) -> dict:
    return {"children": [{"id": v, "type": "qemu", "name": f"g{v}", "children": []} for v in vmids]}


def status_cluster(api: FakeAPI) -> FakeAPI:
    cluster(api)
    api.route("GET", "/cluster/backup", JOBS)
    api.route("GET", "/cluster/backup/backup-daily/included_volumes", _included(100, 101))
    api.route("GET", "/cluster/backup/backup-off/included_volumes", _included(100, 200))
    api.route("GET", "/cluster/backup-info/not-backed-up", [{"vmid": 9000, "name": "tpl", "type": "qemu"}])
    api.route("GET", "/access/permissions",
              lambda c: {c.params["path"]: {"Sys.Audit": 1, "VM.Backup": 1}})
    tasks = {
        "pve1": [{"upid": U1, "id": "", "type": "vzdump", "status": "job errors", "starttime": NOW - 3700,
                  "endtime": NOW - 3600, "node": "pve1"}],
        "pve2": [
            {"upid": U2, "id": "101", "type": "vzdump", "status": "job errors", "starttime": NOW - 2 * DAY,
             "endtime": NOW - 2 * DAY + 60, "node": "pve2"},
            {"upid": U_OLD, "id": "101", "type": "vzdump", "status": "OK", "starttime": NOW - 9 * DAY,
             "endtime": NOW - 9 * DAY + 60, "node": "pve2"},
        ],
    }
    api.route("GET", "/nodes/{node}/tasks", lambda c: tasks.get(c.match["node"], []))
    logs = {
        U1: log("INFO: Starting Backup of VM 100 (qemu)", "INFO: Finished Backup of VM 100 (00:00:05)",
                "INFO: Starting Backup of VM 200 (lxc)",
                "ERROR: Backup of VM 200 failed - unable to create temporary directory",
                "TASK ERROR: job errors"),
        U2: log("INFO: Starting Backup of VM 101 (qemu)",
                "ERROR: Backup of VM 101 failed - no space left on device", "TASK ERROR: job errors"),
    }
    api.route("GET", "/nodes/{node}/tasks/{upid}/log", lambda c: logs[c.match["upid"]])
    return api


def _doc(cli, *args: str) -> dict[int, dict]:
    r = cli("backup", "status", "-p", "p1", "--json", *args)
    assert r.exit_code == 0, r.output
    return {row["vmid"]: row for row in json.loads(r.stdout)}


def test_coverage_last_backup_and_last_run_per_guest(cli, api):
    status_cluster(api)
    rows = _doc(cli)
    assert set(rows) == {100, 101, 200, 9000}
    assert rows[100]["jobs"] == ["backup-daily", "backup-off (disabled)"]
    assert rows[101]["jobs"] == ["backup-daily"]
    assert rows[200]["jobs"] == ["backup-off (disabled)"]
    assert rows[9000]["jobs"] == []
    # Newest backup on any storage: the PBS one for 100, local for 200
    assert rows[100]["last_backup"] == NOW - 3600 and rows[100]["last_volid"].startswith("pbs:")
    assert rows[200]["last_backup"] == NOW - 3 * DAY
    assert rows[9000]["last_backup"] is None
    # Last vzdump run: a multi-guest task is split per guest from its log
    assert rows[100]["last_run"]["state"] == "ok"
    assert rows[200]["last_run"] == {"state": "failed", "time": NOW - 3700,
                                     "detail": "unable to create temporary directory"}
    # The newest task of 101 failed, an older one succeeded: the newest wins
    assert rows[101]["last_run"]["state"] == "failed"
    assert rows[101]["last_run"]["detail"] == "no space left on device"
    assert rows[9000]["last_run"]["state"] == "none"
    assert api.writes() == []


def test_tasks_are_asked_for_vzdump_only(cli, api):
    status_cluster(api)
    _doc(cli)
    calls = api.called("GET", "/nodes/{node}/tasks")
    assert {c.path for c in calls} == {"/nodes/pve1/tasks", "/nodes/pve2/tasks"}  # pve3 offline
    assert all(c.params["typefilter"] == "vzdump" for c in calls)


def test_failed_filter(cli, api):
    status_cluster(api)
    assert set(_doc(cli, "--failed")) == {101, 200}


def test_older_than_filter_includes_never_backed_up(cli, api):
    status_cluster(api)
    # 101 is 2d1h old, 200 3d, 9000 never; 100 is 1h old
    assert set(_doc(cli, "--older-than", "2d")) == {101, 200, 9000}


def test_both_filters_show_either_problem(cli, api):
    status_cluster(api)
    assert set(_doc(cli, "--older-than", "4d", "--failed")) == {101, 200, 9000}


def test_table_output(cli, api):
    status_cluster(api)
    r = cli("backup", "status", "-p", "p1")
    assert r.exit_code == 0, r.output
    row = next(line for line in r.stdout.splitlines() if " web2 " in line)
    assert "backup-daily" in row and "FAILED" in row and "no space left" in row
    row = next(line for line in r.stdout.splitlines() if " tpl " in line)
    assert "never" in row and "no job" in row


def test_tasks_hidden_without_sys_audit_are_not_checked_never_ok(cli, api):
    status_cluster(api)
    # Without Sys.Audit on a node, PVE silently lists only the user's own tasks
    api.route("GET", "/access/permissions",
              lambda c: {c.params["path"]: {"VM.Backup": 1}} if c.params["path"] == "/nodes/pve2"
              else {c.params["path"]: {"Sys.Audit": 1}})
    rows = _doc(cli)
    assert rows[101]["last_run"]["state"] == "not checked"
    assert "Sys.Audit" in rows[101]["last_run"]["detail"] and "/nodes/pve2" in rows[101]["last_run"]["detail"]
    assert rows[100]["last_run"]["state"] == "ok"
    # Not in --failed (unknown) but never reported as fine either
    r = cli("backup", "status", "-p", "p1")
    row = next(line for line in r.stdout.splitlines() if " web2 " in line)
    assert "not checked" in row and " OK " not in row


def test_jobs_unreadable_are_not_checked(cli, api):
    status_cluster(api)
    api.route("GET", "/cluster/backup", api.forbidden("/", "Sys.Audit"))
    api.route("GET", "/cluster/backup-info/not-backed-up", api.forbidden("/", "Sys.Audit"))
    r = cli("backup", "status", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    rows = {x["vmid"]: x for x in json.loads(r.stdout)}
    assert rows[100]["jobs"] is None
    assert "Sys.Audit" in rows[100]["jobs_detail"]
    assert "not checked" in r.stderr


def test_included_volumes_missing_falls_back_to_not_backed_up(cli, api):
    status_cluster(api)
    api.route("GET", "/cluster/backup/{id}/included_volumes", api.not_implemented())
    rows = _doc(cli)
    # Covered, but which job is unknown on this version
    assert rows[100]["jobs"] == ["(some job)"]
    assert rows[9000]["jobs"] == []


def test_unreadable_storage_does_not_claim_never(cli, api):
    status_cluster(api)
    api.route("GET", "/nodes/{node}/storage/pbs/content", api.error(500, "pbs: cannot connect"))
    r = cli("backup", "status", "-p", "p1", "--json")
    rows = {x["vmid"]: x for x in json.loads(r.stdout)}
    # 100 still has a local backup; the PBS could hold a newer one: said on stderr
    assert rows[100]["last_backup"] == NOW - DAY
    assert "pbs" in r.stderr and "cannot connect" in r.stderr
