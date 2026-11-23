"""Changing the firewall: rules, aliases, IP sets, security groups.

Against tests/fake_firewall.py, which behaves like PVE 9.1 did on the lab,
so each test checks the firewall as it ends up. p1 may write to the whole
cluster (firewall_write), p2 to single guests only.
"""

import pytest

from src.cli.firewall_edit import renamed, section_of, takes_digest
from tests.fake_firewall import FakeFirewall

NODES = [{"node": "pve1", "status": "online"}]
GUESTS = [{"vmid": 101, "type": "qemu", "node": "pve1", "name": "web"},
          {"vmid": 102, "type": "qemu", "node": "pve1", "name": "db"}]
DC = "/cluster/firewall"
VM = "/nodes/pve1/qemu/101/firewall"
VM2 = "/nodes/pve1/qemu/102/firewall"


@pytest.fixture
def fw(api, monkeypatch):
    import src.cli.firewall_guard as guard

    # pvecli talks to pve1 from inside its cluster network (see test_firewall_switch.py)
    api.route("GET", "/cluster/status", [{"type": "node", "name": "pve1", "ip": "10.0.0.1"}])
    api.route("GET", "/nodes/pve1/network", [{"iface": "vmbr0", "cidr": "10.0.0.1/24"}])
    api.route("GET", "/version", {"version": "9.1"})
    monkeypatch.setattr(guard, "source_address", lambda host, port: "10.0.0.7")
    monkeypatch.setattr(guard, "APPLY_DELAY", 0)

    async def pve1(client, host):
        return "pve1"

    monkeypatch.setattr(guard, "api_node", pve1)
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/cluster/resources", GUESTS)
    api.route("GET", "/cluster/sdn/vnets", [])
    for g in GUESTS:
        api.route("GET", f"/nodes/pve1/qemu/{g['vmid']}/config", {"net0": "virtio,bridge=vmbr0,firewall=1"})
    return FakeFirewall(api)


def rule(**kw):
    return {"type": "in", "action": "ACCEPT", "enable": 1, **kw}


def ok(r):
    assert r.exit_code == 0, r.output
    return r


# ── rules ────────────────────────────────────────────────────────────────


def test_add_goes_first_by_default(cli, fw):
    fw.level(VM).rules[:] = [rule(macro="SSH")]
    ok(cli("vm", "firewall", "rule", "add", "101", "-ac", "drop", "-pt", "tcp", "-dpt", "23", "-y", "-p", "p2"))
    assert [r.get("dport") or r.get("macro") for r in fw.level(VM).rules] == ["23", "SSH"]
    assert fw.level(VM).rules[0]["action"] == "DROP"


def test_add_at_a_position_never_acts_from_the_top(cli, fw, api):
    fw.level(VM).rules[:] = [rule(macro="SSH"), rule(macro="HTTP")]
    ok(cli("vm", "firewall", "rule", "add", "101", "-ac", "DROP", "-src", "10.9.0.0/16", "-po", "2", "-y", "-p", "p2"))
    assert [r.get("macro") or r.get("source") for r in fw.level(VM).rules] == ["SSH", "HTTP", "10.9.0.0/16"]
    assert fw.level(VM).rules[2]["enable"] == 1
    # Created disabled at the top, moved, then enabled
    post = api.called("POST", f"{VM}/rules")[0]
    assert post.data["enable"] == 0


def test_add_disabled_stays_disabled_at_its_place(cli, fw):
    fw.level(VM).rules[:] = [rule(macro="SSH")]
    ok(cli("vm", "firewall", "rule", "add", "101", "-ac", "ACCEPT", "-ma", "HTTP", "-po", "1", "--disabled",
           "-y", "-p", "p2"))
    assert fw.level(VM).rules[1] == rule(macro="HTTP", enable=0)


def test_add_out_of_range(cli, fw):
    r = cli("vm", "firewall", "rule", "add", "101", "-ac", "ACCEPT", "-po", "3", "-y", "-p", "p2")
    assert r.exit_code == 1 and "out of range: 0 to 0" in r.output


def test_add_inserts_a_security_group(cli, fw):
    fw.groups["web"] = {"comment": "", "rules": []}
    ok(cli("vm", "firewall", "rule", "add", "101", "-ac", "web", "-y", "-p", "p2"))
    assert fw.level(VM).rules[0] == {"type": "group", "action": "web", "enable": 1}


@pytest.mark.parametrize("args, error", [
    (["-ac", "MAYBE"], "Unknown action 'MAYBE'"),
    (["-ac", "ACCEPT", "-dn", "forward"], "Direction 'forward' is not possible on VM 101"),
    (["-ac", "ACCEPT", "-lg", "loud"], "Unknown log level 'loud'"),
])
def test_add_refuses_bad_input(cli, fw, args, error):
    r = cli("vm", "firewall", "rule", "add", "101", *args, "-y", "-p", "p2")
    assert r.exit_code == 1 and error in r.output
    assert fw.level(VM).rules == []


def test_a_group_rule_takes_no_port(cli, fw):
    fw.groups["web"] = {"comment": "", "rules": []}
    r = cli("vm", "firewall", "rule", "add", "101", "-ac", "web", "-dpt", "80", "-y", "-p", "p2")
    assert r.exit_code == 1 and "takes no macro, protocol nor port" in r.output


def test_vnet_rules_are_forward(cli, fw, api):
    api.route("GET", "/cluster/sdn/vnets", [{"vnet": "v1", "zone": "z"}])
    ok(cli("sdn", "vnet", "firewall", "rule", "add", "v1", "-ac", "ACCEPT", "-pt", "icmp", "-y", "-p", "p1"))
    assert fw.level("/cluster/sdn/vnets/v1/firewall").rules[0]["type"] == "forward"


def test_edit_changes_only_what_is_given_and_clears(cli, fw):
    fw.level(VM).rules[:] = [rule(proto="tcp", dport="23", source="10.0.0.0/8", comment="telnet")]
    ok(cli("vm", "firewall", "rule", "edit", "101", "0", "-dpt", "2323", "--clear", "source", "-y", "-p", "p2"))
    assert fw.level(VM).rules[0] == rule(proto="tcp", dport="2323", comment="telnet")


def test_edit_needs_something_to_change(cli, fw):
    fw.level(VM).rules[:] = [rule()]
    r = cli("vm", "firewall", "rule", "edit", "101", "0", "-y", "-p", "p2")
    assert r.exit_code == 1 and "Nothing to change" in r.output


def test_remove_several_last_first(cli, fw, api):
    fw.level(VM).rules[:] = [rule(dport=str(p), proto="tcp") for p in (1, 2, 3, 4)]
    ok(cli("vm", "firewall", "rule", "remove", "101", "0", "2", "-y", "-p", "p2"))
    assert [r["dport"] for r in fw.level(VM).rules] == ["2", "4"]
    assert [c.path.rsplit("/", 1)[1] for c in api.called("DELETE", f"{VM}/rules/{{p}}")] == ["2", "0"]


def test_remove_from_a_menu(cli, fw, ui):
    fw.level(VM).rules[:] = [rule(macro="SSH"), rule(macro="HTTP")]
    ui.multi(["HTTP"])
    ui.confirm(True)
    ok(cli("vm", "firewall", "rule", "remove", "101", "-p", "p2"))
    assert [r["macro"] for r in fw.level(VM).rules] == ["SSH"]


def test_remove_declined_writes_nothing(cli, fw, ui, api):
    fw.level(VM).rules[:] = [rule(macro="SSH")]
    ui.confirm(False)
    ok(cli("vm", "firewall", "rule", "remove", "101", "0", "-p", "p2"))
    assert fw.level(VM).rules == [rule(macro="SSH")]
    assert [c for c in api.writes() if "firewall" in c.path] == []


@pytest.mark.parametrize("move, order", [((0, 2), "BCA"), ((2, 0), "CAB"), ((1, 2), "ACB"), ((1, 0), "BAC")])
def test_move_lands_where_asked(cli, fw, move, order):
    fw.level(VM).rules[:] = [rule(comment=c) for c in "ABC"]
    ok(cli("vm", "firewall", "rule", "move", "101", str(move[0]), str(move[1]), "-y", "-p", "p2"))
    assert "".join(r["comment"] for r in fw.level(VM).rules) == order


def test_disable_then_enable(cli, fw):
    fw.level(VM).rules[:] = [rule(macro="SSH"), rule(macro="HTTP")]
    ok(cli("vm", "firewall", "rule", "disable", "101", "0", "1", "-y", "-p", "p2"))
    assert [r["enable"] for r in fw.level(VM).rules] == [0, 0]
    r = ok(cli("vm", "firewall", "rule", "disable", "101", "0", "-y", "-p", "p2"))
    assert "Already disabled" in r.output
    ok(cli("vm", "firewall", "rule", "enable", "101", "1", "-y", "-p", "p2"))
    assert [r["enable"] for r in fw.level(VM).rules] == [0, 1]


def test_rules_of_a_security_group(cli, fw):
    fw.groups["web"] = {"comment": "", "rules": [rule(macro="HTTP")]}
    ok(cli("cluster", "firewall", "rule", "add", "-g", "web", "-ac", "ACCEPT", "-ma", "HTTPS", "-po", "1",
           "-y", "-p", "p1"))
    assert [r["macro"] for r in fw.groups["web"]["rules"]] == ["HTTP", "HTTPS"]


def test_the_first_write_carries_the_digest_of_its_list(cli, fw, api):
    fw.level(VM).rules[:] = [rule(macro="SSH")]
    ok(cli("vm", "firewall", "rule", "disable", "101", "0", "-y", "-p", "p2"))
    assert api.called("PUT", f"{VM}/rules/{{p}}")[0].data["digest"]


def test_a_list_changed_meanwhile_is_refused(cli, fw, monkeypatch):
    fw.level(VM).rules[:] = [rule(macro="SSH"), rule(macro="HTTP")]

    def someone_else(message, *a, **k):
        # Another admin adds a rule while this plan waits for its answer
        fw.level(VM).rules.insert(0, rule(macro="DNS"))
        return True

    import src.cli.firewall_edit

    monkeypatch.setattr(src.cli.firewall_edit, "confirm", someone_else)
    r = cli("vm", "firewall", "rule", "remove", "101", "0", "-p", "p2")
    assert r.exit_code == 1 and "detected modified configuration" in r.output
    # SSH, the rule meant, is not removed by position from a list that moved
    assert [r["macro"] for r in fw.level(VM).rules] == ["DNS", "SSH", "HTTP"]


# ── who may write where ──────────────────────────────────────────────────


@pytest.mark.parametrize("args", [
    ("cluster", "firewall", "rule", "add", "-ac", "ACCEPT"),
    ("cluster", "firewall", "alias", "add", "gw", "10.0.0.1"),
    ("cluster", "firewall", "group", "add", "web"),
    ("node", "firewall", "rule", "add", "pve1", "-ac", "ACCEPT"),
])
def test_cluster_wide_writes_need_firewall_write(cli, fw, args):
    r = cli(*args, "-y", "-p", "p2")
    assert r.exit_code == 1
    assert "firewall_write: true" in r.output
    assert fw.level(DC).rules == [] and fw.level(DC).aliases == {} and fw.groups == {}


def test_a_guest_needs_no_switch(cli, fw):
    ok(cli("vm", "firewall", "alias", "add", "101", "gw", "10.0.0.1", "-y", "-p", "p2"))
    assert fw.level(VM).aliases == {"gw": {"cidr": "10.0.0.1"}}


# ── aliases ──────────────────────────────────────────────────────────────


def test_alias_add_checks_its_input(cli, fw):
    assert cli("cluster", "firewall", "alias", "add", "1gw", "10.0.0.1", "-y", "-p", "p1").exit_code == 1
    assert cli("cluster", "firewall", "alias", "add", "gw", "not-an-ip", "-y", "-p", "p1").exit_code == 1
    ok(cli("cluster", "firewall", "alias", "add", "gw", "10.0.0.0/24", "-c", "lan", "-y", "-p", "p1"))
    r = cli("cluster", "firewall", "alias", "add", "gw", "10.0.0.1", "-y", "-p", "p1")
    assert r.exit_code == 1 and "already exists" in r.output


def _dc_alias_in_use(fw):
    fw.level(DC).aliases["gw"] = {"cidr": "10.0.0.1"}
    fw.level(DC).rules[:] = [rule(source="gw")]
    fw.level(VM).rules[:] = [rule(source="dc/gw"), rule(dest="gw")]
    fw.level(DC).ipsets["admins"] = {"comment": "", "entries": [{"cidr": "gw"}]}
    fw.groups["web"] = {"comment": "", "rules": [rule(source="10.0.0.5,gw")]}


def test_alias_in_use_is_not_removed(cli, fw):
    _dc_alias_in_use(fw)
    r = cli("cluster", "firewall", "alias", "remove", "gw", "-y", "-p", "p1")
    assert r.exit_code == 1
    for where in ("datacenter, rule 0 (source)", "security group web, rule 0 (source)",
                  "VM 101, rule 0 (source)", "VM 101, rule 1 (dest)", "datacenter, IP set admins"):
        assert where in r.output
    assert "gw" in fw.level(DC).aliases


def test_alias_removed_with_force(cli, fw):
    _dc_alias_in_use(fw)
    ok(cli("cluster", "firewall", "alias", "remove", "gw", "-f", "-y", "-p", "p1"))
    assert fw.level(DC).aliases == {}


def test_a_guest_alias_of_the_same_name_hides_the_datacenter_one(cli, fw):
    fw.level(DC).aliases["gw"] = {"cidr": "10.0.0.1"}
    fw.level(VM).aliases["gw"] = {"cidr": "10.1.0.1"}
    fw.level(VM).rules[:] = [rule(source="gw")]  # the alias of VM 101
    ok(cli("cluster", "firewall", "alias", "remove", "gw", "-y", "-p", "p1"))
    r = cli("vm", "firewall", "alias", "remove", "101", "gw", "-y", "-p", "p1")
    assert r.exit_code == 1 and "VM 101, rule 0 (source)" in r.output


def test_alias_rename_carries_every_reference_in_its_form(cli, fw):
    _dc_alias_in_use(fw)
    ok(cli("cluster", "firewall", "alias", "edit", "gw", "--rename", "router", "-y", "-p", "p1"))
    assert set(fw.level(DC).aliases) == {"router"}
    assert fw.level(DC).rules[0]["source"] == "router"
    assert [r.get("source") or r.get("dest") for r in fw.level(VM).rules] == ["dc/router", "router"]
    assert fw.groups["web"]["rules"][0]["source"] == "10.0.0.5,router"
    assert fw.level(DC).ipsets["admins"]["entries"] == [{"cidr": "router"}]


def test_alias_rename_leaves_a_shadowing_guest_alone(cli, fw):
    fw.level(DC).aliases["gw"] = {"cidr": "10.0.0.1"}
    fw.level(VM).aliases["gw"] = {"cidr": "10.1.0.1"}
    fw.level(VM).rules[:] = [rule(source="gw"), rule(source="dc/gw")]
    ok(cli("cluster", "firewall", "alias", "edit", "gw", "--rename", "router", "-y", "-p", "p1"))
    assert [r["source"] for r in fw.level(VM).rules] == ["gw", "dc/router"]


def test_alias_edit_address_and_comment(cli, fw):
    fw.level(VM).aliases["gw"] = {"cidr": "10.0.0.1", "comment": "old"}
    ok(cli("vm", "firewall", "alias", "edit", "101", "gw", "--cidr", "10.0.0.2", "-y", "-p", "p2"))
    assert fw.level(VM).aliases["gw"] == {"cidr": "10.0.0.2", "comment": "old"}


# ── IP sets ──────────────────────────────────────────────────────────────


def test_ipset_add_creates_and_skips_what_is_there(cli, fw):
    ok(cli("cluster", "firewall", "ipset", "add", "admins", "10.0.0.0/24", "10.0.0.66", "-y", "-p", "p1"))
    ok(cli("cluster", "firewall", "ipset", "add", "admins", "10.0.0.66", "10.0.1.0/24", "-y", "-p", "p1"))
    assert [e["cidr"] for e in fw.level(DC).ipsets["admins"]["entries"]] == ["10.0.0.0/24", "10.0.0.66", "10.0.1.0/24"]
    ok(cli("cluster", "firewall", "ipset", "add", "admins", "10.0.0.7", "--nomatch", "-y", "-p", "p1"))
    assert fw.level(DC).ipsets["admins"]["entries"][-1] == {"cidr": "10.0.0.7", "nomatch": 1}


def test_ipset_management_warns(cli, fw):
    r = ok(cli("cluster", "firewall", "ipset", "add", "management", "10.0.0.0/24", "-y", "-p", "p1"))
    assert "opens the web interface and SSH" in r.output


def test_ipset_remove_entries_then_the_set(cli, fw):
    fw.level(DC).ipsets["admins"] = {"comment": "", "entries": [{"cidr": "10.0.0.0/24"}, {"cidr": "10.0.1.0/24"}]}
    ok(cli("cluster", "firewall", "ipset", "remove", "admins", "10.0.0.0/24", "-y", "-p", "p1"))
    assert fw.level(DC).ipsets["admins"]["entries"] == [{"cidr": "10.0.1.0/24"}]
    r = cli("cluster", "firewall", "ipset", "remove", "admins", "10.9.9.9", "-y", "-p", "p1")
    assert r.exit_code == 1 and "Not in IP set 'admins': 10.9.9.9" in r.output
    ok(cli("cluster", "firewall", "ipset", "remove", "admins", "-y", "-p", "p1"))
    assert fw.level(DC).ipsets == {}


def test_ipset_in_use_is_not_removed(cli, fw):
    fw.level(DC).ipsets["admins"] = {"comment": "", "entries": []}
    fw.level(VM2).rules[:] = [rule(source="+admins")]
    r = cli("cluster", "firewall", "ipset", "remove", "admins", "-y", "-p", "p1")
    assert r.exit_code == 1 and "VM 102, rule 0 (source)" in r.output


def test_ipset_rename_copies_entries_and_carries_references(cli, fw):
    fw.level(DC).ipsets["admins"] = {"comment": "ops", "entries": [{"cidr": "10.0.0.0/24", "comment": "lan"}]}
    fw.level(VM).rules[:] = [rule(source="+dc/admins"), rule(dest="+admins")]
    ok(cli("cluster", "firewall", "ipset", "edit", "admins", "--rename", "ops", "-y", "-p", "p1"))
    assert set(fw.level(DC).ipsets) == {"ops"}
    assert fw.level(DC).ipsets["ops"] == {"comment": "ops", "entries": [{"cidr": "10.0.0.0/24", "comment": "lan"}]}
    assert [r.get("source") or r.get("dest") for r in fw.level(VM).rules] == ["+dc/ops", "+ops"]


# ── security groups ──────────────────────────────────────────────────────


def test_group_add_rename_and_remove(cli, fw):
    ok(cli("cluster", "firewall", "group", "add", "web", "-c", "front", "-y", "-p", "p1"))
    fw.groups["web"]["rules"].append(rule(macro="HTTP"))
    fw.level(VM).rules[:] = [{"type": "group", "action": "web", "enable": 1}]
    ok(cli("cluster", "firewall", "group", "rename", "web", "front", "-y", "-p", "p1"))
    assert fw.level(VM).rules[0]["action"] == "front"
    r = cli("cluster", "firewall", "group", "remove", "front", "-y", "-p", "p1")
    assert r.exit_code == 1 and "VM 101, rule 0 (action)" in r.output
    fw.level(VM).rules.clear()
    r = ok(cli("cluster", "firewall", "group", "remove", "front", "-y", "-p", "p1"))
    assert "with its rule 0: IN ACCEPT HTTP" in r.output
    assert fw.groups == {}


# ── helpers ──────────────────────────────────────────────────────────────


def test_renamed_keeps_the_form_of_each_reference():
    old = {"dc/gw", "gw"}
    assert renamed("gw", old, "router") == "router"
    assert renamed("dc/gw", old, "router") == "dc/router"
    assert renamed("10.0.0.1,gw", old, "router") == "10.0.0.1,router"
    assert renamed("+dc/admins", {"+dc/admins", "+admins"}, "ops") == "+dc/ops"
    assert renamed("gateway", old, "router") == "gateway"


@pytest.mark.parametrize("method, path, section, digest", [
    ("POST", f"{DC}/rules", f"{DC}/rules", True),
    ("PUT", f"{VM}/rules/3", f"{VM}/rules", True),
    ("POST", f"{DC}/aliases", f"{DC}/aliases", False),
    ("DELETE", f"{DC}/aliases/gw", f"{DC}/aliases", True),
    ("POST", f"{DC}/ipset", f"{DC}/ipset", True),
    ("POST", f"{DC}/ipset/admins", f"{DC}/ipset/admins", False),
    ("DELETE", f"{DC}/ipset/admins", f"{DC}/ipset", False),
    ("DELETE", f"{DC}/ipset/admins/10.0.0.0/24", f"{DC}/ipset/admins", True),
    ("POST", f"{DC}/groups", f"{DC}/groups", True),
    ("POST", f"{DC}/groups/web", f"{DC}/groups/web", True),
    ("DELETE", f"{DC}/groups/web", f"{DC}/groups", False),
    ("PUT", f"{DC}/groups/web/0", f"{DC}/groups/web", True),
    ("PUT", f"{DC}/options", f"{DC}/options", True),
])
def test_the_list_and_the_digest_of_each_write(method, path, section, digest):
    assert section_of(method, path) == section
    assert takes_digest(method, path) is digest


def test_add_step_by_step_without_action(cli, fw, ui):
    fw.level(VM).rules[:] = [rule(macro="HTTP")]
    ui.menu("DROP", "in", "(none: protocol and ports)")
    ui.text("tcp", "23", "10.9.0.0/16", "", "no telnet")
    ui.confirm(True)
    ok(cli("vm", "firewall", "rule", "add", "101", "-p", "p2"))
    assert fw.level(VM).rules[0] == {"type": "in", "action": "DROP", "proto": "tcp", "dport": "23",
                                     "source": "10.9.0.0/16", "comment": "no telnet", "enable": 1}


def test_add_step_by_step_a_macro(cli, fw, ui):
    ui.menu("ACCEPT", "out", "SSH")
    ui.text("", "", "")
    ui.confirm(True)
    ok(cli("vm", "firewall", "rule", "add", "101", "-p", "p2"))
    assert fw.level(VM).rules[0] == {"type": "out", "action": "ACCEPT", "macro": "SSH", "enable": 1}
