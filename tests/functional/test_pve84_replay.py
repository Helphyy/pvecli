"""PVE 8.4 fallbacks, replayed from answers recorded on a real 8.4.21 cluster.

pvecli detects what an API offers by calling it: a 501 "not implemented"
sends it down the older path. These tests feed the recorded PVE 8.4 answers
(tests/fixtures/pve-8.4, cluster pve8lab: nodes pve8a and pve8b) to the
commands whose fallback only exists for clusters older than PVE 9.
"""

from __future__ import annotations

from typing import Any

from tests.fake_api import FakeAPI, fixture, upid

V = "pve-8.4"


def recorded(api: FakeAPI, name: str) -> Any:
    """A recorded response, or the recorded error of PVE 8.4."""
    data = fixture(f"{V}/{name}")
    if isinstance(data, dict) and "__error__" in data:
        return api.error(data["__status__"], data["__error__"])
    return data


def not_implemented(method: str, path: str) -> Any:
    """How PVE 8.4 refuses an endpoint it lacks: same status and wording as
    the recorded GET errors (bulk_action_guest, ha_rules, sdn_dry_run)."""
    rec = fixture(f"{V}/bulk_action_guest")
    from src.api.exceptions import APIError

    return APIError(f"Method '{method} {path}' not implemented", status_code=rec["__status__"])


def cluster(api: FakeAPI) -> None:
    api.route("GET", "/nodes", recorded(api, "nodes"))
    api.route("GET", "/cluster/status", recorded(api, "cluster_status"))
    api.route("GET", "/cluster/resources", recorded(api, "cluster_resources_vm"))
    api.route("GET", "/cluster/ha/resources", recorded(api, "ha_resources"))
    api.route("GET", "/cluster/ha/groups", recorded(api, "ha_groups"))
    api.route("GET", "/cluster/ha/rules", recorded(api, "ha_rules"))
    api.route("GET", "/cluster/ha/status/current", recorded(api, "ha_status_current"))


def test_recorded_answers_are_the_pve_8_ones():
    """Guard: the fixtures really come from 8.4 and show the missing endpoints."""
    assert fixture(f"{V}/version")["release"] == "8.4"
    for name in ("bulk_action_guest", "ha_rules", "sdn_dry_run", "lxc_migrate_preconditions"):
        assert fixture(f"{V}/{name}")["__status__"] == 501, name
    assert [g["group"] for g in fixture(f"{V}/ha_groups")] == ["prefa"]


# ── HA: groups mode, no rules ────────────────────────────────────────────


def test_ha_add_with_nodes_is_refused_in_groups_mode(cli, api):
    """/cluster/ha/groups answers (group prefa) and /rules is 501: a PVE 8
    cluster, HA pinning goes through groups."""
    cluster(api)
    api.route("GET", "/cluster/ha/resources", [])
    r = cli("vm", "ha", "add", "130", "--nodes", "pve8b", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "--group" in r.output
    assert api.writes() == []


def test_ha_add_with_a_group_never_touches_rules(cli, api):
    cluster(api)
    api.route("GET", "/cluster/ha/resources", [])
    api.route("POST", "/cluster/ha/resources", None)
    r = cli("vm", "ha", "add", "130", "--group", "prefa", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert (w.method, w.path, w.data["sid"], w.data["group"]) == ("POST", "/cluster/ha/resources", "vm:130", "prefa")
    assert not any("/ha/rules" in c.path for c in api.calls if c.method != "GET")


def test_ha_disarm_says_it_needs_92(cli, api):
    cluster(api)
    api.route("POST", "/cluster/ha/status/disarm-ha",
              not_implemented("POST", "/cluster/ha/status/disarm-ha"))
    r = cli("cluster", "ha", "disarm", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "needs PVE 9.2" in r.output


# ── bulk actions: per-node startall/stopall ──────────────────────────────


def test_bulk_start_falls_back_on_startall_per_node(cli, api):
    cluster(api)
    api.route("GET", "/cluster/ha/resources", [])
    api.route("POST", "/cluster/bulk-action/guest/{action}",
              lambda c: (_ for _ in ()).throw(not_implemented("POST", c.path)))
    api.route("POST", "/nodes/{node}/startall", lambda c: upid(c.match["node"], "startall"))
    api.route("GET", "/nodes/{node}/qemu/{vmid}/status/current", {"status": "running"})
    api.route("GET", "/nodes/{node}/lxc/{vmid}/status/current", {"status": "running"})
    r = cli("cluster", "guests", "start", "--vms", "101,111", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    by_node = {c.match["node"]: c.data for c in api.called("POST", "/nodes/{node}/startall")}
    # 101 sits on pve8a, 111 on pve8b in the recorded resources
    assert by_node == {"pve8a": {"vms": "101", "force": 1}, "pve8b": {"vms": "111", "force": 1}}


def test_bulk_shutdown_of_ha_guests_follows_the_ha_manager(cli, api, clock):
    """Recorded HA guests 120 and 125 (group prefa): stopall only asks HA to
    stop them, the check waits for HA to report them stopped."""
    cluster(api)
    api.route("POST", "/cluster/bulk-action/guest/{action}",
              lambda c: (_ for _ in ()).throw(not_implemented("POST", c.path)))
    api.route("POST", "/nodes/{node}/stopall", lambda c: upid(c.match["node"], "stopall"))
    base = recorded(api, "ha_status_current")
    polls = {"n": 0}

    def ha_status(_call: Any) -> list[dict[str, Any]]:
        polls["n"] += 1
        state = "request_stop" if polls["n"] < 3 else "stopped"
        return [dict(e, state=state) if e.get("type") == "service" else e for e in base]

    api.route("GET", "/cluster/ha/status/current", ha_status)
    # The live status still says running while HA shuts them down
    api.route("GET", "/nodes/{node}/qemu/{vmid}/status/current", {"status": "running"})
    r = cli("cluster", "guests", "shutdown", "--vms", "120,125", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "done for 2 guest(s)" in r.output
    assert [c.match["node"] for c in api.called("POST", "/nodes/{node}/stopall")] == ["pve8a"]


# ── migration: no preconditions endpoint for containers ─────────────────


def test_ct_migration_is_attempted_without_preconditions(cli, api):
    cluster(api)
    api.route("GET", "/nodes/pve8a/lxc/141/status/current", {"status": "stopped", "name": "ct101"})
    api.route("GET", "/nodes/pve8a/lxc/141/migrate", recorded(api, "lxc_migrate_preconditions"))
    api.route("POST", "/nodes/pve8a/lxc/141/migrate", upid("pve8a", "vzmigrate", 141))
    r = cli("ct", "migrate", "141", "pve8b", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert w.data == {"target": "pve8b"}


# ── SDN apply: no dry-run, no lock ───────────────────────────────────────


def test_sdn_apply_without_preview_nor_lock(cli, api):
    api.route("GET", "/cluster/sdn/zones", [{"zone": "ztest", "type": "simple", "state": "new"}])
    api.route("GET", "/cluster/sdn/vnets", [])
    api.route("GET", "/cluster/sdn/vnets/{vnet}/subnets", [])
    api.route("GET", "/cluster/config/totem", {"cluster_name": "pve8lab"})
    api.route("GET", "/nodes", recorded(api, "nodes"))
    api.route("GET", "/cluster/sdn/dry-run", recorded(api, "sdn_dry_run"))
    api.route("GET", "/nodes/{node}/sdn/zones", [])
    api.route("POST", "/cluster/sdn/lock", not_implemented("POST", "/cluster/sdn/lock"))
    api.route("PUT", "/cluster/sdn", upid("pve8a", "reloadnetworkall"))
    r = cli("sdn", "apply", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.called("PUT", "/cluster/sdn")
    assert put.data is None
    assert "SDN configuration applied" in r.stdout
