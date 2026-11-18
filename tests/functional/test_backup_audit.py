"""pvecli audit: the backup freshness check (too old, or last run failed)."""

from __future__ import annotations

from tests.fake_api import FakeAPI
from tests.functional._backup_cluster import DAY, NOW, log
from tests.functional.test_audit import lab, lines, run_json

U_LAB1 = f"UPID:pve-lab1:0000AAAA:0000BBBB:{NOW - 7200:08X}:vzdump::root@pam:"
CHECK = "Backup freshness"


def backups(api: FakeAPI, ages: dict[int, int], failed: dict[int, str] | None = None,
            job_vmids: str = "300,301") -> None:
    """lab1 with one enabled job on 300 and 301, backups of the given ages
    (seconds) on local, and one vzdump run on pve-lab1."""
    guests = lab(api)
    failed = failed or {}
    included = {"children": [{"id": int(v), "type": "qemu", "children": []} for v in job_vmids.split(",")]}
    api.route("GET", "/cluster/backup", [{"id": "nightly", "type": "vzdump", "enabled": 1,
                                         "schedule": "02:00", "vmid": job_vmids, "storage": "local"}])
    api.route("GET", "/cluster/backup/nightly/included_volumes", included)
    api.route("GET", "/access/permissions", lambda c: {c.params["path"]: {"Sys.Audit": 1}})
    by_node = {g["vmid"]: g["node"] for g in guests}

    def content(call):
        node, storage = call.match["node"], call.match["storage"]
        if storage != "local" or (call.params or {}).get("content") != "backup":
            return []
        return [
            {"volid": f"local:backup/vzdump-qemu-{v}-{NOW - age}.vma.zst", "vmid": v, "ctime": NOW - age,
             "content": "backup", "subtype": "qemu", "format": "vma.zst", "size": 1}
            for v, age in ages.items() if by_node.get(v) == node
        ]

    api.route("GET", "/nodes/{node}/storage/{storage}/content", content)
    ran = [v for v in (300, 301) if by_node[v] == "pve-lab1"]
    lines_ = []
    for v in ran:
        lines_.append(f"INFO: Starting Backup of VM {v} (qemu)")
        lines_.append(f"ERROR: Backup of VM {v} failed - {failed[v]}" if v in failed
                      else f"INFO: Finished Backup of VM {v} (00:00:01)")
    api.route("GET", "/nodes/{node}/tasks", lambda c: [
        {"upid": U_LAB1, "id": "", "type": "vzdump", "status": "job errors" if failed else "OK",
         "starttime": NOW - 7200, "endtime": NOW - 7100}
    ] if c.match["node"] == "pve-lab1" else [])
    api.route("GET", "/nodes/{node}/tasks/{upid}/log", log(*lines_))


def test_fresh_backups_and_successful_runs_are_ok(cli, api):
    backups(api, {300: 3600, 301: 7200})
    r, doc = run_json(cli)
    found = lines(doc, CHECK)
    assert [f["level"] for f in found] == ["OK"], found
    assert api.writes() == []


def test_failed_last_run_fails_with_exit_2(cli, api):
    backups(api, {300: 3600, 301: 3 * DAY}, failed={301: "no space left on device"})
    r, doc = run_json(cli)
    assert r.exit_code == 2
    fails = [f for f in lines(doc, CHECK) if f["level"] == "FAIL"]
    assert len(fails) == 1 and "301" in fails[0]["detail"] and "no space left" in fails[0]["detail"]


def test_too_old_backup_warns(cli, api):
    backups(api, {300: 3 * DAY, 301: 3600})
    r, doc = run_json(cli)
    assert r.exit_code == 0
    warns = [f for f in lines(doc, CHECK) if f["level"] == "WARN"]
    assert len(warns) == 1 and "300" in warns[0]["detail"] and "2d" in warns[0]["detail"]
    assert "OK" not in {f["level"] for f in lines(doc, CHECK)}


def test_guest_in_a_job_never_backed_up_warns(cli, api):
    backups(api, {301: 3600})
    r, doc = run_json(cli)
    warns = [f for f in lines(doc, CHECK) if f["level"] == "WARN"]
    assert warns and "300" in warns[0]["detail"] and "never" in warns[0]["detail"]


def test_backup_age_option(cli, api):
    backups(api, {300: 3 * DAY, 301: 3600})
    r, doc = run_json(cli, "--backup-age", "5d")
    assert [f["level"] for f in lines(doc, CHECK)] == ["OK"]


def test_guests_outside_any_job_are_left_to_the_backups_check(cli, api):
    # 311 is in no job and never backed up: not this check's business
    backups(api, {300: 3600, 301: 3600})
    r, doc = run_json(cli)
    assert all("311" not in f["detail"] for f in lines(doc, CHECK))


def test_tasks_hidden_without_sys_audit_are_not_checked(cli, api):
    backups(api, {300: 3600, 301: 3600})
    api.route("GET", "/access/permissions", lambda c: {c.params["path"]: {"VM.Audit": 1}})
    r, doc = run_json(cli)
    found = lines(doc, CHECK)
    assert "OK" not in {f["level"] for f in found}
    assert any("not checked" in f["detail"] and "Sys.Audit" in f["detail"] for f in found)


def test_jobs_unreadable_are_not_checked(cli, api):
    backups(api, {300: 3600, 301: 3600})
    # Both need Sys.Audit on /: they fail together
    api.route("GET", "/cluster/backup", api.forbidden("/", "Sys.Audit"))
    api.route("GET", "/cluster/backup-info/not-backed-up", api.forbidden("/", "Sys.Audit"))
    r, doc = run_json(cli)
    found = lines(doc, CHECK)
    assert [f["level"] for f in found] == ["INFO"]
    assert "not checked" in found[0]["detail"] and "Sys.Audit" in found[0]["detail"]


def test_backup_storage_unreadable_is_not_checked(cli, api):
    backups(api, {300: 3600, 301: 3600})
    api.route("GET", "/nodes/{node}/storage/local/content",
              lambda c: api.forbidden("/storage/local", "Datastore.Audit"))
    r, doc = run_json(cli)
    found = lines(doc, CHECK)
    assert "OK" not in {f["level"] for f in found}
    assert any("not checked" in f["detail"] and "local" in f["detail"] for f in found)
