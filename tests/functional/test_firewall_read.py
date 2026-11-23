"""The read side of the firewall: status, rules, options, objects, log, export.

PVE endpoints: GET .../firewall/{options,rules,aliases,ipset,ipset/{name},log}
under /cluster, /nodes/{node}, /nodes/{node}/{qemu,lxc}/{vmid} and
/cluster/sdn/vnets/{vnet}; /cluster/firewall/{groups,groups/{group},macros,refs}.
"""

import json

import pytest
import yaml

from tests.fake_api import FakeAPI

NODES = [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"}]
GUESTS = [
    {"vmid": 101, "type": "qemu", "node": "pve1", "name": "web", "status": "running"},
    {"vmid": 102, "type": "lxc", "node": "pve2", "name": "db", "status": "running"},
    {"vmid": 900, "type": "qemu", "node": "pve1", "name": "tpl", "template": 1},
]
CONFIGS = {
    "qemu/101": {"net0": "virtio=AA,bridge=vmbr0,firewall=1", "net1": "virtio=BB,bridge=vmbr1"},
    "lxc/102": {"net0": "name=eth0,bridge=vmbr0,ip=dhcp"},
}
SSH = {"pos": 0, "type": "in", "action": "ACCEPT", "macro": "SSH", "enable": 1, "ipversion": 4}
WEB = {"pos": 1, "type": "in", "action": "ACCEPT", "proto": "tcp", "dport": "443", "enable": 1,
       "comment": "https"}
DROP_OFF = {"pos": 2, "type": "in", "action": "DROP", "source": "10.9.0.0/16", "enable": 0}
USE_WEB = {"pos": 0, "type": "group", "action": "web", "enable": 1}

FIREWALLS = {
    "/cluster/firewall": {
        "options": {"enable": 1, "policy_in": "DROP", "digest": "d0"},
        "rules": [WEB, SSH],
        "aliases": [{"name": "gw", "cidr": "10.0.0.1", "comment": "router", "digest": "a"}],
        "ipset": [{"name": "management", "comment": "admins", "digest": "i"}],
        "ipset/management": [{"cidr": "10.0.0.0/24", "digest": "e"},
                             {"cidr": "10.0.0.66", "nomatch": 1, "comment": "kiosk", "digest": "e"}],
    },
    "/nodes/pve1/firewall": {"options": {"nftables": 1}, "rules": []},
    "/nodes/pve2/firewall": {"options": {"enable": 0}, "rules": [SSH]},
    "/nodes/pve1/qemu/101/firewall": {
        "options": {"enable": 1, "policy_in": "DROP"}, "rules": [USE_WEB, DROP_OFF],
        "aliases": [], "ipset": [],
    },
    "/nodes/pve2/lxc/102/firewall": {
        "options": {"enable": 1}, "rules": [SSH], "aliases": [], "ipset": [],
    },
    "/cluster/sdn/vnets/v1/firewall": {"options": {"enable": 1}, "rules": [
        {"pos": 0, "type": "forward", "action": "ACCEPT", "proto": "icmp", "enable": 1}]},
}
GROUPS = {"web": [{**WEB, "pos": 0}, {**SSH, "pos": 1}]}


def _firewall(call):
    path = call.path
    for base, parts in FIREWALLS.items():
        if path.startswith(base + "/"):
            key = path[len(base) + 1:]
            if key in parts:
                return parts[key]
            if key.startswith("ipset/"):
                return []
            return [] if key != "options" else {}
    raise AssertionError(f"unrouted {path}")


@pytest.fixture
def fw(api: FakeAPI) -> FakeAPI:
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/cluster/resources", GUESTS)
    # /nodes/pve1/qemu/101/config -> CONFIGS["qemu/101"]
    api.route("GET", "/nodes/{node}/{kind}/{vmid}/config", lambda c: CONFIGS["/".join(c.path.split("/")[3:5])])
    api.route("GET", "/cluster/sdn/vnets", [{"vnet": "v1", "zone": "z1"}])
    for pattern in (
        "/cluster/firewall/{a}", "/cluster/firewall/{a}/{b}",
        "/nodes/{n}/firewall/{a}", "/nodes/{n}/{k}/{v}/firewall/{a}", "/nodes/{n}/{k}/{v}/firewall/{a}/{b}",
        "/cluster/sdn/vnets/{v}/firewall/{a}",
    ):
        api.route("GET", pattern, _firewall)
    api.route("GET", "/cluster/firewall/groups", [{"group": "web", "comment": "front", "digest": "g"}])
    api.route("GET", "/cluster/firewall/groups/{g}", lambda c: GROUPS[c.path.rsplit("/", 1)[1]])
    api.route("GET", "/cluster/firewall/macros", [{"macro": "SSH", "descr": "Secure shell"},
                                                  {"macro": "HTTPS", "descr": "Web over TLS"}])
    api.route("GET", "/cluster/firewall/refs", [{"type": "ipset", "name": "management", "ref": "+management",
                                                 "scope": "dc", "comment": "admins"}])
    return api


def js(r):
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


# ── status ───────────────────────────────────────────────────────────────


def test_vm_status_names_the_unfiltered_card(cli, fw):
    st = js(cli("vm", "firewall", "status", "101", "-p", "p1", "--json"))
    assert st["enforced"] is True
    assert st["nics"] == {"net0": True, "net1": False}
    assert st["rules"] == 2 and st["rules_enabled"] == 1
    assert st["issues"] == ["not filtered: net1 (no firewall=1)"]


def test_ct_status_on_but_no_card_flagged(cli, fw):
    r = cli("ct", "firewall", "status", "102", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Enforced:" in r.output and "no" in r.output
    assert "no network card has firewall=1" in r.output


def test_node_status_shows_the_backend_and_a_node_off(cli, fw):
    st = js(cli("node", "firewall", "status", "pve2", "-p", "p1", "--json"))
    assert st["nftables"] is False and st["enabled"] is False
    assert "the firewall of this node is off" in st["issues"][0]
    assert js(cli("node", "firewall", "status", "pve1", "-p", "p1", "--json"))["nftables"] is True


def test_vnet_status_wants_nftables_on_every_node(cli, fw):
    st = js(cli("sdn", "vnet", "firewall", "status", "v1", "-p", "p1", "--json"))
    assert st["issues"] == ["vnet rules need the nftables firewall: on 1 of 2 node(s) only"]


def test_cluster_status_covers_every_level_but_templates(cli, fw):
    view = js(cli("cluster", "firewall", "status", "-p", "p1", "--json"))
    assert view["datacenter"]["enabled"] is True
    levels = [(lv["level"], lv["target"]) for lv in view["levels"]]
    assert levels == [("node", "pve1"), ("node", "pve2"), ("vm", 101), ("ct", 102), ("vnet", "v1")]


def test_cluster_status_table(cli, fw):
    r = cli("cluster", "firewall", "status", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "101 web" in r.output and "102 db" in r.output
    assert "1/2" in r.output  # the cards of VM 101


def test_datacenter_off_is_said_once_above_the_table(cli, fw):
    fw.route("GET", "/cluster/firewall/options", {"enable": 0})
    r = cli("cluster", "firewall", "status", "-p", "p1")
    assert r.output.count("datacenter firewall is off") == 1


def test_status_runs_on_several_profiles(cli, fw):
    for host, name in (("h1", "c1"), ("h2", "c2")):
        fw.route("GET", "/cluster/status", [{"type": "cluster", "name": name, "id": "cluster"}], host=host)
    r = cli("vm", "firewall", "status", "101", "-p", "p1,p2")
    assert r.exit_code == 0, r.output
    assert r.output.count("Firewall of VM 101") == 2


def test_status_asks_which_vm_when_omitted(cli, fw, ui):
    fw.route("GET", "/nodes/{node}/qemu", [g for g in GUESTS if g["type"] == "qemu"])
    ui.menu("101")
    r = cli("vm", "firewall", "status", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Firewall of VM 101" in r.output


def test_unknown_node_is_an_error(cli, fw):
    r = cli("node", "firewall", "status", "pve9", "-p", "p1")
    assert r.exit_code == 1
    assert "Node 'pve9' not found" in r.output


def test_a_vm_id_must_be_a_number(cli, fw):
    r = cli("vm", "firewall", "status", "web", "-p", "p1")
    assert r.exit_code == 1
    assert "Not a VM ID: web" in r.output


# ── rules, options, objects ──────────────────────────────────────────────


def test_rule_list_in_evaluation_order(cli, fw):
    rules = js(cli("cluster", "firewall", "rule", "list", "-p", "p1", "--json"))
    assert [r["pos"] for r in rules] == [0, 1]
    r = cli("vm", "firewall", "rule", "list", "101", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "web" in r.output and "10.9.0.0/16" in r.output


def test_rule_list_of_a_security_group(cli, fw):
    rules = js(cli("cluster", "firewall", "rule", "list", "--group", "web", "-p", "p1", "--json"))
    assert [r.get("macro") or r.get("dport") for r in rules] == ["443", "SSH"]
    assert fw.called("GET", "/cluster/firewall/groups/web")


def test_group_is_only_an_option_of_the_datacenter(cli, fw):
    r = cli("vm", "firewall", "rule", "list", "101", "--group", "web", "-p", "p1")
    assert r.exit_code == 2


def test_empty_rule_list(cli, fw):
    r = cli("node", "firewall", "rule", "list", "pve1", "-p", "p1")
    assert "No firewall rule on node pve1" in r.output


def test_options_fill_in_the_defaults(cli, fw):
    opts = js(cli("vm", "firewall", "options", "101", "-p", "p1", "--json"))
    assert opts == {"enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT"}
    r = cli("cluster", "firewall", "options", "-p", "p1")
    assert "ACCEPT (default)" in r.output
    assert "digest" not in r.output


def test_ipsets_with_their_entries(cli, fw):
    sets = js(cli("cluster", "firewall", "ipset", "list", "-p", "p1", "--json"))
    assert sets[0]["name"] == "management"
    assert [e["cidr"] for e in sets[0]["entries"]] == ["10.0.0.0/24", "10.0.0.66"]
    r = cli("cluster", "firewall", "ipset", "list", "-p", "p1")
    assert "!10.0.0.66" in r.output  # nomatch


def test_aliases(cli, fw):
    assert js(cli("cluster", "firewall", "alias", "list", "-p", "p1", "--json")) == [
        {"name": "gw", "cidr": "10.0.0.1", "comment": "router"}]
    assert "No alias on VM 101" in cli("vm", "firewall", "alias", "list", "101", "-p", "p1").output


def test_nodes_and_vnets_have_no_ipset_nor_alias(cli, fw):
    assert cli("node", "firewall", "ipset", "list", "pve1", "-p", "p1").exit_code == 2
    assert cli("sdn", "vnet", "firewall", "alias", "list", "v1", "-p", "p1").exit_code == 2


def test_groups_and_where_they_are_used(cli, fw):
    groups = js(cli("cluster", "firewall", "group", "list", "-p", "p1", "--json"))
    assert groups == [{"group": "web", "comment": "front", "rules": 2}]
    info = js(cli("cluster", "firewall", "group", "info", "web", "-p", "p1", "--json"))
    assert info["used_by"] == ["VM 101"]


def test_macros_and_refs(cli, fw):
    assert "Secure shell" in cli("cluster", "firewall", "macros", "-p", "p1").output
    assert "+management" in cli("cluster", "firewall", "refs", "-p", "p1").output


def test_log_of_a_guest(cli, fw):
    fw.route("GET", "/nodes/{n}/{k}/{v}/firewall/log",
             [{"n": 2, "t": "101 6 tap101i0-IN 23/Nov DROP: IN=fwbr101i0 SRC=10.9.0.4"},
              {"n": 1, "t": "101 6 tap101i0-IN 23/Nov ACCEPT: IN=fwbr101i0 SRC=10.0.0.4"}])
    r = cli("vm", "firewall", "log", "101", "-l", "2", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert r.output.index("ACCEPT") < r.output.index("DROP")
    assert fw.called("GET", "/nodes/pve1/qemu/101/firewall/log")[0].params == {"limit": 2}


def test_the_datacenter_has_no_log(cli, fw):
    assert cli("cluster", "firewall", "log", "-p", "p1").exit_code == 2


# ── export ───────────────────────────────────────────────────────────────


def test_export_keeps_what_import_needs_and_nothing_else(cli, fw):
    doc = yaml.safe_load(cli("cluster", "firewall", "export", "-p", "p1").stdout)
    dc = doc["datacenter"]
    assert dc["options"] == {"enable": 1, "policy_in": "DROP"}
    # In evaluation order, without pos nor ipversion
    assert dc["rules"] == [{"type": "in", "action": "ACCEPT", "macro": "SSH", "enable": 1},
                           {k: v for k, v in WEB.items() if k != "pos"}]
    assert "ipversion" not in json.dumps(doc) and "digest" not in json.dumps(doc)
    assert dc["groups"] == [{"name": "web", "comment": "front",
                             "rules": [{k: v for k, v in r.items() if k not in ("pos", "ipversion")}
                                       for r in GROUPS["web"]]}]
    assert dc["ipsets"][0]["entries"][1] == {"cidr": "10.0.0.66", "nomatch": 1, "comment": "kiosk"}
    # A node with nothing set is left out; the template too
    assert set(doc["nodes"]) == {"pve1", "pve2"}
    assert set(doc["guests"]) == {101, 102}
    assert doc["guests"][101]["type"] == "vm" and doc["guests"][102]["type"] == "ct"
    assert doc["vnets"]["v1"]["options"] == {"enable": 1}


def test_export_to_a_file(cli, fw, tmp_path):
    out = tmp_path / "fw.yaml"
    r = cli("cluster", "firewall", "export", "-ou", str(out), "-p", "p1")
    assert r.exit_code == 0, r.output
    assert yaml.safe_load(out.read_text())["pvecli_firewall"] == 1


def test_export_without_vnet_firewall_on_older_pve(cli, fw):
    fw.route("GET", "/cluster/sdn/vnets/{v}/firewall/{a}", FakeAPI.not_implemented())
    doc = yaml.safe_load(cli("cluster", "firewall", "export", "-p", "p1").stdout)
    assert doc["vnets"] == {}


# ── vm info / ct info ────────────────────────────────────────────────────


def test_info_line_when_unreadable_is_left_out():
    import asyncio

    from src.api.exceptions import PermissionError as PVEPermissionError
    from src.cli.firewall import info_line

    class Client:
        async def get(self, *a, **k):
            raise PVEPermissionError("Permission check failed (/, Sys.Audit)")

    assert asyncio.run(info_line(Client(), "vm", "pve1", 101)) is None
