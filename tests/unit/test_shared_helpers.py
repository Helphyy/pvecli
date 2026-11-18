"""Helpers of src/cli/_shared.py: ID lists, guest targeting by filter, HA
placement checks and rules, migration precondition parsing.

Protects the rules that decide WHICH guests a multi-target command acts on
and what is sent to the HA manager.
"""

from __future__ import annotations

import pytest
import typer

from src.api.client import ProxmoxClient
from src.api.exceptions import PVECliError
from src.cli import _shared
from src.cli._shared import (
    _blocked_reason,
    _check_placement_option,
    _describe_rule,
    _first,
    _ha_rule_name,
    _rule_resources,
    _validate_ha_nodes,
    has_filters,
    parse_id_list,
    resolve_targets,
)
from src.models.config import ProfileConfig
from tests.fake_api import fixture


@pytest.fixture
def client(api) -> ProxmoxClient:
    """A real ProxmoxClient whose network layer is the fake API."""
    return ProxmoxClient(ProfileConfig(
        host="h1", verify_ssl=False,
        auth={"type": "password", "user": "root@pam", "password": "x"},
    ))


# ── parse_id_list ────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    ("100", [100]),
    ("100,101,102", [100, 101, 102]),
    (" 100 , 101 ", [100, 101]),
    ("100-103", [100, 101, 102, 103]),
    ("105-105", [105]),
    ("100-102,200,300-301", [100, 101, 102, 200, 300, 301]),
    ("100,,101,", [100, 101]),
    ("100 - 102", [100, 101, 102]),
])
def test_parse_id_list_accepts_lists_and_ranges(raw, expected):
    assert parse_id_list(raw) == expected


@pytest.mark.parametrize("raw, message", [
    ("105-100", "start > end"),
    ("abc", "Invalid CT ID"),
    ("100-abc", "Invalid CT range"),
    ("100-", "Invalid CT range"),
    ("-100", "Invalid CT range"),
    ("", "No valid CT IDs"),
    (" , ", "No valid CT IDs"),
])
def test_parse_id_list_rejects_bad_input_naming_the_label(raw, message, capsys):
    with pytest.raises(typer.Exit) as exc:
        parse_id_list(raw, "CT")
    assert exc.value.exit_code == 1
    assert message in capsys.readouterr().err


# ── has_filters / resolve_targets ────────────────────────────────────────


def test_has_filters():
    assert has_filters(None, None, None) is False
    assert has_filters("", "", "") is False
    assert has_filters("prod", None, None) is True
    assert has_filters(None, "pool1", None) is True
    assert has_filters(None, None, "pve1") is True


GUESTS = [
    {"vmid": 103, "type": "qemu", "node": "pve1", "tags": "prod", "pool": "p2", "name": "c"},
    {"vmid": 100, "type": "qemu", "node": "pve1", "tags": "web;prod", "pool": "p1", "name": "a"},
    {"vmid": 101, "type": "qemu", "node": "pve2", "tags": "production", "name": "b"},
    {"vmid": 102, "type": "qemu", "node": "pve1", "tags": "prod", "template": 1, "name": "tpl"},
    {"vmid": 200, "type": "lxc", "node": "pve1", "tags": "prod", "pool": "p1", "name": "ct"},
]


@pytest.fixture
def guests(api):
    api.route("GET", "/cluster/resources", GUESTS)
    return api


async def test_resolve_targets_without_filter_is_parse_id_list(client, guests):
    assert await resolve_targets(client, "100,101", "qemu", "VM") == [100, 101]
    # No API call: the IDs are taken as given
    assert guests.called("GET", "/cluster/resources") == []


@pytest.mark.parametrize("kwargs, expected", [
    # Exact tag only: 'production' is not 'prod'; template 102 and CT 200 excluded
    ({"tag": "prod"}, [100, 103]),
    ({"pool": "p1"}, [100]),
    ({"node_filter": "pve1"}, [100, 103]),
    ({"node_filter": "pve2"}, [101]),
    ({"tag": "prod", "pool": "p2"}, [103]),
    ({"tag": "production"}, [101]),
])
async def test_resolve_targets_filters_are_and_ed_and_sorted(client, guests, kwargs, expected):
    assert await resolve_targets(client, None, "qemu", "VM", **kwargs) == expected


async def test_resolve_targets_filters_by_type(client, guests):
    assert await resolve_targets(client, None, "lxc", "CT", tag="prod") == [200]


async def test_resolve_targets_ids_narrow_the_filter(client, guests):
    assert await resolve_targets(client, "100-101", "qemu", "VM", tag="prod") == [100]


async def test_resolve_targets_nothing_matching_is_an_error(client, guests):
    with pytest.raises(PVECliError, match="tag=prod, node=pve2"):
        await resolve_targets(client, None, "qemu", "VM", tag="prod", node_filter="pve2")


async def test_resolve_targets_never_picks_templates(client, guests):
    with pytest.raises(PVECliError):
        await resolve_targets(client, "102", "qemu", "VM", tag="prod")


async def test_resolve_targets_confirms_an_action(client, guests, monkeypatch):
    asked = []
    monkeypatch.setattr(_shared, "confirm", lambda msg, default=False: asked.append(msg) or False)
    with pytest.raises(typer.Exit) as exc:
        await resolve_targets(client, None, "qemu", "VM", tag="prod", action="Stop")
    assert exc.value.exit_code == 0
    assert asked and "2" in asked[0]

    asked.clear()
    monkeypatch.setattr(_shared, "confirm", lambda msg, default=False: asked.append(msg) or True)
    assert await resolve_targets(client, None, "qemu", "VM", tag="prod", action="Stop") == [100, 103]
    assert len(asked) == 1


async def test_resolve_targets_yes_skips_confirmation(client, guests, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("confirm must not be called with --yes")

    monkeypatch.setattr(_shared, "confirm", boom)
    assert await resolve_targets(client, None, "qemu", "VM", tag="prod", action="Stop", yes=True) == [100, 103]


# ── HA rules ─────────────────────────────────────────────────────────────


def test_ha_rule_name_is_unique_per_guest():
    assert _ha_rule_name("vm", 100) == "pvecli-vm100"
    assert _ha_rule_name("ct", 100) == "pvecli-ct100"
    names = {_ha_rule_name(k, i) for k in ("vm", "ct") for i in (10, 100, 1000)}
    assert len(names) == 6


def test_rule_resources_splits_and_trims():
    rule = {"resources": "vm:100, vm:101,,ct:200 "}
    assert _rule_resources(rule) == ["vm:100", "vm:101", "ct:200"]
    assert _rule_resources({}) == []
    assert _rule_resources({"resources": ""}) == []
    # vm:100 does not cover vm:1000
    assert "vm:100" not in _rule_resources({"resources": "vm:1000,vm:10"})


def test_describe_node_affinity_rule():
    rule = {"rule": "pvecli-vm100", "type": "node-affinity", "nodes": "pve1:2,pve2", "strict": 1}
    assert _describe_rule(rule) == "pvecli-vm100: nodes pve1:2,pve2 (strict)"
    rule["strict"] = 0
    assert _describe_rule(rule) == "pvecli-vm100: nodes pve1:2,pve2"


def test_describe_resource_affinity_rule():
    rule = {"rule": "apart", "type": "resource-affinity", "affinity": "negative",
            "resources": "vm:100,vm:101", "disable": 1}
    assert _describe_rule(rule) == "apart: negative affinity with vm:100,vm:101 [disabled]"


@pytest.mark.parametrize("mode, group, nodes, refused, hint", [
    ("rules", "grp1", None, True, "--nodes"),
    ("rules", None, "pve1:2", False, None),
    ("rules", None, None, False, None),
    ("groups", None, "pve1", True, "--group"),
    ("groups", None, "", True, "--group"),
    ("groups", "grp1", None, False, None),
    ("groups", None, None, False, None),
])
def test_check_placement_option(mode, group, nodes, refused, hint):
    if refused:
        with pytest.raises(PVECliError, match=hint):
            _check_placement_option(mode, group, nodes)
    else:
        _check_placement_option(mode, group, nodes)


@pytest.fixture
def lab_nodes(api):
    api.route("GET", "/nodes", fixture("pve-9.1/nodes"))
    return api


async def test_validate_ha_nodes_cleans_a_valid_list(client, lab_nodes):
    assert await _validate_ha_nodes(client, " pve-lab1:2 , pve-lab2,") == "pve-lab1:2,pve-lab2"


@pytest.mark.parametrize("nodes, message", [
    ("pve-lab10", "Unknown node 'pve-lab10'"),  # close to pve-lab1, not a member
    ("pve-lab", "Unknown node"),
    ("pve-lab1:high", "Invalid priority"),
    ("pve-lab1:-1", "Invalid priority"),
    ("", "No node"),
    (" , ", "No node"),
])
async def test_validate_ha_nodes_rejects(client, lab_nodes, nodes, message):
    with pytest.raises(PVECliError, match=message):
        await _validate_ha_nodes(client, nodes)


async def test_validate_ha_nodes_empty_priority(client, lab_nodes):
    # PVE's <node>[:<prio>] format refuses 'pve-lab1:': refuse it or drop the colon
    try:
        out = await _validate_ha_nodes(client, "pve-lab1:")
    except PVECliError:
        return
    assert out == "pve-lab1"


# ── migration preconditions ──────────────────────────────────────────────


def test_first_reads_both_spellings():
    qemu = fixture("pve-9.1/qemu_migrate_preconditions")
    lxc = fixture("pve-9.2/lxc_migrate_preconditions")
    assert _first(qemu, "allowed_nodes", "allowed-nodes") == ["pve-lab1", "pve-lab4", "pve-lab5", "pve-lab3"]
    assert _first(lxc, "allowed_nodes", "allowed-nodes") == []
    assert _first(lxc, "not_allowed_nodes", "not-allowed-nodes") == {}
    assert _first({}, "a", "b") is None
    # Present but empty wins over a later key: presence, not truthiness
    assert _first({"a": [], "b": [1]}, "a", "b") == []


def test_blocked_reason():
    # PVE 9.1 answers an empty dict for a node with nothing to say
    entries = fixture("pve-9.1/qemu_migrate_preconditions")["not_allowed_nodes"]
    assert _blocked_reason(entries["pve-lab1"]) == "not allowed"
    assert _blocked_reason({"unavailable_storages": ["local-zfs", "tank"]}) == \
        "unavailable storages: local-zfs, tank"
    assert _blocked_reason({"unavailable-storages": ["a"], "reason": "x"}) == \
        "unavailable storages: a; reason: x"
    assert _blocked_reason("node is offline") == "node is offline"
