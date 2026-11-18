"""Bulk guest actions: cluster guests start|shutdown|suspend|migrate and node evacuate."""

from __future__ import annotations

import pytest

from tests.fake_api import FakeAPI, upid

GUESTS = [
    {"vmid": 100, "type": "qemu", "node": "pve1", "status": "stopped", "name": "web", "tags": "x"},
    {"vmid": 101, "type": "qemu", "node": "pve2", "status": "stopped", "name": "db", "tags": "x"},
    {"vmid": 200, "type": "lxc", "node": "pve1", "status": "stopped", "name": "ct", "tags": "x"},
    {"vmid": 300, "type": "qemu", "node": "pve2", "status": "stopped", "name": "other"},
    {"vmid": 9000, "type": "qemu", "node": "pve1", "status": "stopped", "name": "tpl", "template": 1, "tags": "x"},
]

SCHEMA_ERR = "Parameter verification failed. (400)\n\n{key}: property is not defined in schema and the schema does not allow additional properties"


def status_route(api: FakeAPI, state: dict) -> None:
    """status/current answers from {vmid: dict}, keyed by the vmid of the path."""

    def answer(call):
        return state[int(call.match["vmid"])]

    api.route("GET", "/nodes/{node}/qemu/{vmid}/status/current", answer)
    api.route("GET", "/nodes/{node}/lxc/{vmid}/status/current", answer)


def setup(api: FakeAPI, bulk: bool, state: dict | None = None) -> None:
    api.route("GET", "/cluster/resources", GUESTS)
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"},
                                {"node": "pve3", "status": "online"}])
    api.route("GET", "/cluster/ha/resources", [])
    if bulk:
        api.route("POST", "/cluster/bulk-action/guest/{action}", lambda c: upid("pve1", "bulkaction"))
    else:
        api.route("POST", "/cluster/bulk-action/guest/{action}", api.not_implemented("POST", "/cluster/bulk-action/guest/start"))
    for action in ("startall", "stopall", "suspendall", "migrateall"):
        api.route("POST", f"/nodes/{{node}}/{action}", lambda c, a=action: upid(c.match["node"], a))
    api.route("POST", "/nodes/{node}/qemu/{vmid}/status/suspend", lambda c: upid(c.match["node"], "qmsuspend", c.match["vmid"]))
    status_route(api, state or {})


def running(*ids: int) -> dict:
    return {i: {"status": "running", "qmpstatus": "running"} for i in ids}


# ── selection ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["start", "shutdown", "suspend"])
def test_no_filter_is_refused(cli, api, action):
    setup(api, bulk=True)
    r = cli("cluster", "guests", action, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "--vms, --tag, --pool or --node" in r.output
    assert api.writes() == []


def test_migrate_without_filter_is_refused(cli, api):
    setup(api, bulk=True)
    r = cli("cluster", "guests", "migrate", "pve3", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


# ── one cluster task when bulk-action exists ─────────────────────────────


def test_bulk_action_is_one_task_with_the_ids(cli, api):
    setup(api, bulk=True, state=running(100, 101, 200))
    r = cli("cluster", "guests", "start", "--tag", "x", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    writes = api.writes()
    assert [w.path for w in writes] == ["/cluster/bulk-action/guest/start"]
    # template 9000 carries the tag but is never a target
    assert writes[0].data["vms"] == [100, 101, 200]
    assert "9000" not in r.stdout


def test_node_evacuate_uses_bulk_migrate_online(cli, api):
    setup(api, bulk=True, state={100: {"status": "running"}, 200: {"status": "stopped"}})
    r = cli("node", "evacuate", "pve1", "pve3", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    writes = api.writes()
    assert [w.path for w in writes] == ["/cluster/bulk-action/guest/migrate"]
    data = writes[0].data
    assert data["vms"] == [100, 200]
    assert data["target"] == "pve3"
    assert data["online"] == 1


def test_evacuate_to_unknown_node_writes_nothing(cli, api):
    setup(api, bulk=True)
    r = cli("node", "evacuate", "pve1", "nope", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "nope" in r.output
    assert api.writes() == []


# ── fallback on the per-node endpoints (bulk-action answers 501) ─────────


def test_fallback_startall_is_forced_per_node(cli, api):
    setup(api, bulk=False, state=running(100, 101, 200))
    r = cli("cluster", "guests", "start", "--tag", "x", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    calls = api.called("POST", "/nodes/{node}/startall")
    by_node = {c.match["node"]: c.data for c in calls}
    assert set(by_node) == {"pve1", "pve2"}
    # without force PVE only starts the onboot=1 guests
    assert by_node["pve1"]["force"] == 1 and by_node["pve2"]["force"] == 1
    assert by_node["pve1"]["vms"] == "100,200"
    assert by_node["pve2"]["vms"] == "101"


def test_fallback_migrateall_gets_one_worker_by_default(cli, api):
    setup(api, bulk=False, state={100: {"status": "running"}, 101: {"status": "running"}})
    r = cli("cluster", "guests", "migrate", "pve3", "--vms", "100,101", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    calls = api.called("POST", "/nodes/{node}/migrateall")
    assert len(calls) == 2
    for c in calls:
        assert c.data["max-workers"] == 1
        assert c.data["target"] == "pve3"


def test_fallback_retries_maxworkers_spelling(cli, api):
    setup(api, bulk=False, state={100: {"status": "running"}})

    def migrateall(call):
        if "max-workers" in call.data:
            return api.error(400, SCHEMA_ERR.format(key="max-workers"))
        return upid("pve1", "migrateall")

    api.route("POST", "/nodes/{node}/migrateall", migrateall)
    r = cli("cluster", "guests", "migrate", "pve3", "--vms", "100", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    datas = [c.data for c in api.called("POST", "/nodes/pve1/migrateall")]
    assert len(datas) == 2
    assert "max-workers" in datas[0]
    assert datas[1].get("maxworkers") == 1 and "max-workers" not in datas[1]


def test_fallback_runs_without_worker_count_and_warns(cli, api):
    setup(api, bulk=False, state={100: {"status": "running"}})

    def migrateall(call):
        for key in ("max-workers", "maxworkers"):
            if key in call.data:
                return api.error(400, SCHEMA_ERR.format(key=key))
        return upid("pve1", "migrateall")

    api.route("POST", "/nodes/{node}/migrateall", migrateall)
    r = cli("cluster", "guests", "migrate", "pve3", "--vms", "100", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    datas = [c.data for c in api.called("POST", "/nodes/pve1/migrateall")]
    assert len(datas) == 3
    assert "max-workers" not in datas[2] and "maxworkers" not in datas[2]
    assert datas[2]["target"] == "pve3" and datas[2]["vms"] == "100"
    assert "cannot limit" in r.output


def test_other_errors_are_not_retried(cli, api):
    setup(api, bulk=False)
    api.route("POST", "/nodes/{node}/migrateall", api.error(500, "no such target"))
    r = cli("cluster", "guests", "migrate", "pve3", "--vms", "100", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "no such target" in r.output
    assert len(api.called("POST", "/nodes/pve1/migrateall")) == 1


def test_fallback_suspend_in_memory_never_uses_suspendall(cli, api):
    setup(api, bulk=False, state={i: {"status": "running", "qmpstatus": "paused"} for i in (100, 101)})
    r = cli("cluster", "guests", "suspend", "--tag", "x", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("POST", "/nodes/{node}/suspendall") == []
    paths = sorted(w.path for w in api.writes() if w.path.startswith("/nodes"))
    assert paths == ["/nodes/pve1/qemu/100/status/suspend", "/nodes/pve2/qemu/101/status/suspend"]
    # containers cannot be suspended: skipped, never sent
    assert "200" in r.output
    assert not any("/lxc/" in w.path for w in api.writes())


def test_fallback_suspend_to_disk_uses_suspendall(cli, api):
    setup(api, bulk=False, state={i: {"status": "stopped", "lock": "suspended"} for i in (100, 101)})
    r = cli("cluster", "guests", "suspend", "--vms", "100,101", "--to-disk", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    calls = api.called("POST", "/nodes/{node}/suspendall")
    assert sorted(c.match["node"] for c in calls) == ["pve1", "pve2"]
    assert api.called("POST", "/nodes/{node}/qemu/{vmid}/status/suspend") == []


def test_bulk_suspend_passes_to_disk_only_when_asked(cli, api):
    setup(api, bulk=True, state={100: {"status": "running", "qmpstatus": "paused"}})
    r = cli("cluster", "guests", "suspend", "--vms", "100", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    (w,) = api.writes()
    assert w.path == "/cluster/bulk-action/guest/suspend"
    assert "to-disk" not in w.data


# ── task outcome ─────────────────────────────────────────────────────────


def test_task_ending_with_warnings_is_not_a_failure(cli, api):
    setup(api, bulk=True, state=running(100, 101))
    api.route("GET", "/nodes/{node}/tasks/{upid}/status", {"status": "stopped", "exitstatus": "WARNINGS: 1"})
    r = cli("cluster", "guests", "start", "--vms", "100,101", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "warnings" in r.output.lower()


def test_failed_task_is_an_error(cli, api):
    setup(api, bulk=True, state=running(100))
    api.route("GET", "/nodes/{node}/tasks/{upid}/status", {"status": "stopped", "exitstatus": "command failed"})
    r = cli("cluster", "guests", "start", "--vms", "100", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "command failed" in r.output


def test_ok_task_with_a_guest_left_behind_is_listed(cli, api):
    state = {100: {"status": "running"}, 101: {"status": "stopped", "lock": "backup"}}
    setup(api, bulk=True, state=state)
    r = cli("cluster", "guests", "start", "--vms", "100,101", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "1/2" in r.output
    assert "101" in r.output and "backup" in r.output
