"""pvecli ct edit: each menu ends in the exact config change, resize or pool move PVE expects."""

from __future__ import annotations

import getpass

import pytest

from tests.fake_api import fixture

GUESTS = fixture("pve-9.1/cluster_resources_vm")  # 311: lxc on pve-lab1
CONFIG = fixture("pve-9.1/lxc_config")             # hostname fixct, net0 on vsimple, rootfs local-lvm 1G
NODE = "pve-lab1"
BASE = f"/nodes/{NODE}/lxc/311"

NODE_STORAGE = [
    {"storage": "local", "type": "dir", "content": "backup,import,vztmpl,iso", "active": 1, "enabled": 1},
    {"storage": "local-lvm", "type": "lvmthin", "content": "rootdir,images", "active": 1, "enabled": 1},
]
BRIDGES = [
    {"iface": "vmbr0", "type": "bridge", "options": ["alias LAN"]},
    {"iface": "vsimple", "type": "vnet", "comments": "lab vnet"},
]


def setup(api, config: dict | None = None, resources: list[dict] | None = None) -> None:
    api.route("GET", "/cluster/resources", resources if resources is not None else GUESTS)
    api.route("GET", f"{BASE}/config", config if config is not None else CONFIG)
    api.route("GET", "/cluster/options", {})
    api.route("GET", "/pools", [{"poolid": "prod"}, {"poolid": "old"}])
    api.route("GET", f"/nodes/{NODE}/storage", NODE_STORAGE)
    api.route("GET", f"/nodes/{NODE}/network", BRIDGES)
    api.route("PUT", f"{BASE}/config", None)
    api.route("PUT", f"{BASE}/resize", None)
    api.route("PUT", "/pools/{pool}", None)


def kv(value: str) -> dict[str, str]:
    """'a=1,b=2' -> {'a': '1', 'b': '2'}: order-insensitive comparison."""
    return dict(part.split("=", 1) for part in value.split(","))


def apply(ui, ok: bool = True) -> None:
    ui.menu("Apply")
    ui.confirm(ok)


def config_put(api):
    [put] = api.called("PUT", f"{BASE}/config")
    return put.data


# ── simple fields ───────────────────────────────────────────────────────


def test_cpu_and_memory(cli, api, ui):
    setup(api)
    ui.menu("CPU Cores")
    ui.text("2")
    ui.menu("Memory")
    ui.text("1024")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [(c.method, c.path) for c in api.writes()] == [("PUT", f"{BASE}/config")]
    assert config_put(api) == {"cores": 2, "memory": 1024}
    assert not ui.left()


def test_value_set_back_to_original_is_not_sent(cli, api, ui):
    setup(api)
    ui.menu("Memory")
    ui.text("512")
    ui.menu("Memory")
    ui.text("256")          # the current value: no change left
    ui.menu("Hostname")
    ui.text("web")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"hostname": "web"}


def test_invalid_number_changes_nothing(cli, api, ui):
    setup(api)
    ui.menu("Swap")
    ui.text("lots")
    ui.menu("Cancel")
    r = cli("ct", "edit", "311", "-p", "p1")
    assert "Invalid number" in r.output
    assert api.writes() == []


def test_onboot_bool_is_sent_as_integer(cli, api, ui):
    setup(api)
    ui.menu("Start on boot")
    ui.menu("Yes")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"onboot": 1}


def test_cancel_after_changes_writes_nothing(cli, api, ui):
    setup(api)
    ui.menu("CPU Cores")
    ui.text("4")
    ui.menu("Cancel")
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_escape_writes_nothing(cli, api, ui):
    setup(api)
    ui.menu("CPU Cores")
    ui.text("4")
    ui.menu(None)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_declined_summary_writes_nothing(cli, api, ui):
    setup(api)
    ui.menu("CPU Cores")
    ui.text("4")
    apply(ui, ok=False)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0
    assert "CPU Cores: 1 -> 4" in r.output
    assert api.writes() == []


def test_unknown_container(cli, api, ui):
    setup(api)
    r = cli("ct", "edit", "999", "-p", "p1")
    assert r.exit_code == 1
    assert "999 not found" in r.output
    assert api.writes() == []


def test_container_picked_from_menu(cli, api, ui):
    setup(api)
    ui.menu("311 - fixct")
    ui.menu("CPU Cores")
    ui.text("2")
    apply(ui)
    r = cli("ct", "edit", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"cores": 2}


# ── password ────────────────────────────────────────────────────────────


@pytest.fixture
def passwords(monkeypatch, ui):
    """ct edit reads passwords with getpass.getpass: route it to the script."""
    monkeypatch.setattr(getpass, "getpass", lambda prompt="", *a, **k: ui.text_answer(prompt))
    return ui


def test_password_confirmed_is_sent(cli, api, passwords):
    setup(api)
    passwords.menu("Password")
    passwords.text("s3cret!", "s3cret!")
    apply(passwords)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"password": "s3cret!"}
    assert "s3cret!" not in r.output


@pytest.mark.parametrize("answers", [("abc",), ("s3cret!", "other")], ids=["too-short", "mismatch"])
def test_password_rejected_is_not_sent(cli, api, passwords, answers):
    setup(api)
    passwords.menu("Password")
    passwords.text(*answers)
    passwords.menu("Cancel")
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


# ── pool and tags ───────────────────────────────────────────────────────


def test_pool_move_removes_from_old_and_adds_to_new(cli, api, ui):
    resources = [{**g, "pool": "old"} if g["vmid"] == 311 else g for g in GUESTS]
    setup(api, resources=resources)
    ui.menu("Pool")
    ui.menu("prod")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [(c.method, c.path, c.data) for c in api.writes()] == [
        ("PUT", "/pools/old", {"vms": "311", "delete": 1}),
        ("PUT", "/pools/prod", {"vms": "311", "allow-move": 1}),
    ]


def test_pool_left_to_none(cli, api, ui):
    resources = [{**g, "pool": "old"} if g["vmid"] == 311 else g for g in GUESTS]
    setup(api, resources=resources)
    ui.menu("Pool")
    ui.menu("(none)")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [(c.path, c.data) for c in api.writes()] == [("/pools/old", {"vms": "311", "delete": 1})]


def test_tags_with_custom_one(cli, api, ui):
    setup(api, config={**CONFIG, "tags": "web"}, resources=[{**g, "tags": "web;db"} for g in GUESTS])
    ui.menu("Tags")
    ui.multi(["db", "+ Add custom tag"])
    ui.text("blue")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"tags": "blue;db"}


def test_removing_every_tag_deletes_the_key(cli, api, ui):
    setup(api, config={**CONFIG, "tags": "web;db"})
    ui.menu("Tags")
    ui.multi([])
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"delete": "tags"}


# ── disks ───────────────────────────────────────────────────────────────


def test_resize_rootfs(cli, api, ui):
    setup(api)
    ui.menu("Disks")
    ui.menu("rootfs")
    ui.text("4")
    ui.menu("Back")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [(c.method, c.path, c.data) for c in api.writes()] == [
        ("PUT", f"{BASE}/resize", {"disk": "rootfs", "size": "4G"}),
    ]


def test_resize_with_invalid_size_changes_nothing(cli, api, ui):
    setup(api)
    ui.menu("Disks")
    ui.menu("rootfs")
    ui.text("big")
    ui.menu("Back")
    ui.menu("Cancel")
    r = cli("ct", "edit", "311", "-p", "p1")
    assert "Invalid number" in r.output
    assert api.writes() == []


def test_add_mountpoint(cli, api, ui):
    setup(api)
    ui.menu("Disks")
    ui.menu("Add mountpoint")
    ui.menu("local-lvm")
    ui.number(10)
    ui.text(None)  # default mount path
    ui.menu("Back")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"mp0": "local-lvm:10,mp=/mnt/mp0"}
    # Only storages holding container volumes are offered
    storage_question = next(q for k, q in ui.asked if "Storage" in q)
    assert "'local'" not in storage_question


def test_add_mountpoint_takes_next_free_index(cli, api, ui):
    setup(api, config={**CONFIG, "mp0": "local-lvm:vm-311-disk-1,mp=/data,size=2G"})
    ui.menu("Disks")
    ui.menu("Add mountpoint")
    ui.menu("local-lvm")
    ui.number(5)
    ui.text("/srv")
    ui.menu("Back")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"mp1": "local-lvm:5,mp=/srv"}


def test_remove_mountpoint(cli, api, ui):
    setup(api, config={**CONFIG, "mp0": "local-lvm:vm-311-disk-1,mp=/data,size=2G"})
    ui.menu("Disks")
    ui.menu("Remove mountpoint")
    ui.menu("mp0")
    ui.menu("Back")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"delete": "mp0"}


def test_rootfs_cannot_be_removed(cli, api, ui):
    setup(api)
    ui.menu("Disks")
    ui.menu("Back")
    ui.menu("Cancel")
    cli("ct", "edit", "311", "-p", "p1")
    disks_question = next(q for k, q in ui.asked if "Disks:" in q)
    assert "Remove mountpoint" not in disks_question


def test_mountpoint_added_then_removed_is_not_sent(cli, api, ui):
    setup(api)
    ui.menu("Disks")
    ui.menu("Add mountpoint")
    ui.menu("local-lvm")
    ui.number(10)
    ui.text(None)
    ui.menu("Remove mountpoint")
    ui.menu("mp0")
    ui.menu("Back")
    ui.menu("Cancel")
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    main_menus = [q for k, q in ui.asked if q.startswith("  CT 311")]
    assert "(no changes)" in main_menus[-1]


# ── network ─────────────────────────────────────────────────────────────


def test_edit_nic_keeps_mac_and_type(cli, api, ui):
    setup(api)
    ui.menu("Network")
    ui.menu("net0")
    ui.menu("vmbr0")         # bridge
    ui.menu("static")        # IPv4
    ui.text("10.0.0.5/24", "10.0.0.1", "20")  # address, gateway, VLAN
    ui.menu("Yes")           # firewall
    ui.menu("Back")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    net0 = config_put(api)["net0"]
    assert kv(net0) == {
        "name": "eth0", "bridge": "vmbr0", "hwaddr": "BC:24:11:A6:9E:69", "type": "veth",
        "ip": "10.0.0.5/24", "gw": "10.0.0.1", "tag": "20", "firewall": "1",
    }


def test_edit_nic_to_no_ipv4_drops_ip_and_gateway(cli, api, ui):
    setup(api, config={**CONFIG, "net0": "name=eth0,bridge=vsimple,hwaddr=BC:24:11:A6:9E:69,"
                                         "ip=10.0.0.5/24,gw=10.0.0.1,tag=20,type=veth"})
    ui.menu("Network")
    ui.menu("net0")
    ui.menu(None)            # keep the bridge
    ui.menu("none")          # no IPv4
    ui.text("")              # clear the VLAN
    ui.menu(None)            # firewall unchanged
    ui.menu("Back")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert kv(config_put(api)["net0"]) == {
        "name": "eth0", "bridge": "vsimple", "hwaddr": "BC:24:11:A6:9E:69", "type": "veth",
    }


def test_edit_nic_without_change_is_not_sent(cli, api, ui):
    setup(api)
    ui.menu("Network")
    ui.menu("net0")
    ui.menu(None)
    ui.menu("keep")
    ui.text(None)
    ui.menu(None)
    ui.menu("Back")
    ui.menu("Cancel")
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    main_menus = [q for k, q in ui.asked if q.startswith("  CT 311")]
    assert "(no changes)" in main_menus[-1]


def test_add_nic(cli, api, ui):
    setup(api)
    ui.menu("Network")
    ui.menu("Add NIC")
    ui.menu("vmbr0")
    ui.menu("dhcp")
    ui.text(None)            # no VLAN
    ui.menu("No")            # no firewall
    ui.menu("Back")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"net1": "name=eth1,bridge=vmbr0,ip=dhcp"}
    bridge_question = next(q for k, q in ui.asked if "Bridge:" in q)
    assert "vmbr0  (LAN)" in bridge_question


def test_add_nic_static_with_vlan_and_firewall(cli, api, ui):
    setup(api)
    ui.menu("Network")
    ui.menu("Add NIC")
    ui.menu("vsimple")
    ui.menu("static")
    ui.text("192.168.1.10/24", "", "30")
    ui.menu("Yes")
    ui.menu("Back")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert kv(config_put(api)["net1"]) == {
        "name": "eth1", "bridge": "vsimple", "ip": "192.168.1.10/24", "tag": "30", "firewall": "1",
    }


def test_remove_nic(cli, api, ui):
    setup(api)
    ui.menu("Network")
    ui.menu("Remove NIC")
    ui.menu("net0")
    ui.menu("Back")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"delete": "net0"}


def test_changes_and_deletions_go_in_one_request(cli, api, ui):
    setup(api, config={**CONFIG, "mp0": "local-lvm:vm-311-disk-1,mp=/data,size=2G"})
    ui.menu("CPU Cores")
    ui.text("2")
    ui.menu("Network")
    ui.menu("Remove NIC")
    ui.menu("net0")
    ui.menu("Back")
    ui.menu("Disks")
    ui.menu("Remove mountpoint")
    ui.menu("mp0")
    ui.menu("rootfs")
    ui.text("2")
    ui.menu("Back")
    apply(ui)
    r = cli("ct", "edit", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [(c.method, c.path, c.data) for c in api.writes()] == [
        ("PUT", f"{BASE}/config", {"cores": 2, "delete": "mp0,net0"}),
        ("PUT", f"{BASE}/resize", {"disk": "rootfs", "size": "2G"}),
    ]
