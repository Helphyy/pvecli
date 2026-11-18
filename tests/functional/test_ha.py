"""HA placement of guests: groups vs rules mode, foreign rules, purge, live status."""

from __future__ import annotations

from typing import Any

from tests.fake_api import FakeAPI, fixture

NODES = [{"node": n, "status": "online"} for n in ("pve1", "pve2", "pve3")]
GUESTS = [
    {"vmid": 100, "type": "qemu", "node": "pve1", "status": "running", "name": "web"},
    {"vmid": 101, "type": "lxc", "node": "pve1", "status": "running", "name": "dns"},
]


def recorded(api: FakeAPI, name: str) -> Any:
    """A recorded response, turned into the real API error when it was one."""
    data = fixture(name)
    if isinstance(data, dict) and "__error__" in data:
        return api.error(data["__status__"], data["__error__"])
    return data


def setup(api: FakeAPI, mode: str, ha_resources: list[dict[str, Any]] | None = None,
          rules: list[dict[str, Any]] | None = None) -> None:
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/cluster/resources", GUESTS)
    api.route("GET", "/cluster/ha/resources", ha_resources or [])
    if mode == "rules":
        # Real PVE 9.1 answer once the groups were converted to rules
        api.route("GET", "/cluster/ha/groups", recorded(api, "pve-9.1/ha_groups"))
        api.route("GET", "/cluster/ha/rules", rules or [])
    else:
        api.route("GET", "/cluster/ha/groups", [{"group": "g1", "nodes": "pve1,pve2"}])
    api.route("POST", "/cluster/ha/resources", None)
    api.route("PUT", "/cluster/ha/resources/{sid}", None)
    api.route("DELETE", "/cluster/ha/resources/{sid}", None)
    api.route("POST", "/cluster/ha/rules", None)
    api.route("PUT", "/cluster/ha/rules/{rule}", None)
    api.route("DELETE", "/cluster/ha/rules/{rule}", None)


# ── rules mode (PVE 9 after migration) ───────────────────────────────────

def test_rules_mode_add_creates_resource_then_owned_rule(cli, api):
    setup(api, "rules")
    r = cli("vm", "ha", "add", "100", "--nodes", "pve1:2,pve2", "--strict", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    writes = api.writes()
    assert [(w.method, w.path) for w in writes] == [
        ("POST", "/cluster/ha/resources"),
        ("POST", "/cluster/ha/rules"),
    ]
    res = writes[0].data
    assert res["sid"] == "vm:100"
    assert "group" not in res
    rule = writes[1].data
    assert rule["rule"] == "pvecli-vm100"
    assert rule["type"] == "node-affinity"
    assert rule["resources"] == "vm:100"
    assert rule["nodes"] == "pve1:2,pve2"
    assert rule["strict"] == 1


def test_rules_mode_ct_add_uses_ct_sid(cli, api):
    setup(api, "rules")
    r = cli("ct", "ha", "add", "101", "--nodes", "pve2", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    res, rule = api.writes()
    assert res.data["sid"] == "ct:101"
    assert rule.data["rule"] == "pvecli-ct101"
    assert rule.data["resources"] == "ct:101"
    assert rule.data["strict"] == 0


def test_rules_mode_refuses_group_without_writing(cli, api):
    setup(api, "rules")
    r = cli("vm", "ha", "add", "100", "--group", "g1", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "migrated to HA rules" in r.output
    assert "--nodes" in r.output
    assert api.writes() == []


def test_rules_mode_unknown_node_refused_before_any_write(cli, api):
    setup(api, "rules")
    r = cli("vm", "ha", "add", "100", "--nodes", "pve9", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Unknown node 'pve9'" in r.output
    assert api.writes() == []


# ── groups mode (PVE 8, or 9 before migration) ───────────────────────────

def test_groups_mode_refuses_nodes_without_writing(cli, api):
    setup(api, "groups")
    r = cli("vm", "ha", "add", "100", "--nodes", "pve1", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "--group" in r.output
    assert api.writes() == []


def test_groups_mode_passes_group_and_never_touches_rules(cli, api):
    setup(api, "groups")
    r = cli("vm", "ha", "add", "100", "--group", "g1", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    writes = api.writes()
    assert [(w.method, w.path) for w in writes] == [("POST", "/cluster/ha/resources")]
    assert writes[0].data["group"] == "g1"
    assert not api.called("GET", "/cluster/ha/rules")


# ── rules created outside pvecli ─────────────────────────────────────────

FOREIGN = [{"rule": "site-a", "type": "node-affinity", "resources": "vm:100,vm:102", "nodes": "pve3"}]


def test_set_never_modifies_a_foreign_rule(cli, api):
    setup(api, "rules", ha_resources=[{"sid": "vm:100", "state": "started"}], rules=FOREIGN)
    r = cli("vm", "ha", "set", "100", "--nodes", "pve1", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "site-a" in r.output
    assert "did not create" in r.output
    assert api.writes() == []


def test_set_strict_never_modifies_a_foreign_rule(cli, api):
    setup(api, "rules", ha_resources=[{"sid": "vm:100", "state": "started"}], rules=FOREIGN)
    r = cli("vm", "ha", "set", "100", "--strict", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "site-a" in r.output
    assert api.writes() == []


def test_add_never_modifies_a_foreign_rule(cli, api):
    setup(api, "rules", rules=FOREIGN)
    r = cli("vm", "ha", "add", "100", "--nodes", "pve1", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "site-a" in r.output
    assert not [w for w in api.writes() if w.path.startswith("/cluster/ha/rules")]


def test_add_with_foreign_rule_registers_nothing(cli, api):
    setup(api, "rules", rules=FOREIGN)
    r = cli("vm", "ha", "add", "100", "--nodes", "pve1", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_set_updates_its_own_rule(cli, api):
    own = [{"rule": "pvecli-vm100", "type": "node-affinity", "resources": "vm:100", "nodes": "pve1"}]
    setup(api, "rules", ha_resources=[{"sid": "vm:100", "state": "started"}], rules=own)
    r = cli("vm", "ha", "set", "100", "--nodes", "pve2:3", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    (w,) = api.writes()
    assert (w.method, w.path) == ("PUT", "/cluster/ha/rules/pvecli-vm100")
    assert w.data == {"type": "node-affinity", "nodes": "pve2:3"}


# ── remove: purge only where rules exist ─────────────────────────────────

def test_remove_purges_in_rules_mode(cli, api):
    setup(api, "rules", ha_resources=[{"sid": "vm:100", "state": "started"}])
    r = cli("vm", "ha", "remove", "100", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    (w,) = api.writes()
    assert (w.method, w.path) == ("DELETE", "/cluster/ha/resources/vm:100")
    assert w.params == {"purge": 1}


def test_remove_never_purges_in_groups_mode(cli, api):
    setup(api, "groups", ha_resources=[{"sid": "ct:101", "state": "started"}])
    r = cli("ct", "ha", "remove", "101", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    (w,) = api.writes()
    assert (w.method, w.path) == ("DELETE", "/cluster/ha/resources/ct:101")
    assert not w.params or "purge" not in w.params


# ── status: live columns keyed by sid ────────────────────────────────────

def live_status() -> list[dict[str, Any]]:
    # Real 9.1 entries (quorum, master, lrm) plus the service entry PVE adds
    # per HA resource: 'id' is service:vm:100, 'sid' is vm:100.
    return fixture("pve-9.1/ha_status_current") + [{
        "id": "service:vm:100", "sid": "vm:100", "type": "service",
        "node": "pve3", "state": "migrate", "crm_state": "migrate",
        "request_state": "started", "status": "vm:100 (pve3, migrate)",
    }]


def test_status_list_fills_current_and_node_from_sid(cli, api):
    setup(api, "rules", ha_resources=[{"sid": "vm:100", "state": "started", "type": "vm"}],
          rules=[{"rule": "pvecli-vm100", "type": "node-affinity", "resources": "vm:100", "nodes": "pve3"}])
    api.route("GET", "/cluster/ha/status/current", live_status())
    r = cli("vm", "ha", "status", "-p", "p1")
    assert r.exit_code == 0, r.output
    row = next(line for line in r.stdout.splitlines() if "vm:100" in line)
    assert "migrate" in row
    assert "pve3" in row
    assert "pvecli-vm100" in row
    assert api.writes() == []


def test_status_single_guest_shows_current_state_and_node(cli, api):
    setup(api, "groups", ha_resources=[{"sid": "vm:100", "state": "started", "group": "g1"}])
    api.route("GET", "/cluster/ha/status/current", live_status())
    r = cli("vm", "ha", "status", "100", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Current state:" in r.stdout and "migrate" in r.stdout
    assert "Node:" in r.stdout and "pve3" in r.stdout
    assert "g1" in r.stdout


def test_add_without_a_terminal_says_to_pass_yes(cli, api, monkeypatch):
    from src.utils import menu
    monkeypatch.setattr(menu, "_interactive", lambda: False)
    setup(api, "rules")
    r = cli("vm", "ha", "add", "100", "-p", "p1")
    assert r.exit_code == 1
    assert "pass -y" in r.output
    assert api.writes() == []
