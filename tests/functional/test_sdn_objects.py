"""SDN objects: zone, vnet and subnet add/set/remove, unlock, discard.

Every write stays pending until 'sdn apply'. The rules checked here are the
ones PVE enforces (identifier syntax, tag ranges, a vnet owning subnets
cannot go) plus the ones only the CLI can (no guest left on a removed vnet).
"""

from __future__ import annotations

import pytest

import src.cli.sdn as sdn
from tests.fake_api import FakeAPI, fixture, upid

SUBNET = {
    "subnet": "zsimple-10.0.30.0-24", "cidr": "10.0.30.0/24", "vnet": "vsimple",
    "zone": "zsimple", "type": "subnet", "gateway": "10.0.30.1",
}


def flat(text: str) -> str:
    return " ".join(text.replace("│", " ").split())


def route_sdn(api: FakeAPI, subnets: dict | None = None, guests: dict | None = None) -> None:
    """Recorded zones and vnets, subnets per vnet, guest configs per vmid."""
    guests = guests or {}
    api.route("GET", "/cluster/sdn/zones", fixture("pve-9.1/sdn_zones"))
    api.route("GET", "/cluster/sdn/vnets", fixture("pve-9.1/sdn_vnets"))
    api.route("GET", "/cluster/sdn/vnets/{vnet}/subnets", lambda c: (subnets or {}).get(c.match["vnet"], []))
    api.route("GET", "/cluster/resources", [
        {"vmid": vmid, "type": kind, "node": "pve-lab1", "name": f"g{vmid}", "status": "running"}
        for vmid, (kind, _) in guests.items()
    ])
    api.route("GET", "/nodes/{node}/qemu/{vmid}/config", lambda c: guests[int(c.match["vmid"])][1])
    api.route("GET", "/nodes/{node}/lxc/{vmid}/config", lambda c: guests[int(c.match["vmid"])][1])
    for method, path in (
        ("POST", "/cluster/sdn/zones"),
        ("PUT", "/cluster/sdn/zones/{zone}"),
        ("DELETE", "/cluster/sdn/zones/{zone}"),
        ("POST", "/cluster/sdn/vnets"),
        ("DELETE", "/cluster/sdn/vnets/{vnet}"),
        ("POST", "/cluster/sdn/vnets/{vnet}/subnets"),
        ("DELETE", "/cluster/sdn/vnets/{vnet}/subnets/{subnet}"),
        ("POST", "/cluster/sdn/rollback"),
        ("DELETE", "/cluster/sdn/lock"),
    ):
        api.route(method, path, None)


# ── profile without sdn_write ────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        ("zone", "remove", "zsimple", "-y"),
        ("vnet", "add", "vnew", "--zone", "zsimple", "-y"),
        ("vnet", "remove", "vsimple", "-y"),
        ("subnet", "add", "10.0.40.0/24", "--vnet", "vsimple", "-y"),
        ("subnet", "remove", "10.0.30.0/24", "--vnet", "vsimple", "-y"),
        ("discard", "-y"),
    ],
    ids=["zone-remove", "vnet-add", "vnet-remove", "subnet-add", "subnet-remove", "discard"],
)
def test_object_writes_refused_without_sdn_write(cli, api, args):
    route_sdn(api, subnets={"vsimple": [SUBNET]})

    r = cli("sdn", *args, "-p", "p2")

    assert r.exit_code == 1
    assert "SDN writes are disabled for profile 'p2'" in r.output
    assert api.writes() == []


# ── zone add ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["z", "1zone", "zone-1", "z_1", "abcdefghi"])
def test_zone_add_refuses_an_invalid_identifier(cli, api, name):
    route_sdn(api)

    r = cli("sdn", "zone", "add", name, "--type", "simple", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "2 to 8 characters" in r.output
    assert api.writes() == []


@pytest.mark.parametrize("name", ["zb", "abcdefgh", "Lab9"])
def test_zone_add_accepts_a_valid_identifier(cli, api, name):
    route_sdn(api)

    r = cli("sdn", "zone", "add", name, "--type", "simple", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert w.data == {"zone": name, "type": "simple", "ipam": "pve"}


def test_zone_add_refuses_an_existing_zone(cli, api):
    route_sdn(api)

    r = cli("sdn", "zone", "add", "zvlan", "--type", "simple", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "Zone 'zvlan' already exists" in r.output
    assert api.writes() == []


def test_zone_add_refuses_an_unknown_type(cli, api):
    route_sdn(api)

    r = cli("sdn", "zone", "add", "z1", "--type", "geneve", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "Unknown zone type 'geneve'" in r.output
    assert api.writes() == []


@pytest.mark.parametrize(
    ("zone_type", "missing"),
    [("vlan", "--bridge"), ("qinq", "--bridge"), ("vxlan", "--peers"), ("evpn", "--vrf-vxlan")],
)
def test_zone_add_non_interactive_names_the_missing_option(cli, api, zone_type, missing):
    route_sdn(api)
    api.route("GET", "/cluster/sdn/controllers", [{"controller": "evpnctl", "type": "evpn"}])
    extra = ("--controller", "evpnctl") if zone_type == "evpn" else ()

    r = cli("sdn", "zone", "add", "z1", "--type", zone_type, *extra, "-y", "-p", "p1")

    assert r.exit_code == 1
    assert missing in r.output
    assert api.writes() == []


def test_zone_add_qinq_sends_bridge_and_service_tag(cli, api):
    route_sdn(api)

    r = cli("sdn", "zone", "add", "zq", "--type", "qinq", "--bridge", "vmbr0", "--tag", "100",
            "--mtu", "1496", "--nodes", "pve-lab1,pve-lab2", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert w.data == {
        "zone": "zq", "type": "qinq", "bridge": "vmbr0", "tag": 100, "mtu": 1496,
        "nodes": "pve-lab1,pve-lab2", "ipam": "pve",
    }
    assert "pvecli sdn apply" in r.stdout


@pytest.mark.parametrize("tag", ["0", "4095", "5000"])
def test_zone_add_qinq_refuses_a_service_tag_outside_1_4094(cli, api, tag):
    route_sdn(api)

    r = cli("sdn", "zone", "add", "zq", "--type", "qinq", "--bridge", "vmbr0", "--tag", tag,
            "-y", "-p", "p1")

    assert r.exit_code == 1
    assert api.writes() == []


def test_zone_add_evpn_without_any_controller_is_refused(cli, api):
    route_sdn(api)
    api.route("GET", "/cluster/sdn/controllers", [])

    r = cli("sdn", "zone", "add", "ze", "--type", "evpn", "--vrf-vxlan", "10000", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "needs a controller" in r.output
    assert api.writes() == []


def test_zone_add_evpn_sends_controller_and_vrf_vni(cli, api):
    route_sdn(api)
    api.route("GET", "/cluster/sdn/controllers", [{"controller": "evpnctl", "type": "evpn"}])

    r = cli("sdn", "zone", "add", "ze", "--type", "evpn", "--controller", "evpnctl",
            "--vrf-vxlan", "16777215", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert w.data == {"zone": "ze", "type": "evpn", "controller": "evpnctl",
                      "vrf-vxlan": 16777215, "ipam": "pve"}


def test_zone_add_interactive_vlan_picks_the_bridge_of_the_first_online_node(cli, api, ui):
    route_sdn(api)
    api.route("GET", "/nodes", fixture("pve-9.1/nodes"))
    api.route("GET", "/nodes/{node}/network", [
        {"iface": "eno1", "type": "eth"},
        {"iface": "vmbr0", "type": "bridge", "comments": "LAN"},
        {"iface": "vmbr1", "type": "bridge"},
    ])
    ui.menu("vlan", "vmbr1")
    ui.text("zv2")
    ui.confirm(True)

    r = cli("sdn", "zone", "add", "-p", "p1")

    assert r.exit_code == 0, r.output
    # Sorted: pve-lab1 is the first online node
    assert api.called("GET", "/nodes/pve-lab1/network")
    [w] = api.writes()
    assert w.data == {"zone": "zv2", "type": "vlan", "bridge": "vmbr1", "ipam": "pve"}
    assert ui.left() == {}


def test_zone_add_interactive_vxlan_offers_the_cluster_node_ips_as_peers(cli, api, ui, monkeypatch):
    route_sdn(api)
    api.route("GET", "/cluster/status", fixture("pve-9.1/cluster_status"))
    ips = sorted(n["ip"] for n in fixture("pve-9.1/cluster_status") if n.get("type") == "node")
    seen = {}

    def answer(message, *a, default=None, **k):
        seen[str(message)] = default
        return ui.text_answer(message, *a, default=default, **k)

    ui.menu("vxlan")
    ui.text("zx", None)  # second answer: keep the default peers
    ui.confirm(True)
    monkeypatch.setattr(sdn, "menu_prompt", answer)

    r = cli("sdn", "zone", "add", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert seen["  Peer IPs (comma separated)"] == ",".join(ips)
    [w] = api.writes()
    assert w.data["peers"] == ",".join(ips)


def test_zone_add_interactive_qinq_reprompts_a_non_numeric_tag(cli, api, ui):
    route_sdn(api)
    ui.menu("qinq")
    ui.text("zq", "abc", "300")
    ui.confirm(True)

    r = cli("sdn", "zone", "add", "--bridge", "vmbr0", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "Enter a whole number" in r.output
    [w] = api.writes()
    assert w.data["tag"] == 300


def test_zone_add_interactive_cancel_on_type_writes_nothing(cli, api, ui):
    route_sdn(api)
    ui.menu(None)

    r = cli("sdn", "zone", "add", "-p", "p1")

    assert r.exit_code == 0
    assert api.writes() == []


def test_zone_add_declined_confirmation_writes_nothing(cli, api, ui):
    route_sdn(api)
    ui.confirm(False)

    r = cli("sdn", "zone", "add", "z1", "--type", "simple", "-p", "p1")

    assert r.exit_code == 0
    assert "Zone: z1" in r.stdout
    assert api.writes() == []


# ── zone set / remove ────────────────────────────────────────────────────


def test_zone_set_mtu_nodes_and_peers(cli, api):
    route_sdn(api)

    r = cli("sdn", "zone", "set", "zvxlan", "--mtu", "1450", "--peers", "10.0.0.1,10.0.0.2",
            "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert (w.method, w.path) == ("PUT", "/cluster/sdn/zones/zvxlan")
    assert w.data == {"mtu": 1450, "peers": "10.0.0.1,10.0.0.2"}


def test_zone_set_no_dhcp_deletes_the_property(cli, api):
    route_sdn(api)

    r = cli("sdn", "zone", "set", "zsimple", "--no-dhcp", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert w.data == {"delete": "dhcp"}
    assert "dhcp: (cleared)" in r.stdout


def test_zone_set_without_option_is_refused(cli, api):
    route_sdn(api)

    r = cli("sdn", "zone", "set", "zsimple", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "Nothing to change" in r.output
    assert api.writes() == []


def test_zone_set_unknown_zone(cli, api):
    route_sdn(api)

    r = cli("sdn", "zone", "set", "nope", "--mtu", "1400", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "Zone 'nope' not found" in r.output
    assert api.writes() == []


def test_zone_set_picks_from_menu_and_cancel_on_confirmation(cli, api, ui):
    route_sdn(api)
    ui.menu("zvlan")
    ui.confirm(False)

    r = cli("sdn", "zone", "set", "--mtu", "1400", "-p", "p1")

    assert r.exit_code == 0
    assert "Zone: zvlan" in r.stdout
    assert api.writes() == []


def test_zone_remove_refused_while_a_vnet_uses_it(cli, api):
    route_sdn(api)

    r = cli("sdn", "zone", "remove", "zvlan", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "still holds vnet(s): vvlan" in r.output
    assert api.writes() == []


def test_zone_remove_empty_zone(cli, api):
    route_sdn(api)
    api.route("GET", "/cluster/sdn/zones", [*fixture("pve-9.1/sdn_zones"), {"zone": "zempty", "type": "simple"}])

    r = cli("sdn", "zone", "remove", "zempty", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert (w.method, w.path) == ("DELETE", "/cluster/sdn/zones/zempty")


def test_zone_remove_declined_writes_nothing(cli, api, ui):
    route_sdn(api)
    api.route("GET", "/cluster/sdn/zones", [{"zone": "zempty", "type": "simple"}])
    ui.confirm(False)

    r = cli("sdn", "zone", "remove", "zempty", "-p", "p1")

    assert r.exit_code == 0
    assert api.writes() == []


def test_zone_remove_unknown_zone(cli, api):
    route_sdn(api)

    r = cli("sdn", "zone", "remove", "nope", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "Zone 'nope' not found" in r.output
    assert api.writes() == []


# ── vnet add ─────────────────────────────────────────────────────────────


def test_vnet_add_in_a_vlan_zone_sends_tag_alias_and_vlanaware(cli, api):
    route_sdn(api)

    r = cli("sdn", "vnet", "add", "vv2", "--zone", "zvlan", "--tag", "4094", "--alias", "DMZ",
            "--vlanaware", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert (w.method, w.path) == ("POST", "/cluster/sdn/vnets")
    assert w.data == {"vnet": "vv2", "zone": "zvlan", "tag": 4094, "alias": "DMZ", "vlanaware": 1}


@pytest.mark.parametrize(
    ("zone", "tag"),
    [("zvlan", "0"), ("zvlan", "4095"), ("zvxlan", "16777216")],
)
def test_vnet_add_refuses_a_tag_outside_the_zone_range(cli, api, zone, tag):
    route_sdn(api)

    r = cli("sdn", "vnet", "add", "vv2", "--zone", zone, "--tag", tag, "-y", "-p", "p1")

    assert r.exit_code == 1
    assert f"got {tag}" in r.output
    assert api.writes() == []


def test_vnet_add_vxlan_accepts_the_largest_vni(cli, api):
    route_sdn(api)

    r = cli("sdn", "vnet", "add", "vx2", "--zone", "zvxlan", "--tag", "16777215", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert w.data["tag"] == 16777215


def test_vnet_add_asks_the_tag_and_reprompts_out_of_range(cli, api, ui):
    route_sdn(api)
    ui.text("5000", "abc", "42")

    r = cli("sdn", "vnet", "add", "vv2", "--zone", "zvlan", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "Enter a value between 1 and 4094" in r.output
    assert "Enter a whole number" in r.output
    [w] = api.writes()
    assert w.data["tag"] == 42
    assert any("VLAN tag (1-4094)" in q for _, q in ui.asked)


def test_vnet_add_empty_tag_answer_cancels(cli, api, ui):
    route_sdn(api)
    ui.text(None)
    ui.confirm(True)

    r = cli("sdn", "vnet", "add", "vv2", "--zone", "zvlan", "-p", "p1")

    assert r.exit_code == 0
    assert api.writes() == []


@pytest.mark.parametrize(("zone", "label"), [("zvlan", "1-4094"), ("zvxlan", "1-16777215")])
def test_vnet_add_without_terminal_names_the_missing_tag(cli, api, monkeypatch, zone, label):
    # Seen on the lab: a script got a bare "Aborted." from the tag question
    from src.utils import menu

    monkeypatch.setattr(menu, "_interactive", lambda: False)
    route_sdn(api)

    r = cli("sdn", "vnet", "add", "vv2", "--zone", zone, "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "--tag" in r.output and label in r.output
    assert api.writes() == []


@pytest.mark.parametrize("name", ["v", "9net", "v-net", "vnet12345"])
def test_vnet_add_refuses_an_invalid_identifier(cli, api, name):
    route_sdn(api)

    r = cli("sdn", "vnet", "add", name, "--zone", "zsimple", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "2 to 8 characters" in r.output
    assert api.writes() == []


def test_vnet_add_refuses_an_existing_vnet(cli, api):
    route_sdn(api)

    r = cli("sdn", "vnet", "add", "vsimple", "--zone", "zsimple", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "Vnet 'vsimple' already exists" in r.output
    assert api.writes() == []


def test_vnet_add_refuses_an_unknown_zone(cli, api):
    route_sdn(api)

    r = cli("sdn", "vnet", "add", "vn", "--zone", "nope", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "Zone 'nope' not found" in r.output
    assert api.writes() == []


def test_vnet_add_without_any_zone(cli, api):
    route_sdn(api)
    api.route("GET", "/cluster/sdn/zones", [])

    r = cli("sdn", "vnet", "add", "vn", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "pvecli sdn zone add" in r.output
    assert api.writes() == []


def test_vnet_add_interactive_simple_zone_asks_no_tag(cli, api, ui):
    route_sdn(api)
    ui.menu("zsimple")
    ui.text("vn2")
    ui.confirm(True)

    r = cli("sdn", "vnet", "add", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert w.data == {"vnet": "vn2", "zone": "zsimple"}
    assert ui.left() == {}


def test_vnet_add_declined_writes_nothing(cli, api, ui):
    route_sdn(api)
    ui.confirm(False)

    r = cli("sdn", "vnet", "add", "vn2", "--zone", "zsimple", "-p", "p1")

    assert r.exit_code == 0
    assert api.writes() == []


# ── vnet remove ──────────────────────────────────────────────────────────


def test_vnet_remove_refused_while_a_guest_is_attached(cli, api):
    route_sdn(api, guests={
        300: ("qemu", fixture("pve-9.1/qemu_config")),
        400: ("lxc", fixture("pve-9.1/lxc_config")),
    })

    r = cli("sdn", "vnet", "remove", "vsimple", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "still used by: 300 (g300), 400 (g400)" in flat(r.output)
    assert api.writes() == []


def test_vnet_remove_refused_while_it_owns_a_subnet(cli, api):
    route_sdn(api, subnets={"vsimple": [SUBNET]})

    r = cli("sdn", "vnet", "remove", "vsimple", "-y", "-p", "p1")

    assert r.exit_code == 1
    out = flat(r.output)
    assert "still owns subnet(s): 10.0.30.0/24" in out
    assert "pvecli sdn subnet remove <cidr> --vnet vsimple" in out
    assert api.writes() == []


def test_vnet_remove_ignores_a_guest_on_a_vnet_sharing_its_prefix(cli, api):
    route_sdn(api, guests={301: ("qemu", {"net0": "virtio=BC:24:11:00:00:01,bridge=vsimple1"})})

    r = cli("sdn", "vnet", "remove", "vsimple", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert (w.method, w.path) == ("DELETE", "/cluster/sdn/vnets/vsimple")


def test_vnet_remove_picked_from_menu_then_declined(cli, api, ui):
    route_sdn(api)
    ui.menu("vvxlan")
    ui.confirm(False)

    r = cli("sdn", "vnet", "remove", "-p", "p1")

    assert r.exit_code == 0
    assert api.writes() == []


def test_vnet_remove_unknown_vnet(cli, api):
    route_sdn(api)

    r = cli("sdn", "vnet", "remove", "nope", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "Vnet 'nope' not found" in r.output
    assert api.writes() == []


# ── subnet add / remove ──────────────────────────────────────────────────


def test_subnet_add_sends_gateway_snat_dhcp_range_and_dns(cli, api):
    route_sdn(api)

    r = cli("sdn", "subnet", "add", "10.0.40.0/24", "--vnet", "vsimple", "--gateway", "10.0.40.1",
            "--snat", "--dhcp-range", "10.0.40.100:10.0.40.200", "--dhcp-dns", "10.0.40.1",
            "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert (w.method, w.path) == ("POST", "/cluster/sdn/vnets/vsimple/subnets")
    assert w.data == {
        "subnet": "10.0.40.0/24", "type": "subnet", "vnet": "vsimple", "gateway": "10.0.40.1",
        "snat": 1, "dhcp-range": "start-address=10.0.40.100,end-address=10.0.40.200",
        "dhcp-dns-server": "10.0.40.1",
    }
    # zsimple has DHCP: no warning about an unused range
    assert "has no DHCP enabled" not in r.output
    assert "dnsmasq" in r.output


def test_subnet_add_warns_when_the_zone_has_no_dhcp(cli, api):
    route_sdn(api)

    r = cli("sdn", "subnet", "add", "10.0.42.0/24", "--vnet", "vvlan",
            "--dhcp-range", "10.0.42.10:10.0.42.20", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "Zone 'zvlan' has no DHCP enabled" in flat(r.output)
    assert len(api.writes()) == 1


def test_subnet_add_on_a_vnet_still_pending_creation(cli, api):
    route_sdn(api)
    api.route("GET", "/cluster/sdn/vnets", lambda c: (
        [{"vnet": "vnew", "state": "new", "pending": {"zone": "zsimple"}}] if c.params else []
    ))

    r = cli("sdn", "subnet", "add", "10.0.50.0/24", "--vnet", "vnew", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert w.path == "/cluster/sdn/vnets/vnew/subnets"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("10.0.40.0",), "CIDR form"),
        (("10.0.40.0/24", "--dhcp-range", "10.0.40.100"), "start-ip:end-ip"),
    ],
    ids=["no-mask", "bad-range"],
)
def test_subnet_add_refuses_bad_input(cli, api, args, message):
    route_sdn(api)

    r = cli("sdn", "subnet", "add", *args, "--vnet", "vsimple", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert message in r.output
    assert api.writes() == []


def test_subnet_add_unknown_vnet(cli, api):
    route_sdn(api)

    r = cli("sdn", "subnet", "add", "10.0.40.0/24", "--vnet", "nope", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "Vnet 'nope' not found" in r.output
    assert api.writes() == []


def test_subnet_add_interactive_then_declined(cli, api, ui):
    route_sdn(api)
    ui.menu("vvlan")
    ui.text("10.0.42.0/24")
    ui.confirm(False)

    r = cli("sdn", "subnet", "add", "-p", "p1")

    assert r.exit_code == 0
    assert "Subnet: 10.0.42.0/24" in r.stdout
    assert api.writes() == []


def test_subnet_remove_uses_the_api_id_not_the_cidr(cli, api):
    route_sdn(api, subnets={"vsimple": [SUBNET]})

    r = cli("sdn", "subnet", "remove", "10.0.30.0/24", "--vnet", "vsimple", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert (w.method, w.path) == ("DELETE", "/cluster/sdn/vnets/vsimple/subnets/zsimple-10.0.30.0-24")


def test_subnet_remove_unknown_cidr_lists_the_available_ones(cli, api):
    route_sdn(api, subnets={"vsimple": [SUBNET]})

    r = cli("sdn", "subnet", "remove", "10.0.99.0/24", "--vnet", "vsimple", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "Available: 10.0.30.0/24" in r.output
    assert api.writes() == []


def test_subnet_remove_on_a_vnet_without_subnet(cli, api):
    route_sdn(api)

    r = cli("sdn", "subnet", "remove", "--vnet", "vvlan", "-y", "-p", "p1")

    assert r.exit_code == 0
    assert "Vnet 'vvlan' has no subnet" in r.stdout
    assert api.writes() == []


def test_subnet_remove_interactive_then_declined(cli, api, ui):
    route_sdn(api, subnets={"vsimple": [SUBNET]})
    ui.menu("vsimple", "10.0.30.0/24")
    ui.confirm(False)

    r = cli("sdn", "subnet", "remove", "-p", "p1")

    assert r.exit_code == 0
    assert api.writes() == []


# ── unlock / discard ─────────────────────────────────────────────────────


def test_unlock_forces_the_release(cli, api):
    route_sdn(api)

    r = cli("sdn", "unlock", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert (w.method, w.path) == ("DELETE", "/cluster/sdn/lock")
    assert w.params == {"force": 1}
    assert "SDN lock released" in r.stdout


def test_unlock_on_91_has_nothing_to_release(cli, api):
    api.route("DELETE", "/cluster/sdn/lock", api.not_implemented("DELETE", "/cluster/sdn/lock"))

    r = cli("sdn", "unlock", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "no global SDN lock" in r.stdout


def test_unlock_declined_writes_nothing(cli, api, ui):
    route_sdn(api)
    ui.confirm(False)

    r = cli("sdn", "unlock", "-p", "p1")

    assert r.exit_code == 0
    assert api.writes() == []


def route_one_pending(api: FakeAPI) -> None:
    route_sdn(api)
    api.route("GET", "/cluster/sdn/zones", lambda c: (
        [{"zone": "znew", "type": "simple", "state": "new"}] if c.params else []
    ))
    api.route("GET", "/cluster/sdn/vnets", lambda c: (
        [{"vnet": "vnew", "zone": "znew", "state": "new"}] if c.params else []
    ))


def test_discard_rolls_back_every_pending_change(cli, api):
    route_one_pending(api)

    r = cli("sdn", "discard", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "zone znew (new)" in r.stdout
    assert "vnet vnew (new)" in r.stdout
    [w] = api.writes()
    assert (w.method, w.path) == ("POST", "/cluster/sdn/rollback")
    assert "2 pending change(s) dropped" in r.stdout


def test_discard_declined_writes_nothing(cli, api, ui):
    route_one_pending(api)
    ui.confirm(False)

    r = cli("sdn", "discard", "-p", "p1")

    assert r.exit_code == 0
    assert api.writes() == []


def test_discard_with_nothing_pending(cli, api):
    route_sdn(api)

    r = cli("sdn", "discard", "-y", "-p", "p1")

    assert r.exit_code == 0
    assert "Nothing pending" in r.stdout
    assert api.writes() == []


def test_discard_on_a_version_without_rollback(cli, api):
    route_one_pending(api)
    api.route("POST", "/cluster/sdn/rollback", api.not_implemented("POST", "/cluster/sdn/rollback"))

    r = cli("sdn", "discard", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "remove each pending object" in flat(r.output)


# ── apply: after the reload ──────────────────────────────────────────────


def route_apply(api: FakeAPI) -> None:
    route_one_pending(api)
    api.route("GET", "/cluster/config/totem", {"cluster_name": "lab"})
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"}])
    api.route("POST", "/cluster/sdn/lock", api.not_implemented("POST", "/cluster/sdn/lock"))
    api.route("PUT", "/cluster/sdn", upid("pve1", "reloadnetworkall"))
    api.route("GET", "/nodes/{node}/sdn/zones", [{"zone": "znew", "status": "available"}])


def test_apply_waits_until_every_vnet_is_available(cli, api):
    route_apply(api)
    api.route("GET", "/cluster/sdn/vnets", lambda c: (
        [{"vnet": "vnew", "zone": "znew", "state": "new"}] if c.params else [{"vnet": "vnew", "zone": "znew"}]
    ))
    polls = {"n": 0}

    def content(call):
        polls["n"] += 1
        # pve2 catches up after a few rounds
        ready = polls["n"] > 4 or call.match["node"] == "pve1"
        return [{"vnet": "vnew", "status": "available" if ready else "pending"}]

    api.route("GET", "/nodes/{node}/sdn/zones/{zone}/content", content)

    r = cli("sdn", "apply", "-y", "--no-diff", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "Every vnet is available on the 2 node(s)" in r.stdout
    assert api.called("GET", "/cluster/sdn/dry-run") == []


def test_apply_warns_when_a_vnet_never_comes_up(cli, api):
    route_apply(api)
    api.route("GET", "/cluster/sdn/vnets", lambda c: (
        [{"vnet": "vnew", "zone": "znew", "state": "new"}] if c.params else [{"vnet": "vnew", "zone": "znew"}]
    ))
    api.route("GET", "/nodes/{node}/sdn/zones/{zone}/content", lambda c: (
        [{"vnet": "vnew", "status": "available"}] if c.match["node"] == "pve1" else []
    ))

    r = cli("sdn", "apply", "-y", "--no-diff", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "still not available: vnew on pve2" in flat(r.output)
    # Polled 12 times, never forever
    assert len(api.called("GET", "/nodes/pve2/sdn/zones")) == 12


def test_apply_reports_a_failed_reload_task(cli, api):
    route_apply(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status", {"status": "stopped", "exitstatus": "command 'ifreload -a' failed"})

    r = cli("sdn", "apply", "-y", "--no-diff", "-p", "p1")

    assert r.exit_code == 1
    assert "SDN configuration applied" not in r.stdout
    assert "ifreload" in r.output


def test_apply_force_regenerates_with_nothing_pending(cli, api):
    route_apply(api)
    api.route("GET", "/cluster/sdn/zones", [])
    api.route("GET", "/cluster/sdn/vnets", [])

    r = cli("sdn", "apply", "--force", "-y", "--no-diff", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "forced regeneration" in r.stdout
    assert api.called("PUT", "/cluster/sdn")


def test_apply_without_online_node_writes_nothing(cli, api):
    route_apply(api)
    api.route("GET", "/nodes", [{"node": "pve1", "status": "offline"}])

    r = cli("sdn", "apply", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "No online node" in r.output
    assert api.writes() == []


def test_vnet_remove_refused_when_a_guest_cannot_be_checked(cli, api):
    # A guest on an unreachable node may still be bridged on the vnet:
    # "could not read" must never count as "not attached"
    api.route("GET", "/cluster/sdn/vnets", [{"vnet": "vn1", "zone": "z1"}])
    api.route("GET", "/cluster/resources", [{"vmid": 100, "type": "qemu", "node": "pve2", "name": "web"}])
    api.route("GET", "/nodes/{node}/qemu/{vmid}/config", api.error(595, "no route to host"))
    api.route("GET", "/cluster/sdn/vnets/{vnet}/subnets", [])
    api.route("DELETE", "/cluster/sdn/vnets/{vnet}", None)
    r = cli("sdn", "vnet", "remove", "vn1", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "100 on pve2" in r.output
    assert api.writes() == []
