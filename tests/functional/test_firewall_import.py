"""`cluster firewall import`: the firewall ends up as the file says.

Against tests/fake_firewall.py (PVE 9.1 behaviour). An import twice in a
row changes nothing the second time; a level the file leaves out is not
touched.
"""

import pytest
import yaml

import src.cli.firewall_guard as guard
from src.cli.firewall_import import check_refs, rule_edits
from tests.fake_firewall import FakeFirewall

DC = "/cluster/firewall"
VM = "/nodes/pve1/qemu/101/firewall"
NODE = "/nodes/pve1/firewall"

SSH = {"type": "in", "action": "ACCEPT", "macro": "SSH"}
WEB = {"type": "in", "action": "ACCEPT", "proto": "tcp", "dport": "443", "source": "+admins"}
TELNET = {"type": "in", "action": "DROP", "proto": "tcp", "dport": "23"}


@pytest.fixture
def fw(api, monkeypatch):
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}])
    api.route("GET", "/cluster/resources", [{"vmid": 101, "type": "qemu", "node": "pve1", "name": "web"}])
    api.route("GET", "/cluster/sdn/vnets", [])
    api.route("GET", "/cluster/status", [{"type": "node", "name": "pve1", "ip": "10.0.0.1"}])
    api.route("GET", "/nodes/pve1/network", [{"iface": "vmbr0", "cidr": "10.0.0.1/24"}])
    api.route("GET", "/version", {"version": "9.1"})
    monkeypatch.setattr(guard, "source_address", lambda host, port: "10.0.0.7")
    monkeypatch.setattr(guard, "APPLY_DELAY", 0)

    async def pve1(client, host):
        return "pve1"

    monkeypatch.setattr(guard, "api_node", pve1)
    return FakeFirewall(api)


def write(tmp_path, doc):
    f = tmp_path / "fw.yaml"
    f.write_text(yaml.safe_dump({"pvecli_firewall": 1, **doc}, sort_keys=False))
    return str(f)


def ok(r):
    assert r.exit_code == 0, r.output
    return r


def plain(rules):
    return [{k: v for k, v in r.items() if k != "enable"} for r in rules]


FULL = {
    "datacenter": {
        "options": {"enable": 1, "policy_in": "DROP"},
        "aliases": [{"name": "gw", "cidr": "10.0.0.254"}],
        "ipsets": [{"name": "admins", "entries": [{"cidr": "10.0.0.0/28"}, {"cidr": "gw"}]},
                   {"name": "management", "entries": [{"cidr": "10.0.0.0/24"}]}],
        "groups": [{"name": "web", "rules": [WEB]}],
        "rules": [SSH],
    },
    "guests": {101: {"type": "vm", "options": {"enable": 1}, "rules": [{"type": "group", "action": "web"}, TELNET]}},
}


def test_import_builds_the_whole_firewall(cli, fw, tmp_path):
    ok(cli("cluster", "firewall", "import", write(tmp_path, FULL), "-y", "-p", "p1"))
    dc = fw.level(DC)
    assert dc.options == {"enable": 1, "policy_in": "DROP"}
    assert dc.aliases == {"gw": {"cidr": "10.0.0.254"}}
    assert [e["cidr"] for e in dc.ipsets["admins"]["entries"]] == ["10.0.0.0/28", "gw"]
    assert plain(fw.groups["web"]["rules"]) == [WEB]
    assert plain(dc.rules) == [SSH]
    assert plain(fw.level(VM).rules) == [{"type": "group", "action": "web"}, TELNET]
    assert fw.level(VM).options == {"enable": 1}


def test_a_second_import_changes_nothing(cli, fw, tmp_path, api):
    f = write(tmp_path, FULL)
    ok(cli("cluster", "firewall", "import", f, "-y", "-p", "p1"))
    before = len(api.writes())
    r = ok(cli("cluster", "firewall", "import", f, "-y", "-p", "p1"))
    assert "already matches" in r.output
    assert len(api.writes()) == before


def test_rules_in_common_stay_and_the_rest_moves_in(cli, fw, tmp_path, api):
    fw.level(VM).rules[:] = [{**SSH, "enable": 1}, {**TELNET, "enable": 1}]
    target = {"type": "in", "action": "ACCEPT", "macro": "HTTPS"}
    ok(cli("cluster", "firewall", "import", write(tmp_path, {"guests": {101: {"rules": [target, SSH]}}}),
           "-y", "-p", "p1"))
    assert plain(fw.level(VM).rules) == [target, SSH]
    # SSH was kept in place: one removal (TELNET), one addition (HTTPS)
    assert len(api.called("DELETE", f"{VM}/rules/{{p}}")) == 1


def test_objects_go_only_after_the_rules_that_used_them(cli, fw, tmp_path, api):
    fw.level(DC).aliases["old"] = {"cidr": "10.0.0.9"}
    fw.level(DC).rules[:] = [{"type": "in", "action": "ACCEPT", "source": "old", "enable": 1}]
    ok(cli("cluster", "firewall", "import", write(tmp_path, {"datacenter": {"rules": [SSH]}}), "-y", "-p", "p1"))
    assert fw.level(DC).aliases == {} and plain(fw.level(DC).rules) == [SSH]
    paths = [c.path for c in api.writes()]
    assert paths.index(f"{DC}/rules/0") < paths.index(f"{DC}/aliases/old")


def test_a_level_left_out_is_not_touched(cli, fw, tmp_path):
    fw.level(NODE).rules[:] = [{**TELNET, "enable": 1}]
    ok(cli("cluster", "firewall", "import", write(tmp_path, {"guests": {101: {"rules": [SSH]}}}), "-y", "-p", "p1"))
    assert plain(fw.level(NODE).rules) == [TELNET]


def test_options_the_file_leaves_out_go_back_to_default(cli, fw, tmp_path):
    fw.level(VM).options.update({"enable": 1, "dhcp": 1})
    ok(cli("cluster", "firewall", "import", write(tmp_path, {"guests": {101: {"options": {"enable": 1}}}}),
           "-y", "-p", "p1"))
    assert fw.level(VM).options == {"enable": 1}


def test_ipset_entries_are_added_changed_and_removed(cli, fw, tmp_path):
    fw.level(DC).ipsets["admins"] = {"comment": "", "entries": [{"cidr": "10.0.0.1"}, {"cidr": "10.0.0.2"}]}
    doc = {"datacenter": {"ipsets": [{"name": "admins", "entries": [
        {"cidr": "10.0.0.1", "comment": "boss"}, {"cidr": "10.0.0.3"}]}]}}
    ok(cli("cluster", "firewall", "import", write(tmp_path, doc), "-y", "-p", "p1"))
    assert fw.level(DC).ipsets["admins"]["entries"] == [{"cidr": "10.0.0.1", "comment": "boss"}, {"cidr": "10.0.0.3"}]


def test_the_plan_is_shown_and_declined(cli, fw, tmp_path, ui, api):
    ui.confirm(False)
    r = ok(cli("cluster", "firewall", "import", write(tmp_path, FULL), "-p", "p1"))
    assert "datacenter: create alias gw = 10.0.0.254" in r.output
    assert "VM 101: add rule at 1: IN DROP tcp dport 23" in r.output
    assert [c for c in api.writes() if "firewall" in c.path] == []


@pytest.mark.parametrize("doc, error", [
    ({"guests": {999: {"rules": [SSH]}}}, "999 is not a VM or container"),
    ({"nodes": {"pve9": {}}}, "'pve9' is not a node"),
    ({"guests": {101: {"type": "ct"}}}, "101 is a VM, the file says ct"),
    ({"datacenter": {"rules": [{"type": "in"}]}}, "a rule needs a type and an action"),
    ({"datacenter": {"options": {"nosmurfs": 1}}}, "unknown option(s) nosmurfs"),
    ({"guests": {101: {"rules": [{**SSH, "source": "+ghost"}]}}}, "no IP set 'ghost'"),
    ({"datacenter": {"rules": [{**SSH, "source": "dc/nobody"}]}}, "no alias 'nobody'"),
    ({"firewall": {}}, "Unknown key(s) at the top"),
])
def test_a_wrong_file_changes_nothing(cli, fw, tmp_path, api, doc, error):
    r = cli("cluster", "firewall", "import", write(tmp_path, doc), "-y", "-p", "p1")
    assert r.exit_code == 1 and error in r.output, r.output
    assert [c for c in api.writes() if "firewall" in c.path] == []


def test_not_a_firewall_file(cli, fw, tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("guests: []\n")
    r = cli("cluster", "firewall", "import", str(f), "-y", "-p", "p1")
    assert r.exit_code == 1 and "pvecli_firewall: 1" in r.output


def test_an_import_that_closes_the_api_is_refused(cli, fw, tmp_path, monkeypatch):
    doc = {"datacenter": {"options": {"enable": 1}, "ipsets": [{"name": "management", "entries": [{"cidr": "10.9.0.0/16"}]}]}}
    r = cli("cluster", "firewall", "import", write(tmp_path, doc), "-y", "-p", "p1")
    assert r.exit_code == 1 and "not in the 'management' IP set" in r.output
    assert fw.level(DC).options == {}


def test_import_needs_firewall_write_for_the_datacenter(cli, fw, tmp_path):
    r = cli("cluster", "firewall", "import", write(tmp_path, {"datacenter": {"rules": [SSH]}}), "-y", "-p", "p2")
    assert r.exit_code == 1 and "firewall_write: true" in r.output


def test_export_then_import_on_a_copy_gives_the_same(cli, fw, tmp_path):
    ok(cli("cluster", "firewall", "import", write(tmp_path, FULL), "-y", "-p", "p1"))
    out = tmp_path / "back.yaml"
    ok(cli("cluster", "firewall", "export", "-ou", str(out), "-p", "p1"))
    r = ok(cli("cluster", "firewall", "import", str(out), "-y", "-p", "p1"))
    assert "already matches" in r.output


def test_rule_edits_keep_the_longest_common_run():
    a, b, c, d = ({"type": "in", "action": "ACCEPT", "dport": str(p)} for p in (1, 2, 3, 4))
    removed, inserted = rule_edits([a, b, c], [b, d, c])
    assert removed == [0] and inserted == [(1, d)]
    assert rule_edits([a, b], [a, b]) == ([], [])
    # enable 1 written or left out is the same rule
    assert rule_edits([{**a, "enable": 1}], [a]) == ([], [])


def test_check_refs_reads_guest_objects_and_prefixes():
    doc = {"guests": {101: {"aliases": [{"name": "me", "cidr": "10.0.0.7"}],
                            "rules": [{"type": "in", "action": "ACCEPT", "source": "guest/me,10.0.0.0/24"}]}}}
    check_refs(doc, {"alias": set(), "ipset": set()})


def test_emptying_removes_users_before_what_they_use_and_turns_off_first(cli, fw, tmp_path, api):
    ok(cli("cluster", "firewall", "import", write(tmp_path, FULL), "-y", "-p", "p1"))
    start = len(api.calls)
    r = ok(cli("cluster", "firewall", "import", write(tmp_path, {"datacenter": {}, "guests": {101: {}}}),
               "-y", "-p", "p1"))
    paths = [c.path for c in api.calls[start:] if c.method != "GET"]
    off = paths.index(f"{DC}/options")
    assert off == 0  # the datacenter firewall goes off before anything is undone
    assert paths.index(f"{DC}/groups/web") < paths.index(f"{DC}/ipset/admins")
    # management holds an alias in FULL? admins does: the set goes before the alias
    assert paths.index(f"{DC}/ipset/admins") < paths.index(f"{DC}/aliases/gw")
    assert fw.level(DC).aliases == {} and fw.level(DC).ipsets == {} and fw.groups == {}
    assert "Access kept: the datacenter firewall is off" in r.output
