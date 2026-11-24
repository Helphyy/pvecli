"""enable, disable and set, and the guard of the access to the API.

pvecli talks to pve1 (10.0.0.1) from 10.0.0.7, inside the cluster network
10.0.0.0/24, unless a test moves it. The new connection keep_access opens
is replaced by one that works or fails as the test says.
"""

import pytest

import src.cli.firewall_guard as guard
from src.api.exceptions import NetworkError
from tests.fake_firewall import FakeFirewall

DC = "/cluster/firewall"
VM = "/nodes/pve1/qemu/101/firewall"


@pytest.fixture
def fw(api, monkeypatch):
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}])
    api.route("GET", "/cluster/status", [{"type": "node", "name": "pve1", "ip": "10.0.0.1"}])
    monkeypatch.setattr(guard, "api_node", lambda client, host: _pve1())
    api.route("GET", "/nodes/pve1/network", [{"iface": "vmbr0", "cidr": "10.0.0.1/24"}])
    api.route("GET", "/cluster/resources", [{"vmid": 101, "type": "qemu", "node": "pve1", "name": "web",
                                             "status": "running"}])
    api.route("GET", "/nodes/pve1/qemu/101/config",
              {"net0": "virtio=AA,bridge=vmbr0,firewall=1", "net1": "virtio=BB,bridge=vmbr1", "digest": "c"})
    api.route("GET", "/nodes/pve1/qemu/101/status/current", {"status": "running"})
    api.route("PUT", "/nodes/pve1/qemu/101/config", None)
    api.route("GET", "/version", {"version": "9.1"})
    monkeypatch.setattr(guard, "source_address", lambda host, port: "10.0.0.7")
    monkeypatch.setattr(guard, "APPLY_DELAY", 0)
    return FakeFirewall(api)


@pytest.fixture
def api_lost(monkeypatch):
    """New connections to the API fail: the change closed it."""

    class Closed(guard.ProxmoxClient):
        async def get(self, *a, **k):
            raise NetworkError("Connection refused to h1:8006")

    monkeypatch.setattr(guard, "ProxmoxClient", Closed)


async def _pve1():
    return "pve1"


def ok(r):
    assert r.exit_code == 0, r.output
    return r


def test_enable_the_datacenter_from_a_management_host(cli, fw):
    r = ok(cli("cluster", "firewall", "enable", "-y", "-p", "p1"))
    assert fw.level(DC).options == {"enable": 1}
    assert "Access kept" in r.output and "cluster network 10.0.0.0/24" in r.output
    assert "A new connection to the API still works" in r.output


def test_enable_refused_when_it_closes_the_api(cli, fw, monkeypatch):
    monkeypatch.setattr(guard, "source_address", lambda host, port: "192.168.9.9")
    r = cli("cluster", "firewall", "enable", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "closes the API to this machine" in r.output
    assert "ipset add management" in r.output
    assert fw.level(DC).options == {}


def test_forced_enable_says_how_to_get_back_in(cli, fw, monkeypatch, api_lost):
    monkeypatch.setattr(guard, "source_address", lambda host, port: "192.168.9.9")
    r = cli("cluster", "firewall", "enable", "--force", "-y", "-p", "p1")
    assert "--force: going on" in r.output
    assert "A new connection to the API fails" in r.output and "pve-firewall stop" in r.output
    # Nothing is undone: Proxmox closes the open connections it now drops
    assert fw.level(DC).options == {"enable": 1}


def test_a_policy_change_is_guarded_too(cli, fw, monkeypatch):
    fw.level(DC).options.update({"enable": 1, "policy_in": "ACCEPT"})
    monkeypatch.setattr(guard, "source_address", lambda host, port: "192.168.9.9")
    r = cli("cluster", "firewall", "set", "policy_in=DROP", "-y", "-p", "p1")
    assert r.exit_code == 1 and "closes the API" in r.output
    assert fw.level(DC).options["policy_in"] == "ACCEPT"


def test_creating_management_without_this_machine_is_refused(cli, fw):
    fw.level(DC).options["enable"] = 1
    r = cli("cluster", "firewall", "ipset", "add", "management", "10.9.0.0/16", "-y", "-p", "p1")
    assert r.exit_code == 1 and "not in the 'management' IP set" in r.output
    assert fw.level(DC).ipsets == {}
    ok(cli("cluster", "firewall", "ipset", "add", "management", "10.0.0.0/28", "-y", "-p", "p1"))
    assert "management" in fw.level(DC).ipsets


def test_removing_this_machine_from_management(cli, fw):
    fw.level(DC).options["enable"] = 1
    fw.level(DC).ipsets["management"] = {"comment": "", "entries": [{"cidr": "10.0.0.7"}, {"cidr": "10.0.0.8"}]}
    r = cli("cluster", "firewall", "ipset", "remove", "management", "10.0.0.7", "-y", "-p", "p1")
    assert r.exit_code == 1
    ok(cli("cluster", "firewall", "ipset", "remove", "management", "10.0.0.8", "-y", "-p", "p1"))
    assert fw.level(DC).ipsets["management"]["entries"] == [{"cidr": "10.0.0.7"}]


def test_a_node_rule_that_drops_this_machine_is_refused(cli, fw):
    fw.level(DC).options["enable"] = 1
    r = cli("node", "firewall", "rule", "add", "pve1", "-ac", "DROP", "-pt", "tcp", "-dpt", "8006",
            "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "rule 0 of the node drops 10.0.0.7, before the management hosts" in r.output
    assert "a narrower source" in r.output and "ipset add management" not in r.output
    assert fw.level("/nodes/pve1/firewall").rules == []


def test_removing_the_accept_this_machine_relies_on_is_refused(cli, fw):
    fw.level(DC).options["enable"] = 1
    fw.level(DC).ipsets["management"] = {"comment": "", "entries": []}
    fw.level(DC).rules[:] = [{"type": "in", "action": "ACCEPT", "proto": "tcp", "dport": "8006", "enable": 1}]
    r = cli("cluster", "firewall", "rule", "remove", "0", "-y", "-p", "p1")
    assert r.exit_code == 1 and "not in the 'management' IP set" in r.output
    r = cli("cluster", "firewall", "rule", "disable", "0", "-y", "-p", "p1")
    assert r.exit_code == 1


def test_a_rule_of_a_guest_is_not_guarded(cli, fw):
    fw.level(DC).options["enable"] = 1
    r = ok(cli("vm", "firewall", "rule", "add", "101", "-ac", "DROP", "-y", "-p", "p2"))
    assert "Access kept" not in r.output and "Checking the API" not in r.output


def test_no_guard_when_the_datacenter_is_off(cli, fw):
    r = ok(cli("node", "firewall", "disable", "pve1", "-y", "-p", "p1"))
    assert "Checking the API" not in r.output
    assert fw.level("/nodes/pve1/firewall").options == {"enable": 0}


def test_a_guest_says_which_cards_are_not_filtered(cli, fw, api):
    r = ok(cli("vm", "firewall", "enable", "101", "-y", "-p", "p2"))
    assert fw.level(VM).options == {"enable": 1}
    assert "Not filtered: net1" in r.output
    assert api.called("PUT", "/nodes/pve1/qemu/101/config") == []


def test_a_guest_flags_its_cards_with_nics(cli, fw, api):
    r = ok(cli("vm", "firewall", "enable", "101", "--nics", "-y", "-p", "p2"))
    assert "its network drops for a moment" in r.output
    put = api.called("PUT", "/nodes/pve1/qemu/101/config")[0]
    assert put.data == {"net1": "virtio=BB,bridge=vmbr1,firewall=1", "digest": "c"}


def test_already_on(cli, fw):
    fw.level(VM).options["enable"] = 1
    r = ok(cli("vm", "firewall", "enable", "101", "-y", "-p", "p2"))
    assert "already on" in r.output


@pytest.mark.parametrize("pairs, error", [
    (["policy_in=MAYBE"], "policy_in takes one of ACCEPT, REJECT, DROP"),
    (["nosmurfs=1"], "Not an option of this level: 'nosmurfs=1'"),
    (["macfilter=perhaps"], "macfilter takes yes or no"),
    ([], "Nothing to change"),
])
def test_set_checks_each_value(cli, fw, pairs, error):
    r = cli("vm", "firewall", "set", "101", *pairs, "-y", "-p", "p2")
    assert r.exit_code == 1 and error in r.output


def test_set_and_clear_on_a_guest(cli, fw):
    fw.level(VM).options.update({"policy_in": "ACCEPT", "dhcp": 1})
    ok(cli("vm", "firewall", "set", "101", "policy_in=drop", "macfilter=no", "--clear", "dhcp", "-y", "-p", "p2"))
    assert fw.level(VM).options == {"policy_in": "DROP", "macfilter": 0}


def test_set_a_node_number(cli, fw):
    ok(cli("node", "firewall", "set", "pve1", "nf_conntrack_max=524288", "tcpflags=yes", "-y", "-p", "p1"))
    assert fw.level("/nodes/pve1/firewall").options == {"nf_conntrack_max": 524288, "tcpflags": 1}


def test_enable_with_nics_asks_once(cli, fw, api):
    r = ok(cli("vm", "firewall", "enable", "101", "--nics", "-p", "p2", input="y\n"))
    assert r.output.count("Apply?") == 1
    assert "firewall: off -> on" in r.output and "net1: firewall=1" in r.output
    assert fw.level(VM).options == {"enable": 1}
    assert api.called("PUT", "/nodes/pve1/qemu/101/config")[0].data["net1"].endswith("firewall=1")
