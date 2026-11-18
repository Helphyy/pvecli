"""vm edit menus: CPU, memory, options, pool, tags, boot order, disks and network, and the exact changes applied."""

from __future__ import annotations

from tests.fake_api import fixture

GUESTS = [dict(g) for g in fixture("pve-9.1/cluster_resources_vm")]
GUESTS[0]["pool"] = "dev"  # 300, on pve-lab2
GUESTS[0]["tags"] = "web"
GUESTS[1]["tags"] = "db;prod"  # 301

# The recorded config of 300, with a disk and a CD drive as the PVE GUI creates them.
# PVE returns memory as a string.
CONFIG = {
    **fixture("pve-9.1/qemu_config"),
    "boot": "order=scsi0;net0",
    "cores": 2,
    "sockets": 1,
    "memory": "2048",
    "scsihw": "virtio-scsi-single",
    "scsi0": "local-lvm:vm-300-disk-0,iothread=1,size=32G",
    "ide2": "local:iso/debian-13.iso,media=cdrom,size=754M",
    "tags": "web",
}
MAC = "virtio=BC:24:11:EE:5A:C4"  # net0 of the fixture

STORAGES = [
    {"storage": "local", "type": "dir", "content": "iso,vztmpl,backup,images", "active": 1, "enabled": 1},
    {"storage": "local-lvm", "type": "lvmthin", "content": "rootdir,images", "active": 1, "enabled": 1},
]
ISOS = [
    {"volid": "local:iso/debian-13.iso", "content": "iso", "format": "iso", "size": 790626304},
    {"volid": "local:iso/virtio-win.iso", "content": "iso", "format": "iso", "size": 753565696},
]
NETWORK = [
    {"iface": "eno1", "type": "eth"},
    {"iface": "vmbr0", "type": "bridge", "options": ["alias LAN"]},
    {"iface": "vsimple", "type": "vnet", "comments": "sdn simple"},
]

BASE = "/nodes/pve-lab2/qemu/300"


def setup(api, config: dict | None = None) -> None:
    api.route("GET", "/cluster/resources", GUESTS)
    api.route("GET", f"{BASE}/config", config if config is not None else CONFIG)
    api.route("GET", "/pools", [{"poolid": "dev"}, {"poolid": "prod"}])
    api.route("GET", "/cluster/options", {})
    api.route("GET", "/nodes/pve-lab2/storage", STORAGES)
    api.route("GET", "/nodes/pve-lab2/storage/local/content", ISOS)
    api.route("GET", "/nodes/pve-lab2/network", NETWORK)
    api.route("PUT", f"{BASE}/config", None)
    api.route("PUT", f"{BASE}/resize", None)
    api.route("PUT", "/pools/{pool}", None)


def edit(cli, *extra: str):
    return cli("vm", "edit", "300", "-p", "p1", *extra)


def config_put(api):
    [put] = api.called("PUT", f"{BASE}/config")
    return put.data


def apply(ui) -> None:
    ui.menu("Apply")
    ui.confirm(True)


# ── simple fields ───────────────────────────────────────────────────────


def test_cores_and_memory(cli, api, ui):
    setup(api)
    ui.menu("CPU Cores").text("4")
    ui.menu("Memory (MB)").text("4096")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert api.writes()[0].path == f"{BASE}/config"
    assert config_put(api) == {"cores": 4, "memory": 4096}
    assert len(api.writes()) == 1
    assert not ui.left()


def test_name_and_description(cli, api, ui):
    setup(api)
    ui.menu("Name").text("web-01")
    ui.menu("Description").text("front")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"name": "web-01", "description": "front"}


def test_invalid_number_is_not_applied(cli, api, ui):
    setup(api)
    ui.menu("CPU Cores").text("four")
    ui.menu("Cancel")
    r = edit(cli)
    assert "Invalid number" in r.output
    assert api.writes() == []


def test_start_on_boot(cli, api, ui):
    setup(api)
    ui.menu("Start on boot").menu("Yes")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"onboot": 1}


def test_vcpus_within_sockets_times_cores(cli, api, ui):
    setup(api)
    ui.menu("vCPUs").text("1")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"vcpus": 1}


def test_vcpus_above_sockets_times_cores_is_refused(cli, api, ui):
    setup(api)
    ui.menu("vCPUs").text("3")
    ui.menu("Cancel")
    r = edit(cli)
    assert "between 0 and sockets x cores (2)" in r.output
    assert api.writes() == []


def test_vcpus_zero_deletes_the_key(cli, api, ui):
    setup(api, {**CONFIG, "vcpus": 1})
    ui.menu("vCPUs").text("0")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    # Back to "all cores": the key goes, it is not set to 0
    assert config_put(api) == {"delete": "vcpus"}


def test_value_set_back_to_the_original_is_not_a_change(cli, api, ui):
    setup(api)
    ui.menu("CPU Cores").text("4")
    ui.menu("CPU Cores").text("2")
    ui.menu(15)  # the Apply slot, "(no changes)" now
    ui.menu("Cancel")
    ui.confirm(True)
    edit(cli)
    assert api.writes() == []


def test_memory_kept_as_is_is_not_a_change(cli, api, ui):
    setup(api)
    ui.menu("Memory (MB)").text(None)  # Enter: keep the current value
    ui.menu(15)  # the Apply slot: "(no changes)" when nothing changed
    ui.menu("Cancel")
    ui.confirm(True)
    edit(cli)
    assert api.writes() == []


# ── cancellation ────────────────────────────────────────────────────────


def test_cancel_entry_writes_nothing(cli, api, ui):
    setup(api)
    ui.menu("CPU Cores").text("4")
    ui.menu("Cancel")
    r = edit(cli)
    assert r.exit_code == 0
    assert api.writes() == []


def test_escape_writes_nothing(cli, api, ui):
    setup(api)
    ui.menu("CPU Cores").text("4")
    ui.menu(None)
    r = edit(cli)
    assert r.exit_code == 0
    assert api.writes() == []


def test_declined_summary_writes_nothing(cli, api, ui):
    setup(api)
    ui.menu("CPU Cores").text("4")
    ui.menu("Disks").menu("scsi0").text("64").menu("Back")
    ui.menu("Pool").menu("prod")
    ui.menu("Apply").confirm(False)
    r = edit(cli)
    assert r.exit_code == 0
    assert api.writes() == []


def test_vm_picked_from_the_menu(cli, api, ui):
    setup(api)
    ui.menu("300")
    ui.menu("Cancel")
    r = cli("vm", "edit", "-p", "p1")
    assert r.exit_code == 0
    assert "guest-sdn" in ui.asked[1][1]
    assert api.writes() == []


def test_unknown_vm(cli, api, ui):
    setup(api)
    r = cli("vm", "edit", "999", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


# ── pool and tags ───────────────────────────────────────────────────────


def test_pool_move(cli, api, ui):
    setup(api)
    ui.menu("Pool").menu("prod")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    writes = [(c.path, c.data) for c in api.writes()]
    assert writes == [
        ("/pools/dev", {"vms": "300", "delete": 1}),
        ("/pools/prod", {"vms": "300", "allow-move": 1}),
    ]


def test_pool_removed(cli, api, ui):
    setup(api)
    ui.menu("Pool").menu("(none)")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert [(c.path, c.data) for c in api.writes()] == [("/pools/dev", {"vms": "300", "delete": 1})]


def test_tags_replaced_sorted(cli, api, ui):
    setup(api)
    ui.menu("Tags").multi(["prod", "db"])
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"tags": "db;prod"}


def test_tags_custom(cli, api, ui):
    setup(api)
    ui.menu("Tags").multi(["web", "+ Add custom tag"]).text("zz")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"tags": "web;zz"}


# ── boot order ──────────────────────────────────────────────────────────


def test_boot_order_move_and_add(cli, api, ui, monkeypatch):
    import src.cli.vm as vm

    # 1. scsi0, 2. net0, separator, + Add device, Back; one more line once ide2 is added
    keys = iter([(1, "u"), (3, "enter"), (5, "enter")])
    monkeypatch.setattr(vm, "reorder_menu", lambda items, title: next(keys))
    setup(api)
    ui.menu("Boot Order")
    ui.menu("ide2")  # the device to add
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"boot": "order=net0;scsi0;ide2"}


def test_boot_order_remove_entry(cli, api, ui, monkeypatch):
    import src.cli.vm as vm

    keys = iter([(1, "r"), (None, None)])
    monkeypatch.setattr(vm, "reorder_menu", lambda items, title: next(keys))
    setup(api)
    ui.menu("Boot Order")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"boot": "order=scsi0"}


# ── disks ───────────────────────────────────────────────────────────────


def test_disk_resize(cli, api, ui):
    setup(api)
    ui.menu("Disks").menu("scsi0").text("64").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.path == f"{BASE}/resize"
    assert put.data == {"disk": "scsi0", "size": "64G"}


def test_disk_resize_empty_answer_changes_nothing(cli, api, ui):
    setup(api)
    ui.menu("Disks").menu("scsi0").text(None).menu("Back")
    ui.menu("Cancel")
    edit(cli)
    assert api.writes() == []


def test_disk_add_on_file_storage(cli, api, ui):
    setup(api)
    ui.menu("Disks").menu("Add disk").menu("scsi").menu("local").number(16).menu("qcow2").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    # scsi0 is taken; the existing controller is kept
    assert config_put(api) == {"scsi1": "local:16,format=qcow2"}


def test_disk_add_virtio_sets_no_scsi_controller(cli, api, ui):
    cfg = {k: v for k, v in CONFIG.items() if k != "scsihw"}
    setup(api, cfg)
    ui.menu("Disks").menu("Add disk").menu("virtio").menu("local").number(8).menu("raw").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"virtio0": "local:8,format=raw"}


def test_disk_add_scsi_without_controller_sets_one(cli, api, ui):
    cfg = {k: v for k, v in CONFIG.items() if k != "scsihw"}
    setup(api, cfg)
    ui.menu("Disks").menu("Add disk").menu("scsi").menu("local").number(8).menu("raw").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"scsi1": "local:8,format=raw", "scsihw": "virtio-scsi-pci"}


def test_disk_add_on_lvmthin_is_raw(cli, api, ui):
    setup(api)
    ui.menu("Disks").menu("Add disk").menu("scsi").menu("local-lvm").number(16).menu(0).menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert "format=qcow2" not in config_put(api)["scsi1"]


def test_disk_remove_detaches_it(cli, api, ui):
    setup(api)
    ui.menu("Disks").menu("Remove disk").menu("scsi0").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    # PVE turns a deleted disk key into unusedN: the volume is kept
    assert config_put(api) == {"delete": "scsi0"}


def test_disk_remove_cancel_changes_nothing(cli, api, ui):
    setup(api)
    ui.menu("Disks").menu("Remove disk").menu("Cancel").menu("Back")
    ui.menu("Cancel")
    edit(cli)
    assert api.writes() == []


def test_disk_and_nic_removed_together_in_one_delete(cli, api, ui):
    setup(api)
    ui.menu("Disks").menu("Remove disk").menu("scsi0").menu("Back")
    ui.menu("Network").menu("Remove NIC").menu("net0").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"delete": "net0,scsi0"}


def test_eject_cdrom(cli, api, ui):
    setup(api)
    ui.menu("Disks").menu("Eject CDROM").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"ide2": "none,media=cdrom"}


def test_change_iso_of_the_cd_drive(cli, api, ui):
    setup(api)
    ui.menu("Disks").menu("ide2").menu("Change ISO").menu("local").menu("virtio-win").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"ide2": "local:iso/virtio-win.iso,media=cdrom"}


def test_mount_iso_on_a_vm_without_cd_drive(cli, api, ui):
    setup(api, {k: v for k, v in CONFIG.items() if k != "ide2"})
    ui.menu("Disks").menu("Mount ISO").menu("local").menu("virtio-win").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"ide2": "local:iso/virtio-win.iso,media=cdrom"}


def test_mount_iso_never_replaces_a_data_disk(cli, api, ui):
    cfg = {**CONFIG, "ide2": "local-lvm:vm-300-disk-1,size=8G"}
    setup(api, cfg)
    ui.menu("Disks").menu("Mount ISO").menu("local").menu("virtio-win").menu("Back")
    apply(ui)
    edit(cli)
    for put in api.called("PUT", f"{BASE}/config"):
        assert "ide2" not in put.data


# ── network ─────────────────────────────────────────────────────────────


def test_nic_add(cli, api, ui):
    setup(api)
    ui.menu("Network").menu("Add NIC").menu("vmbr0").text("20").menu("Yes").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"net1": "virtio,bridge=vmbr0,tag=20,firewall=1"}


def test_nic_edit_keeps_the_mac_address(cli, api, ui):
    setup(api)
    ui.menu("Network").menu("net0").menu("vmbr0").text("30").menu("No").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    # Losing the MAC would hand the guest a new one: DHCP leases and licences break
    assert config_put(api) == {"net0": f"{MAC},bridge=vmbr0,tag=30"}


def test_nic_edit_drops_vlan_and_firewall(cli, api, ui):
    setup(api, {**CONFIG, "net0": f"{MAC},bridge=vmbr0,firewall=1,tag=30"})
    ui.menu("Network").menu("net0").menu("vmbr0").text("").menu("No").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"net0": f"{MAC},bridge=vmbr0"}


def test_nic_edit_unchanged_is_not_a_change(cli, api, ui):
    setup(api)
    ui.menu("Network").menu("net0").menu("vsimple").text("").menu("No").menu("Back")
    ui.menu("Cancel")
    edit(cli)
    assert api.writes() == []


def test_nic_remove(cli, api, ui):
    setup(api)
    ui.menu("Network").menu("Remove NIC").menu("net0").menu("Back")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 0, r.output
    assert config_put(api) == {"delete": "net0"}


def test_api_error_is_reported(cli, api, ui):
    setup(api)
    api.route("PUT", f"{BASE}/config", api.error(500, "VM is locked (backup)"))
    ui.menu("CPU Cores").text("4")
    apply(ui)
    r = edit(cli)
    assert r.exit_code == 1
    assert "locked" in r.output
