"""pvecli ceph: read-only commands against recorded and realistic PVE answers.

Shapes follow what PVE really returns:
- /cluster/ceph/status is `ceph -s` as JSON (fixture recorded on a 9.1 lab
  with Ceph, and the 9.2 lab without Ceph answering 500 "binary not
  installed");
- /nodes/{node}/ceph/osd is {"root": {"children": [root -> host -> osd]}};
- /nodes/{node}/ceph/log paginates from the FIRST line: start=0 is the
  oldest line, so the tail needs the offset of the end.
"""

from __future__ import annotations

import json
from typing import Any

from tests.fake_api import FakeAPI, fixture

NODES = [
    {"node": "pve2", "status": "online", "type": "node"},
    {"node": "pve1", "status": "online", "type": "node"},
    {"node": "pve0", "status": "offline", "type": "node"},
]


def _load(name: str) -> Any:
    data = fixture(name)
    if isinstance(data, dict) and "__error__" in data:
        return FakeAPI.error(data["__status__"], data["__error__"])
    return data


def _no_ceph() -> Any:
    # What the 9.2 lab without Ceph answers on every Ceph endpoint
    return _load("pve-9.2/ceph_status")


def _status_with(api: FakeAPI, **changes: Any) -> dict:
    status = fixture("pve-9.1/ceph_status")
    status.update(changes)
    api.route("GET", "/cluster/ceph/status", status)
    return status


# ── status ──────────────────────────────────────────────────────────────


def test_status_recorded_health_ok(cli, api):
    api.route("GET", "/cluster/ceph/status", fixture("pve-9.1/ceph_status"))
    api.route("GET", "/cluster/ceph/flags", fixture("pve-9.1/ceph_flags"))
    r = cli("ceph", "status", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "HEALTH_OK" in r.stdout
    # 3 monitors on the lab; 5 OSD slots, 4 up and 4 in
    assert "Monitors" in r.stdout and " 3 " in r.stdout
    assert "4/5 up, 4/5 in" in r.stdout
    # Health is OK although the counts differ: a destroyed OSD not purged
    assert "empty osdmap slot" in r.stdout
    assert "Placement groups" in r.stdout and "97" in r.stdout
    # No flag set on the lab
    assert "none set" in r.stdout
    assert "Warning" not in r.output
    assert api.writes() == []


def test_status_json_recorded(cli, api):
    api.route("GET", "/cluster/ceph/status", fixture("pve-9.1/ceph_status"))
    api.route("GET", "/cluster/ceph/flags", fixture("pve-9.1/ceph_flags"))
    r = cli("ceph", "status", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc == {
        "health": "HEALTH_OK",
        "checks": [],
        "monitors": 3,
        "osds": {"total": 5, "up": 4, "in": 4},
        "capacity": {"total": 51522830336, "used": 177045504, "avail": 51345784832},
        "active_flags": [],
    }


def test_status_warn_lists_checks_and_flags(cli, api):
    _status_with(api, health={"status": "HEALTH_WARN", "mutes": [], "checks": {
        "OSDMAP_FLAGS": {"severity": "HEALTH_WARN", "summary": {"message": "noout flag(s) set", "count": 1}},
    }})
    flags = fixture("pve-9.1/ceph_flags")
    for f in flags:
        if f["name"] == "noout":
            f["value"] = 1
    api.route("GET", "/cluster/ceph/flags", flags)
    r = cli("ceph", "status", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "HEALTH_WARN" in r.stdout
    assert "OSDMAP_FLAGS" in r.stdout and "noout flag(s) set" in r.stdout
    assert "forgotten noout" in r.output

    r = cli("ceph", "status", "-p", "p1", "--json")
    doc = json.loads(r.stdout)
    assert doc["health"] == "HEALTH_WARN"
    assert doc["checks"] == ["OSDMAP_FLAGS"]
    assert doc["active_flags"] == ["noout"]


def test_status_osd_down_with_health_warn_is_not_explained_away(cli, api):
    status = fixture("pve-9.1/ceph_status")
    status["health"] = {"status": "HEALTH_WARN", "checks": {}, "mutes": []}
    status["osdmap"].update(num_osds=4, num_up_osds=3, num_in_osds=4)
    api.route("GET", "/cluster/ceph/status", status)
    api.route("GET", "/cluster/ceph/flags", [])
    r = cli("ceph", "status", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "3/4 up, 4/4 in" in r.stdout
    assert "empty osdmap slot" not in r.stdout


def test_status_unreadable_flags_still_shows_status(cli, api):
    api.route("GET", "/cluster/ceph/status", fixture("pve-9.1/ceph_status"))
    api.route("GET", "/cluster/ceph/flags", api.forbidden("/", "Sys.Audit"))
    r = cli("ceph", "status", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "HEALTH_OK" in r.stdout


def test_status_without_ceph_is_a_clear_message_and_success(cli, api):
    api.route("GET", "/cluster/ceph/status", _no_ceph())
    r = cli("ceph", "status", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "No Ceph on this cluster" in r.output
    assert "Error" not in r.output


def test_status_json_without_ceph_keeps_stdout_parseable(cli, api):
    api.route("GET", "/cluster/ceph/status", _no_ceph())
    r = cli("ceph", "status", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    if r.stdout.strip():
        json.loads(r.stdout)


def test_status_ceph_unreachable_is_an_error(cli, api):
    # Ceph installed but monitors out of quorum: PVE times out on the RADOS call
    api.route("GET", "/cluster/ceph/status", api.error(500, "got timeout"))
    r = cli("ceph", "status", "-p", "p1")
    assert r.exit_code == 1
    assert "got timeout" in r.stderr
    assert "No Ceph" not in r.output


# ── osd ─────────────────────────────────────────────────────────────────


def _osd(i: int, status: str = "up", inn: int = 1, cls: str = "ssd", pct: float = 12.5) -> dict:
    return {"id": i, "name": f"osd.{i}", "type": "osd", "status": status, "in": inn,
            "device_class": cls, "percent_used": pct, "total_space": 10_000_000_000,
            "bytes_used": int(10_000_000_000 * pct / 100), "leaf": 1}


OSD_TREE = {
    "flags": "sortbitwise,recovery_deletes,purged_snapdirs,pglog_hardlimit",
    "root": {"leaf": 0, "children": [{
        "id": -1, "name": "default", "type": "root", "leaf": 0, "children": [
            {"id": -3, "name": "pve1", "type": "host", "leaf": 0, "children": [_osd(1), _osd(0)]},
            {"id": -5, "name": "pve2", "type": "host", "leaf": 0,
             "children": [_osd(2, status="down", inn=0, cls="hdd")]},
        ]}]},
}


def test_osd_tree_is_flattened_with_host(cli, api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/ceph/osd", OSD_TREE)
    r = cli("ceph", "osd", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    # Asked to the first ONLINE node in name order (pve0 is offline)
    assert [c.path for c in api.called("GET", "/nodes/{node}/ceph/osd")] == ["/nodes/pve1/ceph/osd"]
    doc = json.loads(r.stdout)
    assert {(o["id"], o["host"]) for o in doc} == {(0, "pve1"), (1, "pve1"), (2, "pve2")}


def test_osd_table(cli, api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/ceph/osd", OSD_TREE)
    r = cli("ceph", "osd", "-p", "p1")
    assert r.exit_code == 0, r.output
    lines = [line for line in r.stdout.splitlines() if "osd." in line]
    assert [line.split()[1] for line in lines] == ["osd.0", "osd.1", "osd.2"]
    assert "pve2" in lines[2] and "down" in lines[2] and "out" in lines[2] and "hdd" in lines[2]
    assert "up" in lines[0] and " in " in lines[0]


def test_osd_without_ceph(cli, api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/ceph/osd", _no_ceph())
    r = cli("ceph", "osd", "-p", "p1")
    assert r.exit_code == 0
    assert "No Ceph on this cluster" in r.output


def test_osd_no_online_node_is_an_error(cli, api):
    api.route("GET", "/nodes", [{"node": "pve1", "status": "offline"}])
    r = cli("ceph", "osd", "-p", "p1")
    assert r.exit_code == 1
    assert "No online node" in r.stderr
    assert not api.called("GET", "/nodes/{node}/ceph/osd")


def test_osd_unreachable_is_an_error(cli, api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/ceph/osd", api.error(500, "rados_connect failed - Connection timed out"))
    r = cli("ceph", "osd", "-p", "p1")
    assert r.exit_code == 1
    assert "rados_connect failed" in r.stderr


def test_osd_empty_tree(cli, api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/ceph/osd", {"root": {"leaf": 0, "children": []}, "flags": ""})
    r = cli("ceph", "osd", "-p", "p1")
    assert r.exit_code == 0
    assert "No OSD reported" in r.output


# ── pool ────────────────────────────────────────────────────────────────


POOLS = [
    {"pool": 1, "pool_name": ".mgr", "size": 3, "min_size": 2, "pg_num": 1, "crush_rule": 0,
     "crush_rule_name": "replicated_rule", "bytes_used": 1_200_000, "type": "replicated"},
    {"pool": 2, "pool_name": "vm-ssd", "size": 3, "min_size": 2, "pg_num": 32, "crush_rule": 1,
     "crush_rule_name": "ssd-only", "bytes_used": 50_000_000_000, "type": "replicated"},
    {"pool": 3, "pool_name": "scratch", "size": 2, "min_size": 1, "pg_num": 32, "crush_rule": 0,
     "crush_rule_name": "replicated_rule", "bytes_used": 0, "type": "replicated"},
]


def test_pool_warns_on_min_size_below_two_only(cli, api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/ceph/pool", POOLS)
    r = cli("ceph", "pool", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "ssd-only" in r.stdout and "vm-ssd" in r.stdout
    warning = [line for line in r.output.splitlines() if "min_size below 2" in line]
    assert len(warning) == 1
    assert "scratch" in warning[0]
    assert "vm-ssd" not in warning[0] and ".mgr" not in warning[0]


def test_pool_safe_pools_no_warning(cli, api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/ceph/pool", POOLS[:2])
    r = cli("ceph", "pool", "-p", "p1")
    assert r.exit_code == 0
    assert "min_size below 2" not in r.output


def test_pool_json(cli, api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/ceph/pool", POOLS)
    r = cli("ceph", "pool", "-p", "p1", "--json")
    assert r.exit_code == 0
    assert {p["pool_name"] for p in json.loads(r.stdout)} == {".mgr", "vm-ssd", "scratch"}


def test_pool_none_defined_and_no_ceph(cli, api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/ceph/pool", [])
    r = cli("ceph", "pool", "-p", "p1")
    assert r.exit_code == 0 and "No Ceph pool defined" in r.output
    api.route("GET", "/nodes/{node}/ceph/pool", _no_ceph())
    r = cli("ceph", "pool", "-p", "p1")
    assert r.exit_code == 0 and "No Ceph on this cluster" in r.output


def test_pool_unreachable_is_an_error(cli, api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/ceph/pool", api.error(500, "got timeout"))
    r = cli("ceph", "pool", "-p", "p1")
    assert r.exit_code == 1 and "got timeout" in r.stderr


# ── flags ───────────────────────────────────────────────────────────────


def test_flags_recorded_none_set(cli, api):
    api.route("GET", "/cluster/ceph/flags", fixture("pve-9.1/ceph_flags"))
    r = cli("ceph", "flags", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "noout" in r.stdout and "nobackfill" in r.stdout
    assert "Set right now" not in r.output


def test_flags_set_warns_with_their_names(cli, api):
    flags = fixture("pve-9.1/ceph_flags")
    for f in flags:
        if f["name"] in ("noout", "norebalance"):
            f["value"] = 1
    api.route("GET", "/cluster/ceph/flags", flags)
    r = cli("ceph", "flags", "-p", "p1")
    assert r.exit_code == 0
    warning = next(line for line in r.output.splitlines() if "Set right now" in line)
    assert "noout" in warning and "norebalance" in warning and "nobackfill" not in warning

    r = cli("ceph", "flags", "-p", "p1", "--json")
    doc = json.loads(r.stdout)
    assert {f["name"] for f in doc if f["value"]} == {"noout", "norebalance"}


def test_flags_no_ceph_and_unreachable(cli, api):
    api.route("GET", "/cluster/ceph/flags", _load("pve-9.2/ceph_flags"))
    r = cli("ceph", "flags", "-p", "p1")
    assert r.exit_code == 0 and "No Ceph on this cluster" in r.output
    api.route("GET", "/cluster/ceph/flags", api.error(500, "got timeout"))
    r = cli("ceph", "flags", "-p", "p1")
    assert r.exit_code == 1 and "got timeout" in r.stderr


# ── versions ────────────────────────────────────────────────────────────


def _meta(osd_version: str = "19.2.3") -> dict:
    full = "ceph version {v} (deadbeef) squid (stable)"
    return {
        "node": {
            "pve1": {"buildcommit": "deadbeef", "version": {"parts": [19, 2, 3], "str": "19.2.3"}},
            "pve2": {"buildcommit": "deadbeef", "version": {"parts": [19, 2, 3], "str": "19.2.3"}},
        },
        "mon": {"pve1@pve1": {"ceph_version": full.format(v="19.2.3"), "ceph_version_short": "19.2.3",
                              "hostname": "pve1", "name": "pve1"}},
        "osd": {"0": {"ceph_version": full.format(v=osd_version), "ceph_version_short": osd_version,
                      "hostname": "pve2", "id": 0}},
    }


def test_versions_all_equal_no_warning(cli, api):
    api.route("GET", "/cluster/ceph/metadata", _meta())
    r = cli("ceph", "versions", "-p", "p1")
    assert r.exit_code == 0, r.output
    # The whole metadata is asked for: a scope would hide some daemon types
    assert api.called("GET", "/cluster/ceph/metadata")[0].params is None
    assert r.stdout.count("19.2.3") == 4
    assert "Several Ceph versions" not in r.output


def test_versions_straggler_is_reported(cli, api):
    api.route("GET", "/cluster/ceph/metadata", _meta(osd_version="19.2.2"))
    r = cli("ceph", "versions", "-p", "p1")
    assert r.exit_code == 0
    osd_line = next(line for line in r.stdout.splitlines() if line.split()[1:2] == ["osd"] or " osd " in line)
    assert "19.2.2" in osd_line
    assert "Several Ceph versions" in r.output


def test_versions_json(cli, api):
    api.route("GET", "/cluster/ceph/metadata", _meta())
    r = cli("ceph", "versions", "-p", "p1", "--json")
    assert r.exit_code == 0
    assert json.loads(r.stdout) == _meta()


def test_versions_without_ceph(cli, api):
    api.route("GET", "/cluster/ceph/metadata", {"node": {}})
    r = cli("ceph", "versions", "-p", "p1")
    assert r.exit_code == 0 and "No Ceph on this cluster" in r.output
    api.route("GET", "/cluster/ceph/metadata", _no_ceph())
    r = cli("ceph", "versions", "-p", "p1")
    assert r.exit_code == 0 and "No Ceph on this cluster" in r.output


def test_versions_unreachable(cli, api):
    api.route("GET", "/cluster/ceph/metadata", api.error(500, "got timeout"))
    r = cli("ceph", "versions", "-p", "p1")
    assert r.exit_code == 1 and "got timeout" in r.stderr


# ── log ─────────────────────────────────────────────────────────────────


def _log(api: FakeAPI, lines: list[str]) -> None:
    """The real endpoint: start is an offset from the FIRST line."""
    entries = [{"n": i + 1, "t": t} for i, t in enumerate(lines)]

    def page(call: Any) -> list[dict]:
        params = call.params or {}
        start = int(params.get("start", 0))
        limit = int(params.get("limit", 50))
        return entries[start:start + limit]

    api.route("GET", "/nodes/{node}/ceph/log", page)


def test_log_shows_the_most_recent_lines(cli, api):
    api.route("GET", "/nodes", NODES)
    lines = [f"2026-11-18T08:{i // 60:02d}:{i % 60:02d} mon.pve1 line-{i:05d}" for i in range(3000)]
    _log(api, lines)
    r = cli("ceph", "log", "-p", "p1", "--limit", "20")
    assert r.exit_code == 0, r.output
    shown = [line for line in r.stdout.splitlines() if "line-" in line]
    assert [s.rsplit(" ", 1)[1] for s in shown] == [f"line-{i:05d}" for i in range(2980, 3000)]
    assert "line-00000" not in r.stdout
    assert all(c.path == "/nodes/pve1/ceph/log" for c in api.called("GET", "/nodes/{node}/ceph/log"))


def test_log_shorter_than_limit_shows_everything(cli, api):
    api.route("GET", "/nodes", NODES)
    _log(api, [f"line-{i}" for i in range(7)])
    r = cli("ceph", "log", "pve2", "-p", "p1")
    assert r.exit_code == 0
    assert [line for line in r.stdout.splitlines() if line.startswith("line-")] == [f"line-{i}" for i in range(7)]
    # Node given: no node lookup, the log is read where asked
    assert {c.path for c in api.called("GET", "/nodes/{node}/ceph/log")} == {"/nodes/pve2/ceph/log"}


def test_log_brackets_are_kept_verbatim(cli, api):
    api.route("GET", "/nodes", NODES)
    raw = [
        "osd.0 [WRN] osd_mclock_max_capacity_iops_[hdd|ssd] is set",
        "cluster [ERR] Health check failed: 1 osds down [/osd]",
        "cluster [INF] overall HEALTH_OK [bold]not markup[/bold]",
    ]
    _log(api, raw)
    r = cli("ceph", "log", "-p", "p1")
    assert r.exit_code == 0, r.output
    for line in raw:
        assert line in r.stdout


def test_log_empty_no_ceph_unreachable(cli, api):
    api.route("GET", "/nodes", NODES)
    _log(api, [])
    r = cli("ceph", "log", "-p", "p1")
    assert r.exit_code == 0 and "Ceph log is empty" in r.output

    api.route("GET", "/nodes/{node}/ceph/log", _no_ceph())
    r = cli("ceph", "log", "-p", "p1")
    assert r.exit_code == 0 and "No Ceph on this cluster" in r.output

    api.route("GET", "/nodes/{node}/ceph/log", api.error(500, "got timeout"))
    r = cli("ceph", "log", "-p", "p1")
    assert r.exit_code == 1 and "got timeout" in r.stderr


# ── fleet ───────────────────────────────────────────────────────────────


def test_status_on_two_clusters_one_without_ceph(cli, api):
    api.route("GET", "/cluster/status", fixture("pve-9.1/cluster_status"), host="h1")
    api.route("GET", "/cluster/status", fixture("pve-9.2/cluster_status"), host="h2")
    api.route("GET", "/cluster/ceph/status", fixture("pve-9.1/ceph_status"), host="h1")
    api.route("GET", "/cluster/ceph/flags", fixture("pve-9.1/ceph_flags"), host="h1")
    api.route("GET", "/cluster/ceph/status", _no_ceph(), host="h2")
    r = cli("ceph", "status", "-p", "p1,p2")
    assert r.exit_code == 0, r.output
    assert "HEALTH_OK" in r.stdout
    assert "No Ceph on this cluster" in r.output
