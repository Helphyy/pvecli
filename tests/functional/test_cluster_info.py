"""cluster status, resources, tasks and preflight against the fake API."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.fake_api import Call, FakeAPI, fixture

NOW = 1_800_000_000  # FakeClock start


# ── cluster status ───────────────────────────────────────────────────────

def test_status_shows_cluster_and_nodes(cli, api):
    status = fixture("pve-9.1/cluster_status")
    status[1]["online"] = 0  # pve-lab1 down
    api.route("GET", "/cluster/status", status)
    r = cli("cluster", "status", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "sdnlab" in r.output and "Yes" in r.output
    line = next(x for x in r.output.splitlines() if "pve-lab1" in x)
    assert "offline" in line
    assert "online" in next(x for x in r.output.splitlines() if "pve-lab2" in x)
    assert api.writes() == []


def test_status_not_quorate(cli, api):
    api.route("GET", "/cluster/status", [{"type": "cluster", "name": "c", "quorate": 0, "nodes": 2}])
    r = cli("cluster", "status", "-p", "p1")
    assert r.exit_code == 0
    assert "No" in next(x for x in r.output.splitlines() if " c " in x)


def test_status_empty(cli, api):
    api.route("GET", "/cluster/status", [])
    r = cli("cluster", "status", "-p", "p1")
    assert r.exit_code == 0
    assert "No cluster information" in r.output


def test_status_api_error_exits_1(cli, api):
    api.route("GET", "/cluster/status", api.error(500, "cluster not ready"))
    r = cli("cluster", "status", "-p", "p1")
    assert r.exit_code == 1
    assert "cluster not ready" in r.output


# ── cluster resources ────────────────────────────────────────────────────

RESOURCES = [
    {"type": "node", "node": "pve1", "status": "online", "cpu": 0.1, "maxcpu": 8,
     "mem": 2 * 1024**3, "maxmem": 16 * 1024**3, "uptime": 3600, "id": "node/pve1"},
    {"type": "qemu", "vmid": 100, "name": "web", "node": "pve1", "status": "running",
     "cpu": 0.5, "maxcpu": 2, "mem": 1024**3, "maxmem": 2 * 1024**3, "id": "qemu/100"},
    {"type": "lxc", "vmid": 101, "name": "dns", "node": "pve1", "status": "stopped", "id": "lxc/101"},
    {"type": "storage", "storage": "local", "node": "pve1", "status": "available",
     "disk": 10 * 1024**3, "maxdisk": 100 * 1024**3, "id": "storage/pve1/local"},
    {"type": "storage", "storage": "nfs", "node": "pve1", "status": "unknown",
     "disk": 0, "maxdisk": 0, "id": "storage/pve1/nfs"},
    {"type": "sdn", "sdn": "localnetwork", "node": "pve1", "status": "ok", "id": "sdn/pve1/localnetwork"},
]


def test_resources_grouped_by_type(cli, api):
    api.route("GET", "/cluster/resources", RESOURCES)
    r = cli("cluster", "resources", "-p", "p1")
    assert r.exit_code == 0, r.output
    for title in ("Resources: node", "Resources: qemu", "Resources: lxc", "Resources: storage",
                  "Resources: sdn"):
        assert title in r.output
    assert "web" in r.output and "dns" in r.output and "sdn/pve1/localnetwork" in r.output
    (call,) = api.called("GET", "/cluster/resources")
    assert call.params is None


def test_resources_type_filter_is_sent_to_the_api(cli, api):
    api.route("GET", "/cluster/resources", [r for r in RESOURCES if r["type"] == "storage"])
    r = cli("cluster", "resources", "--type", "storage", "-p", "p1")
    assert r.exit_code == 0, r.output
    (call,) = api.called("GET", "/cluster/resources")
    assert call.params == {"type": "storage"}
    assert "Resources: storage" in r.output
    assert "available" in r.output and "unknown" in r.output


def test_resources_json_envelope(cli, api):
    api.route("GET", "/cluster/resources", [])
    r = cli("cluster", "resources", "--type", "vm", "--json", "-p", "p1")
    assert r.exit_code == 0
    assert json.loads(r.stdout) == {"profile": "p1", "type": "vm", "resources": []}


def test_resources_empty_text(cli, api):
    api.route("GET", "/cluster/resources", [])
    r = cli("cluster", "resources", "-p", "p1")
    assert r.exit_code == 0
    assert "No resources found" in r.output


# ── cluster tasks ────────────────────────────────────────────────────────

def task(node: str, start: int, kind: str = "qmstart", status: str | None = "OK") -> dict[str, Any]:
    t = {"upid": f"UPID:{node}:{start}", "node": node, "type": kind, "id": "100",
         "user": "root@pam", "starttime": start}
    if status is not None:
        t["status"] = status
    return t


def tasks_setup(api: FakeAPI) -> None:
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"}])
    per_node = {
        "pve1": [task("pve1", NOW - 10, "vzdump"), task("pve1", NOW - 300, status="command failed")],
        "pve2": [task("pve2", NOW - 20, "qmigrate"), task("pve2", NOW - 200)],
    }
    api.route("GET", "/nodes/{node}/tasks", lambda c: per_node[c.match["node"]])


def test_tasks_merge_every_node_most_recent_first(cli, api):
    tasks_setup(api)
    r = cli("cluster", "tasks", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Cluster Tasks" in r.output
    out = r.output
    assert out.index("vzdump") < out.index("qmigrate") < out.index("command failed")
    calls = api.called("GET", "/nodes/{node}/tasks")
    assert sorted(c.path for c in calls) == ["/nodes/pve1/tasks", "/nodes/pve2/tasks"]
    assert all(c.params == {"limit": 50} for c in calls)


def test_tasks_limit_keeps_the_most_recent_across_nodes(cli, api):
    tasks_setup(api)
    r = cli("cluster", "tasks", "--limit", "2", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "vzdump" in r.output and "qmigrate" in r.output
    assert "command failed" not in r.output
    assert all(c.params == {"limit": 2} for c in api.called("GET", "/nodes/{node}/tasks"))


def test_tasks_running_asks_for_active_tasks(cli, api):
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}])
    api.route("GET", "/nodes/{node}/tasks", [task("pve1", NOW - 5, "vzdump", status=None)])
    r = cli("cluster", "tasks", "--running", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Running Tasks" in r.output and "vzdump" in r.output
    (call,) = api.called("GET", "/nodes/{node}/tasks")
    assert call.params == {"limit": 50, "source": "active"}


def test_tasks_none(cli, api):
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}])
    api.route("GET", "/nodes/{node}/tasks", [])
    r = cli("cluster", "tasks", "-p", "p1")
    assert r.exit_code == 0
    assert "No tasks found" in r.output


# ── cluster preflight ────────────────────────────────────────────────────

SERVICES = [{"name": s, "state": "running"} for s in ("pve-cluster", "pvedaemon", "pveproxy", "pvestatd")]


def preflight_setup(
    api: FakeAPI,
    clock_skew: dict[str, int] | None = None,
    versions: dict[str, tuple[str, str]] | None = None,
    hosts: dict[str, str] | None = None,
    dns: dict[str, dict[str, Any]] | None = None,
    nodes: tuple[str, ...] = ("pve1", "pve2", "pve3"),
    clustered: bool = True,
) -> None:
    skew = clock_skew or {}
    ver = versions or {}
    every = "".join(f"10.0.0.{i} {n}.lab.local {n}\n" for i, n in enumerate(nodes, 1))
    api.route("GET", "/nodes", [{"node": n, "status": "online"} for n in nodes])
    api.route("GET", "/cluster/config/totem", {"cluster_name": "lab"} if clustered else None)
    # UTC epoch in "time", local time in "localtime" (recorded shape)
    api.route("GET", "/nodes/{node}/time", lambda c: {
        "time": NOW + skew.get(c.match["node"], 0),
        "localtime": NOW + 3600 + skew.get(c.match["node"], 0), "timezone": "Europe/Paris"})
    api.route("GET", "/nodes/{node}/version", lambda c: dict(zip(
        ("version", "release"), ver.get(c.match["node"], ("9.1.1", "9.1")), strict=True)))
    api.route("GET", "/nodes/{node}/dns", lambda c: (dns or {}).get(c.match["node"], {"search": "lab.local"}))
    api.route("GET", "/nodes/{node}/hosts", lambda c: {
        "data": "127.0.0.1 localhost\n" + (hosts or {}).get(c.match["node"], every)})
    api.route("GET", "/nodes/{node}/services", SERVICES)
    api.route("GET", "/cluster/status", [{"type": "cluster", "name": "lab", "quorate": 1}])
    api.route("GET", "/cluster/config/join", {"nodelist": [
        {"name": n, "ring0_addr": f"10.0.0.{i}", "ring1_addr": f"10.1.0.{i}"} for i, n in enumerate(nodes, 1)]})
    api.route("GET", "/cluster/config/qdevice", {})
    api.route("GET", "/cluster/resources", [])


def preflight(cli: Any, *extra: str) -> tuple[int, dict[str, Any]]:
    r = cli("cluster", "preflight", "--json", "-p", "p1", *extra)
    doc = json.loads(r.stdout)
    return r.exit_code, doc


def check(doc: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in doc["checks"] if c["check"] == name)


def test_preflight_healthy_cluster(cli, api):
    preflight_setup(api)
    code, doc = preflight(cli)
    assert code == 0
    assert doc["verdict"] == "ok" and doc["cluster"] == "lab"
    assert {c["check"]: c["verdict"] for c in doc["checks"]} == {
        "Nodes online": "ok", "PVE versions": "ok", "Clock drift": "ok", "Name resolution": "ok",
        "Core services": "ok", "Quorum": "ok", "Corosync links": "ok", "Quorum resilience": "ok"}
    assert api.writes() == []


def test_preflight_timezone_is_not_drift(cli, api):
    # Same UTC clock, local times one hour apart: no drift at all
    preflight_setup(api)
    api.route("GET", "/nodes/{node}/time", lambda c: {
        "time": NOW, "localtime": NOW + (7200 if c.match["node"] == "pve2" else 0)})
    _, doc = preflight(cli)
    assert check(doc, "Clock drift")["verdict"] == "ok"


@pytest.mark.parametrize(("skew", "verdict"), [(2, "ok"), (3, "ok"), (10, "warn"), (31, "warn"), (60, "fail")])
def test_preflight_clock_drift_from_offsets(cli, api, skew, verdict):
    # Drift beyond the 1 s API resolution, against --max-drift 2 then 30 s
    preflight_setup(api, clock_skew={"pve2": skew})
    code, doc = preflight(cli)
    assert check(doc, "Clock drift")["verdict"] == verdict
    assert code == (1 if verdict == "fail" else 0)


def test_preflight_max_drift_option(cli, api):
    preflight_setup(api, clock_skew={"pve3": 10})
    _, doc = preflight(cli, "--max-drift", "15")
    assert check(doc, "Clock drift")["verdict"] == "ok"


def test_preflight_different_release_fails(cli, api):
    preflight_setup(api, versions={"pve3": ("9.2.28", "9.2")})
    code, doc = preflight(cli)
    assert code == 1 and doc["verdict"] == "fail"
    c = check(doc, "PVE versions")
    assert c["verdict"] == "fail" and "pve3=9.2.28" in c["detail"]


def test_preflight_different_patch_warns(cli, api):
    preflight_setup(api, versions={"pve3": ("9.1.2", "9.1")})
    code, doc = preflight(cli)
    assert code == 0 and doc["verdict"] == "warn"
    assert check(doc, "PVE versions")["verdict"] == "warn"


def test_preflight_name_resolution_by_short_name_in_hosts(cli, api):
    # Only the short form listed: still resolves
    hosts = {n: "".join(f"10.0.0.{i} {m}\n" for i, m in enumerate(("pve1", "pve2", "pve3"), 1))
             for n in ("pve1", "pve2", "pve3")}
    preflight_setup(api, hosts=hosts)
    _, doc = preflight(cli)
    assert check(doc, "Name resolution")["verdict"] == "ok"


def test_preflight_missing_hosts_entry_without_dns_warns(cli, api):
    preflight_setup(api, hosts={"pve1": "10.0.0.1 pve1.lab.local pve1\n10.0.0.2 pve2\n"})
    code, doc = preflight(cli)
    c = check(doc, "Name resolution")
    assert c["verdict"] == "warn" and "pve1->pve3" in c["detail"]
    assert code == 0


def test_preflight_missing_hosts_entry_with_dns_is_not_verified(cli, api):
    # A commented out entry does not count; DNS may answer but cannot be checked
    preflight_setup(api, hosts={"pve1": "10.0.0.2 pve2\n# 10.0.0.3 pve3\n"},
                    dns={"pve1": {"dns1": "10.0.0.254", "search": "lab.local"}})
    _, doc = preflight(cli)
    c = check(doc, "Name resolution")
    assert c["verdict"] == "skip" and "pve1->pve3" in c["detail"]


def test_preflight_hosts_name_prefix_does_not_count(cli, api):
    # "pve10" must not be taken for "pve1"
    preflight_setup(api, hosts={"pve2": "10.0.0.10 pve10\n10.0.0.3 pve3\n"})
    _, doc = preflight(cli)
    assert "pve2->pve1" in check(doc, "Name resolution")["detail"]


def test_preflight_stopped_service_and_offline_node_fail(cli, api):
    preflight_setup(api)
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"},
                                {"node": "pve3", "status": "offline"}])
    api.route("GET", "/nodes/{node}/services", lambda c: [
        {**s, "state": "dead"} if c.match["node"] == "pve2" and s["name"] == "pveproxy" else s
        for s in SERVICES])
    code, doc = preflight(cli)
    assert code == 1
    assert check(doc, "Nodes online")["verdict"] == "fail"
    assert "pve2:pveproxy=dead" in check(doc, "Core services")["detail"]


def test_preflight_single_link_two_nodes_no_quorum(cli, api):
    preflight_setup(api, nodes=("pve1", "pve2"))
    api.route("GET", "/cluster/config/join", {"nodelist": [{"name": "pve1", "ring0_addr": "a"},
                                                            {"name": "pve2", "ring0_addr": "b"}]})
    api.route("GET", "/cluster/status", [{"type": "cluster", "name": "lab", "quorate": 0}])
    code, doc = preflight(cli)
    assert code == 1
    assert check(doc, "Quorum")["verdict"] == "fail"
    assert check(doc, "Corosync links")["verdict"] == "warn"
    assert check(doc, "Quorum resilience")["verdict"] == "warn"


def test_preflight_standalone_with_guests(cli, api):
    preflight_setup(api, nodes=("pve9",), clustered=False)
    api.route("GET", "/cluster/resources", [{"type": "qemu", "vmid": 100}])
    code, doc = preflight(cli)
    assert code == 0 and doc["cluster"] is None
    assert check(doc, "Cluster")["verdict"] == "skip"
    assert check(doc, "Guests present")["verdict"] == "warn"
    assert check(doc, "Clock drift")["verdict"] == "skip"


def test_preflight_text_output(cli, api):
    preflight_setup(api, versions={"pve3": ("9.1.2", "9.1")})
    r = cli("cluster", "preflight", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Preflight: cluster 'lab'" in r.output
    assert "deserve a look" in r.output


def test_preflight_text_fail_exits_1(cli, api):
    preflight_setup(api, clock_skew={"pve1": 120})
    r = cli("cluster", "preflight", "-p", "p1")
    assert r.exit_code == 1
    assert "Blocking issues" in r.output


def test_preflight_unreadable_node_facts_do_not_crash(cli, api):
    preflight_setup(api)

    def services(call: Call) -> Any:
        if call.match["node"] == "pve3":
            return api.forbidden("/nodes/pve3", "Sys.Audit")
        return SERVICES

    api.route("GET", "/nodes/{node}/services", services)
    code, doc = preflight(cli)
    assert code == 1
    assert "pve3 (unreadable)" in check(doc, "Core services")["detail"]
