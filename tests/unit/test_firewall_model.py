"""The firewall model: defaults, cards, pitfalls, one-line rules."""

from src.cli.firewall import (
    DATACENTER_OFF,
    Status,
    describe_rule,
    effective,
    guest_nics,
    guest_scope,
    is_on,
    pitfalls,
    summary,
)

ACCEPT_SSH = {"pos": 0, "type": "in", "action": "ACCEPT", "macro": "SSH", "enable": 1}


def test_effective_falls_back_on_the_proxmox_default():
    assert effective("cluster", {}, "enable") == 0
    assert effective("node", {}, "enable") == 1
    assert effective("vm", {}, "policy_in") == "DROP"
    assert effective("vm", {"policy_in": "ACCEPT"}, "policy_in") == "ACCEPT"
    assert effective("vm", {}, "unknown") is None


def test_is_on_reads_every_form_proxmox_returns():
    assert is_on(1) and is_on("1") and is_on(True)
    assert not is_on(0) and not is_on("0") and not is_on(None) and not is_on(False)


def test_guest_nics_reads_the_firewall_flag_in_card_order():
    config = {
        "net10": "virtio=AA,bridge=vmbr0,firewall=1",
        "net0": "virtio=BB,bridge=vmbr0,firewall=1,tag=10",
        "net1": "virtio=CC,bridge=vmbr1",
        "name": "web",
        "netx": "ignored",
    }
    assert list(guest_nics(config).items()) == [("net0", True), ("net1", False), ("net10", True)]


def test_a_guest_with_rules_but_off_is_flagged():
    assert pitfalls("vm", True, False, [ACCEPT_SSH], {"net0": True}) == [
        "1 rule(s) but the firewall of this guest is off"
    ]


def test_a_guest_on_without_any_flagged_card_filters_nothing():
    found = pitfalls("ct", True, True, [ACCEPT_SSH], {"net0": False})
    assert found == ["no network card has firewall=1: the rules apply to no interface"]


def test_unflagged_cards_beside_a_flagged_one_are_named():
    found = pitfalls("vm", True, True, [], {"net0": True, "net1": False})
    assert found == ["not filtered: net1 (no firewall=1)"]


def test_datacenter_off_is_said_only_when_something_is_waiting_on_it():
    assert pitfalls("vm", False, False, [], {"net0": False}) == []
    assert DATACENTER_OFF in pitfalls("vm", False, True, [], {"net0": True})
    # A disabled rule waits on nothing
    assert pitfalls("node", False, False, [{**ACCEPT_SSH, "enable": 0}]) == []


def test_a_node_off_under_a_datacenter_on():
    assert pitfalls("node", True, False, []) == [
        "the firewall of this node is off: its host rules are not enforced"
    ]


def test_vnet_rules_need_nftables_on_every_node():
    found = pitfalls("vnet", True, True, [], nftables_nodes=(1, 3))
    assert found == ["vnet rules need the nftables firewall: on 1 of 3 node(s) only"]
    assert pitfalls("vnet", True, True, [], nftables_nodes=(3, 3)) == []


def test_describe_rule_reads_like_the_web_interface():
    rule = {"type": "in", "action": "ACCEPT", "proto": "tcp", "source": "+management",
            "dport": "8006", "iface": "net0", "log": "info", "comment": "web UI"}
    assert describe_rule(rule) == "IN ACCEPT tcp from +management dport 8006 on net0 log info  # web UI"
    assert describe_rule({"type": "group", "action": "web", "enable": 0}) == "group web (disabled)"
    assert describe_rule({**ACCEPT_SSH, "log": "nolog"}) == "IN ACCEPT SSH"


def _status(dc, on, nics, rules):
    return Status(guest_scope("vm", "pve1", 101), dc, on, {}, rules, nics=nics)


def test_summary_says_why_a_guest_is_not_protected():
    assert summary(_status(True, False, {"net0": True}, [])) == "off"
    assert "1 rule not enforced" in summary(_status(True, False, {"net0": True}, [ACCEPT_SSH]))
    assert "datacenter firewall is off" in summary(_status(False, True, {"net0": True}, [ACCEPT_SSH]))
    assert "no card has firewall=1" in summary(_status(True, True, {"net0": False}, []))
    text = summary(_status(True, True, {"net0": True, "net1": False}, [ACCEPT_SSH, ACCEPT_SSH]))
    assert "enforced" in text and "net0, 2 rules" in text and "not on net1" in text


def test_enforced_needs_the_three_switches():
    assert _status(True, True, {"net0": True}, []).enforced
    assert not _status(False, True, {"net0": True}, []).enforced
    assert not _status(True, False, {"net0": True}, []).enforced
    assert not _status(True, True, {"net0": False}, []).enforced
