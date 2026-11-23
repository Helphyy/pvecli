"""What the code review of the firewall found (2026-11-23), one test each.

pvecli talks to pve1 from 10.0.0.7, inside the cluster network
10.0.0.0/24 (see test_firewall_switch.py), unless a test moves it.
"""

import socket

import pytest
import yaml

import src.cli.firewall_guard as guard
from src.api.exceptions import APIError
from src.api.exceptions import PermissionError as PVEPermissionError
from tests.fake_firewall import FakeFirewall

DC = "/cluster/firewall"
NODE = "/nodes/pve1/firewall"
VM = "/nodes/pve1/qemu/101/firewall"
VM2 = "/nodes/pve1/qemu/102/firewall"
SSH = {"type": "in", "action": "ACCEPT", "macro": "SSH"}


@pytest.fixture
def fw(api, monkeypatch):
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}])
    api.route("GET", "/cluster/resources", [{"vmid": 101, "type": "qemu", "node": "pve1", "name": "a"},
                                            {"vmid": 102, "type": "qemu", "node": "pve1", "name": "b"}])
    api.route("GET", "/cluster/sdn/vnets", [])
    api.route("GET", "/cluster/status", [{"type": "node", "name": "pve1", "ip": "10.0.0.1"}])
    api.route("GET", "/nodes/pve1/network", [{"iface": "vmbr0", "cidr": "10.0.0.1/24"}])
    api.route("GET", "/version", {"version": "9.1"})
    monkeypatch.setattr(guard, "source_address", lambda host, port: "10.0.0.7")
    monkeypatch.setattr(guard, "APPLY_DELAY", 0)

    async def pve1(client, host):
        return "pve1"

    monkeypatch.setattr(guard, "api_node", pve1)
    f = FakeFirewall(api)
    f.level(DC).options["enable"] = 1
    return f


def write(tmp_path, doc):
    f = tmp_path / "fw.yaml"
    f.write_text(yaml.safe_dump({"pvecli_firewall": 1, **doc}, sort_keys=False))
    return str(f)


def ok(r):
    assert r.exit_code == 0, r.output
    return r


def outside(monkeypatch):
    monkeypatch.setattr(guard, "source_address", lambda host, port: "192.168.9.9")


# 1. import removing datacenter objects that levels outside the file use

def test_import_keeps_a_set_used_by_a_guest_left_out(cli, fw, tmp_path):
    fw.level(DC).ipsets["admins"] = {"comment": "", "entries": [{"cidr": "10.0.0.0/28"}]}
    fw.level(VM2).rules[:] = [{**SSH, "source": "+dc/admins", "enable": 1}]
    f = write(tmp_path, {"datacenter": {"options": {"enable": 1}}})
    r = cli("cluster", "firewall", "import", f, "-y", "-p", "p1")
    assert r.exit_code == 1 and "VM 102, rule 0 (source)" in r.output
    assert "admins" in fw.level(DC).ipsets
    ok(cli("cluster", "firewall", "import", f, "-f", "-y", "-p", "p1"))
    assert fw.level(DC).ipsets == {}


def test_import_keeps_a_group_a_node_left_out_inserts(cli, fw, tmp_path):
    fw.groups["web"] = {"comment": "", "rules": []}
    fw.level(NODE).rules[:] = [{"type": "group", "action": "web", "enable": 1}]
    r = cli("cluster", "firewall", "import", write(tmp_path, {"datacenter": {"options": {"enable": 1}}}),
            "-y", "-p", "p1")
    assert r.exit_code == 1 and "node pve1, rule 0 (action)" in r.output


def test_import_checks_group_rules_and_alias_entries_of_the_file(cli, fw, tmp_path):
    doc = {"datacenter": {"options": {"enable": 1},
                          "ipsets": [{"name": "s", "entries": [{"cidr": "nobody"}]}],
                          "rules": [{"type": "group", "action": "ghost"}]}}
    r = cli("cluster", "firewall", "import", write(tmp_path, doc), "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "no security group 'ghost'" in r.output and "IP set s: no alias 'nobody'" in r.output


# 2. a rule's dest is matched against the address pvecli connects to

def test_a_drop_on_the_address_pvecli_uses_is_refused(cli, fw, monkeypatch):
    monkeypatch.setattr(socket, "gethostbyname", lambda host: "192.168.10.5")
    r = cli("node", "firewall", "rule", "add", "pve1", "-ac", "DROP", "-pt", "tcp", "-dpt", "8006",
            "-dst", "192.168.10.5", "-y", "-p", "p1")
    assert r.exit_code == 1 and "closes the API" in r.output


# 3. unknown API node: every node is guarded

def test_an_unknown_api_node_guards_every_node(cli, fw, monkeypatch):
    async def unknown(client, host):
        return None

    monkeypatch.setattr(guard, "api_node", unknown)
    r = ok(cli("node", "firewall", "rule", "add", "pve1", "-ac", "ACCEPT", "-ma", "SSH", "-y", "-p", "p1"))
    assert "not checked before the change" in r.output and "A new connection to the API still works" in r.output


# 4. datacenter objects that decide the access are guarded

def test_changing_the_address_of_an_alias_in_management(cli, fw):
    fw.level(DC).aliases["me"] = {"cidr": "10.0.0.7"}
    fw.level(DC).ipsets["management"] = {"comment": "", "entries": [{"cidr": "me"}]}
    r = cli("cluster", "firewall", "alias", "edit", "me", "--cidr", "10.0.0.99", "-y", "-p", "p1")
    assert r.exit_code == 1 and "not in the 'management' IP set" in r.output
    assert fw.level(DC).aliases["me"]["cidr"] == "10.0.0.7"


def test_renaming_management_from_outside_the_cluster_network(cli, fw, monkeypatch):
    outside(monkeypatch)
    fw.level(DC).ipsets["management"] = {"comment": "", "entries": [{"cidr": "192.168.9.9"}]}
    r = cli("cluster", "firewall", "ipset", "edit", "management", "--rename", "old", "-y", "-p", "p1")
    assert r.exit_code == 1 and "outside the cluster network" in r.output
    assert "management" in fw.level(DC).ipsets


def test_adding_this_machine_to_a_set_a_drop_rule_uses(cli, fw):
    fw.level(DC).ipsets["blocked"] = {"comment": "", "entries": []}
    fw.level(NODE).rules[:] = [{"type": "in", "action": "DROP", "source": "+blocked", "enable": 1}]
    r = cli("cluster", "firewall", "ipset", "add", "blocked", "10.0.0.7", "-y", "-p", "p1")
    assert r.exit_code == 1 and "drops 10.0.0.7" in r.output


def test_forced_removal_of_an_alias_in_management(cli, fw):
    fw.level(DC).aliases["me"] = {"cidr": "10.0.0.7"}
    fw.level(DC).ipsets["management"] = {"comment": "", "entries": [{"cidr": "me"}]}
    r = cli("cluster", "firewall", "alias", "remove", "me", "-f", "-y", "-p", "p1")
    # --force passes the use check and the access check: both are said
    assert "--force: going on" in r.output


# 5. a node turned off by an import goes first

def test_a_node_turned_off_goes_first(cli, fw, tmp_path, api):
    fw.level(NODE).rules[:] = [{**SSH, "enable": 1}]
    start = len(api.calls)
    ok(cli("cluster", "firewall", "import", write(tmp_path, {"nodes": {"pve1": {"options": {"enable": 0}}}}),
           "-y", "-p", "p1"))
    writes = [c.path for c in api.calls[start:] if c.method != "GET"]
    assert writes[0] == f"{NODE}/options"


# 6. an import stopped half way checks the access

def test_an_import_stopped_half_way_checks_the_access(cli, fw, tmp_path, api):
    calls = {"n": 0}

    def flaky(call):
        calls["n"] += 1
        if calls["n"] == 2:
            raise APIError("something went wrong")
        return fw.handle(call)

    api.route("POST", "/cluster/firewall/{a}", flaky)
    doc = {"datacenter": {"options": {"enable": 1}, "rules": [SSH, {**SSH, "macro": "HTTP"}]}}
    r = cli("cluster", "firewall", "import", write(tmp_path, doc), "-y", "-p", "p1")
    assert r.exit_code == 1 and "Stopped at" in r.output
    assert "Checking the API is still reachable" in r.output


# 7. an import with a token that cannot read the cluster status

def test_import_without_the_cluster_status(cli, fw, tmp_path, monkeypatch):
    async def denied(client, host):
        raise PVEPermissionError("Permission check failed (/, Sys.Audit)")

    monkeypatch.setattr(guard, "api_node", denied)
    r = ok(cli("cluster", "firewall", "import", write(tmp_path, {"datacenter": {"options": {"enable": 1},
                                                                                "rules": [SSH]}}),
               "-y", "-p", "p1"))
    assert "not checked before the change" in r.output


# 8. a rename leaves the guest's own object of that name alone

def test_rename_follows_dc_but_not_the_guest_own_alias(cli, fw):
    fw.level(DC).aliases["web"] = {"cidr": "10.0.0.1"}
    fw.level(VM).aliases["web"] = {"cidr": "10.1.0.1"}
    fw.level(VM).rules[:] = [{**SSH, "source": "dc/web,web", "enable": 1}]
    ok(cli("cluster", "firewall", "alias", "edit", "web", "--rename", "front", "-y", "-p", "p1"))
    assert fw.level(VM).rules[0]["source"] == "dc/front,web"


# 9. YAML booleans and log: nolog are what Proxmox stores

def test_yaml_booleans_and_nolog_match_on_the_second_import(cli, fw, tmp_path):
    doc = {"datacenter": {"options": {"enable": True, "ebtables": False},
                          "rules": [{**SSH, "log": "nolog", "enable": True}]}}
    f = write(tmp_path, doc)
    ok(cli("cluster", "firewall", "import", f, "-y", "-p", "p1"))
    assert fw.level(DC).options == {"enable": 1, "ebtables": 0}
    assert "already matches" in ok(cli("cluster", "firewall", "import", f, "-y", "-p", "p1")).output


# 10. nothing promises an undo

def test_no_undo_is_promised(cli, fw):
    for args in (("cluster", "firewall", "enable"), ("cluster", "firewall", "set"), ("node", "firewall", "set")):
        assert "undone" not in cli(*args, "-h").output
