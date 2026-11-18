"""SDN read commands: list, zones, info and health, against recorded zones and vnets."""

from __future__ import annotations

import json

from tests.fake_api import FakeAPI, fixture

NODES = [
    {"node": "pve1", "status": "online"},
    {"node": "pve2", "status": "online"},
    {"node": "pve3", "status": "offline"},
]


def flat(text: str) -> str:
    """Output with line wrapping and table borders undone: one row reads as words."""
    return " ".join(text.replace("\u2502", " ").split())


def route_deployment(api: FakeAPI, zone_status: dict, content: dict) -> None:
    """zone_status: {node: [zone entries]}, content: {(node, zone): [vnet entries]}."""
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/sdn/zones", lambda c: zone_status.get(c.match["node"], []))
    api.route(
        "GET",
        "/nodes/{node}/sdn/zones/{zone}/content",
        lambda c: content.get((c.match["node"], c.match["zone"]), []),
    )


def full_deployment(api: FakeAPI) -> None:
    """vsimple everywhere, vvlan missing on pve2, vvxlan deployed nowhere."""
    zs = [{"zone": "zsimple", "status": "available"}, {"zone": "zvlan", "status": "available"}]
    route_deployment(
        api,
        {"pve1": zs, "pve2": zs, "pve3": zs},
        {
            ("pve1", "zsimple"): [{"vnet": "vsimple", "status": "available"}],
            ("pve2", "zsimple"): [{"vnet": "vsimple", "status": "available"}],
            ("pve1", "zvlan"): [{"vnet": "vvlan", "status": "available"}],
            ("pve2", "zvlan"): [{"vnet": "vvlan", "status": "error"}],
        },
    )


# ── list ─────────────────────────────────────────────────────────────────


def test_list_shows_per_vnet_coverage_on_online_nodes_only(cli, api):
    api.route("GET", "/cluster/sdn/vnets", fixture("pve-9.1/sdn_vnets"))
    full_deployment(api)

    r = cli("sdn", "list", "-p", "p1")

    assert r.exit_code == 0, r.output
    out = flat(r.stdout)
    assert "vsimple zsimple - Lab simple no 2/2" in out
    assert "vvlan zvlan 42 Lab VLAN 42 no 1/2 missing: pve2" in out
    assert "vvxlan zvxlan 9000 Lab VXLAN no not deployed" in out
    # The offline node is never asked
    assert api.called("GET", "/nodes/pve3/sdn/zones") == []
    assert api.writes() == []


def test_list_json_carries_the_status_per_node(cli, api):
    api.route("GET", "/cluster/sdn/vnets", fixture("pve-9.1/sdn_vnets"))
    full_deployment(api)

    r = cli("sdn", "list", "--json", "-p", "p1")

    assert r.exit_code == 0, r.output
    data = {v["vnet"]: v for v in json.loads(r.stdout)}
    assert data["vsimple"]["status"] == {"pve1": "available", "pve2": "available"}
    assert data["vvlan"]["status"] == {"pve1": "available", "pve2": "error"}
    assert data["vvlan"]["tag"] == 42
    assert data["vvxlan"]["status"] == {}


def test_list_without_vnet(cli, api):
    api.route("GET", "/cluster/sdn/vnets", fixture("pve-9.2/sdn_vnets"))

    r = cli("sdn", "list", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "No SDN vnet defined" in r.stdout


def test_list_reports_an_api_error(cli, api):
    api.route("GET", "/cluster/sdn/vnets", api.forbidden("/sdn", "SDN.Audit"))

    r = cli("sdn", "list", "-p", "p1")

    assert r.exit_code == 1
    assert "SDN.Audit" in r.output


# ── zones ────────────────────────────────────────────────────────────────


def test_zones_shows_type_restriction_and_deployment(cli, api):
    zones = fixture("pve-9.1/sdn_zones")
    zones[1]["nodes"] = "pve1"
    zones[1]["mtu"] = 1450
    api.route("GET", "/cluster/sdn/zones", zones)
    route_deployment(
        api,
        {
            "pve1": [{"zone": "zsimple", "status": "available"}, {"zone": "zvlan", "status": "available"}],
            "pve2": [{"zone": "zsimple", "status": "available"}, {"zone": "zvlan", "status": "error"}],
        },
        {},
    )

    r = cli("sdn", "zones", "-p", "p1")

    assert r.exit_code == 0, r.output
    out = flat(r.stdout)
    assert "zsimple simple all nodes - 2/2" in out
    assert "zvlan vlan pve1 1450 1/2" in out
    assert "zvxlan vxlan all nodes - nowhere" in out


def test_zones_json_keeps_the_definition_and_adds_the_status(cli, api):
    api.route("GET", "/cluster/sdn/zones", fixture("pve-9.1/sdn_zones"))
    route_deployment(api, {"pve1": [{"zone": "zsimple", "status": "available"}]}, {})

    r = cli("sdn", "zones", "--json", "-p", "p1")

    assert r.exit_code == 0, r.output
    data = {z["zone"]: z for z in json.loads(r.stdout)}
    assert data["zsimple"]["ipam"] == "pve"
    assert data["zsimple"]["status"] == {"pve1": "available"}
    assert data["zvxlan"]["peers"].startswith("10.199.32.51")
    assert data["zvxlan"]["status"] == {}


def test_zones_without_zone(cli, api):
    api.route("GET", "/cluster/sdn/zones", [])

    r = cli("sdn", "zones", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "No SDN zone defined" in r.stdout


# ── info ─────────────────────────────────────────────────────────────────


def route_info(api: FakeAPI, ct_net: str = "name=eth0,bridge=vsimple,hwaddr=BC:24:11:A6:9E:69,ip=dhcp,type=veth") -> None:
    api.route("GET", "/cluster/sdn/vnets", fixture("pve-9.1/sdn_vnets"))
    api.route("GET", "/cluster/sdn/vnets/{vnet}", lambda c: next(
        v for v in fixture("pve-9.1/sdn_vnets") if v["vnet"] == c.match["vnet"]
    ))
    api.route("GET", "/cluster/sdn/vnets/{vnet}/subnets", [
        {
            "subnet": "zsimple-10.0.30.0-24", "cidr": "10.0.30.0/24", "vnet": "vsimple",
            "zone": "zsimple", "type": "subnet", "gateway": "10.0.30.1", "snat": 1,
            "dhcp-range": [{"start-address": "10.0.30.100", "end-address": "10.0.30.200"}],
        },
    ])
    api.route("GET", "/cluster/resources", [
        {"vmid": 300, "type": "qemu", "node": "pve-lab2", "name": "guest-sdn", "status": "stopped"},
        {"vmid": 301, "type": "qemu", "node": "pve-lab1", "name": "other", "status": "running"},
        {"vmid": 400, "type": "lxc", "node": "pve-lab1", "name": "ct-sdn", "status": "running"},
    ])
    api.route("GET", "/nodes/{node}/qemu/{vmid}/config", lambda c: (
        fixture("pve-9.1/qemu_config") if c.match["vmid"] == "300"
        # vsimple1 shares a prefix with vsimple: it is not the same vnet
        else {"net0": "virtio=BC:24:11:00:00:01,bridge=vsimple1"}
    ))
    lxc = fixture("pve-9.1/lxc_config")
    lxc["net1"] = ct_net
    api.route("GET", "/nodes/{node}/lxc/{vmid}/config", lxc)


def test_info_lists_subnets_and_only_the_guests_on_that_vnet(cli, api):
    route_info(api, ct_net="name=eth1,bridge=vvlan,tag=7,type=veth")

    r = cli("sdn", "info", "vsimple", "-p", "p1")

    assert r.exit_code == 0, r.output
    out = flat(r.stdout)
    assert "Zone: zsimple" in out
    assert "Alias: Lab simple" in out
    assert "10.0.30.0/24 10.0.30.1 yes 10.0.30.100 - 10.0.30.200" in out
    assert "Guests on vsimple (2)" in out
    assert "300 guest-sdn VM pve-lab2 net0" in out
    assert "400 ct-sdn CT pve-lab1 net0" in out
    assert "other" not in out
    assert api.writes() == []


def test_info_json_reports_the_vlan_tag_of_a_guest_nic(cli, api):
    route_info(api, ct_net="name=eth1,bridge=vvlan,tag=7,type=veth")

    r = cli("sdn", "info", "vvlan", "--json", "-p", "p1")

    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["definition"]["tag"] == 42
    assert data["guests"] == [{
        "vmid": 400, "name": "ct-sdn", "type": "CT", "node": "pve-lab1",
        "iface": "net1", "tag": "7", "status": "running",
    }]


def test_info_unknown_vnet_names_the_available_ones(cli, api):
    route_info(api)

    r = cli("sdn", "info", "nope", "-p", "p1")

    assert r.exit_code == 1
    assert "Vnet 'nope' not found" in r.output
    assert "vsimple, vvlan, vvxlan" in r.output


def test_info_picks_the_vnet_from_a_menu(cli, api, ui):
    route_info(api)
    api.route("GET", "/cluster/sdn/vnets/{vnet}/subnets", [])
    ui.menu("vvxlan")

    r = cli("sdn", "info", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "Vnet vvxlan" in r.stdout
    assert "No subnet on this vnet" in r.stdout
    assert "No guest attached to this vnet" in r.stdout


def test_info_menu_cancel(cli, api, ui):
    route_info(api)
    ui.menu(None)

    r = cli("sdn", "info", "-p", "p1")

    assert r.exit_code == 0
    assert api.called("GET", "/cluster/sdn/vnets/{vnet}") == []


# ── health (text) ────────────────────────────────────────────────────────


def test_health_text_reports_pending_partial_coverage_and_errors(cli, api):
    api.route("GET", "/cluster/sdn/zones", lambda c: (
        [{"zone": "znew", "type": "simple", "state": "new"}] if c.params else fixture("pve-9.1/sdn_zones")
    ))
    api.route("GET", "/cluster/sdn/vnets", fixture("pve-9.1/sdn_vnets"))
    api.route("GET", "/cluster/sdn/vnets/{vnet}/subnets", lambda c: (
        [{"cidr": "10.9.0.0/24", "state": "deleted"}] if c.match["vnet"] == "vsimple" else []
    ))
    full_deployment(api)

    r = cli("sdn", "health", "-p", "p1")

    assert r.exit_code == 0, r.output
    out = flat(r.stdout)
    assert "zone znew (new)" in out
    assert "subnet 10.9.0.0/24 on vsimple (deleted)" in out
    assert "vvlan missing on pve2" in out
    assert "vvxlan missing on pve1, pve2" in out
    assert "vvlan: {'pve2': 'error'}" in out
    assert "pvecli sdn apply" in out
