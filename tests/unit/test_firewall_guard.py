"""Does this machine keep the API under a firewall configuration?

The order is the one pve-firewall loads on PVE 9.1: the rules of the node,
then of the datacenter (a group in its place), then the management hosts,
then the input policy.
"""

from src.cli.firewall_guard import host_chain, in_set, judge, meets_api

ON = {"enable": 1}
ME = "10.0.0.7"
NODE = "10.0.0.1"


def j(dc=ON, node=None, ipsets=None, aliases=None, nets=("10.0.0.0/24",), node_rules=(), dc_rules=(), groups=None):
    return judge(ME, dc, node or {}, ipsets or {}, aliases or {}, list(nets), list(node_rules), list(dc_rules),
                 groups or {}, NODE)


def drop(**kw):
    return {"pos": 0, "type": "in", "action": "DROP", "enable": 1, **kw}


def accept(**kw):
    return {**drop(**kw), "action": "ACCEPT"}


def test_nothing_filters_the_api():
    assert j(dc={}).kept and "datacenter firewall is off" in j(dc={}).reason
    assert j(node={"enable": 0}).kept


def test_the_cluster_network_is_management_without_the_set():
    assert j().kept
    lost = j(nets=("192.168.9.0/24",))
    assert lost.kept is False and "outside the cluster network" in lost.reason


def test_once_the_set_exists_only_its_members_are_management():
    assert j(ipsets={"management": [{"cidr": "10.0.0.0/28"}]}).kept
    lost = j(ipsets={"management": [{"cidr": "10.9.0.0/16"}]})
    assert lost.kept is False and "not in the 'management' IP set" in lost.reason


def test_a_drop_rule_wins_over_the_management_hosts():
    # Seen on the lab: the rule of the node comes before the management RETURN
    lost = j(node_rules=[drop(proto="tcp", dport="8006", source=ME)])
    assert lost.kept is False and "rule 0 of the node drops 10.0.0.7" in lost.reason
    assert j(dc_rules=[drop()]).kept is False  # drop everything, from anywhere


def test_the_first_rule_that_meets_8006_decides():
    assert j(node_rules=[accept(source=ME)], dc_rules=[drop()]).kept
    assert j(node_rules=[drop(dport="22"), accept(dport="8006")]).kept
    assert j(node_rules=[drop(source="10.9.0.0/16")]).kept  # another source
    assert j(node_rules=[drop(proto="udp")]).kept
    assert j(node_rules=[drop(enable=0)]).kept


def test_a_group_counts_in_its_place():
    groups = {"lock": [drop(dport="8000:8010")]}
    assert j(node_rules=[{"pos": 0, "type": "group", "action": "lock", "enable": 1}], groups=groups).kept is False
    assert j(node_rules=[{"pos": 0, "type": "group", "action": "lock", "enable": 0}], groups=groups).kept


def test_an_accept_rule_keeps_the_api_outside_management():
    rule = accept(proto="tcp", dport="8006", source="+admins")
    access = j(ipsets={"management": [], "admins": [{"cidr": ME}]}, dc_rules=[rule])
    assert access.kept and "rule 0 of the datacenter lets 10.0.0.7 in" in access.reason


def test_a_drop_pvecli_cannot_place_is_not_a_yes():
    access = j(node_rules=[drop(iface="vmbr1")])
    assert access.kept is None and "cannot tell" in access.reason


def test_the_input_policy_accept_keeps_outsiders():
    assert j(dc={"enable": 1, "policy_in": "ACCEPT"}, nets=("192.168.9.0/24",)).kept


def test_nomatch_and_aliases_in_the_set():
    assert not in_set(ME, [{"cidr": "10.0.0.0/24"}, {"cidr": ME, "nomatch": 1}], {})
    assert in_set(ME, [{"cidr": "admin"}], {"admin": ME})
    assert in_set(ME, [{"cidr": "dc/admin"}], {"admin": "10.0.0.0/29"})


def test_which_rules_meet_the_api():
    def m(**kw):
        return meets_api(accept(**kw), ME, NODE, {}, {})

    assert m() and m(proto="tcp", dport="22,8006") and m(dest=NODE)
    assert not m(proto="udp") and not m(dport="443") and not m(macro="SSH") and not m(dest="10.0.0.2")
    assert m(source="+nope") is None  # an IP set pvecli does not know


def test_host_chain_order():
    chain = host_chain([accept(pos=1), {"pos": 0, "type": "group", "action": "g", "enable": 1}],
                       [drop()], {"g": [drop(dport="1")]})
    assert [r["_where"] for r in chain] == ["security group g (rule 0 of the node)", "rule 1 of the node",
                                            "rule 0 of the datacenter"]


def test_unknown_address():
    assert judge(None, ON, {}, {}, {}, [], [], [], {}).kept is None
