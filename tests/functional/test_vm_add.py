"""vm add, with options and through the wizard: the exact VM config posted to Proxmox."""

from __future__ import annotations

import pytest

from tests.fake_api import fixture, upid

NODES = fixture("pve-9.1/nodes")  # pve-lab1 .. pve-lab5
GUESTS = [dict(g) for g in fixture("pve-9.1/cluster_resources_vm")]
GUESTS[0]["tags"] = "web"

STORAGES = [
    {"storage": "local", "type": "dir", "content": "iso,vztmpl,backup,images", "active": 1, "enabled": 1},
    {"storage": "local-lvm", "type": "lvmthin", "content": "rootdir,images", "active": 1, "enabled": 1},
    {"storage": "pbs-lab", "type": "pbs", "content": "backup", "active": 1, "enabled": 1},
]
ISOS = [
    {"volid": "local:iso/debian-13.iso", "content": "iso", "format": "iso", "size": 790626304},
    {"volid": "local:iso/virtio-win.iso", "content": "iso", "format": "iso", "size": 753565696},
]
NETWORK = [
    {"iface": "eno1", "type": "eth"},
    {"iface": "vmbr0", "type": "bridge", "options": ["alias LAN"]},
]


def setup(api, nodes: list[dict] | None = None, pools: list[str] | None = None) -> None:
    api.route("GET", "/nodes", nodes if nodes is not None else NODES)
    api.route("GET", "/cluster/nextid", "105")
    api.route("GET", "/cluster/resources", GUESTS)
    api.route("GET", "/cluster/options", {})
    api.route("GET", "/pools", [{"poolid": p} for p in (pools or [])])
    api.route("GET", "/nodes/{node}/storage", STORAGES)
    api.route("GET", "/nodes/{node}/storage/local/content", ISOS)
    api.route("GET", "/nodes/{node}/network", NETWORK)
    api.route("POST", "/nodes/{node}/qemu", lambda c: upid(c.match["node"], "qmcreate", c.data["vmid"]))


def created(api):
    [post] = api.writes()
    assert post.method == "POST"
    return post


LINUX = ("--name", "web", "--iso-storage", "local", "--iso", "debian-13.iso", "--os-type", "linux")


# ── with options ────────────────────────────────────────────────────────


def test_linux_with_the_required_options(cli, api):
    setup(api)
    r = cli("vm", "add", "pve-lab1", *LINUX, "-p", "p1")
    assert r.exit_code == 0, r.output
    post = created(api)
    assert post.path == "/nodes/pve-lab1/qemu"
    assert post.data == {
        "vmid": 105, "name": "web", "onboot": 0, "agent": 1,
        "ide2": "local:iso/debian-13.iso,media=cdrom", "ostype": "l26",
        "sockets": 1, "cores": 2, "cpu": "x86-64-v2-AES", "memory": 2048, "balloon": 2048,
    }


def test_linux_full_options(cli, api):
    setup(api)
    r = cli("vm", "add", "pve-lab1", *LINUX, "--vmid", "120", "--pool", "dev", "--onboot", "--no-agent",
            "--sockets", "2", "--cores", "4", "--vcpus", "6", "--cpu-type", "host", "--memory", "4096",
            "--disk-storage", "local", "--disk-size", "40", "--disk-format", "qcow2",
            "--bridge", "vmbr0", "--vlan", "20", "--firewall", "--link-down", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).data == {
        "vmid": 120, "name": "web", "pool": "dev", "onboot": 1, "agent": 0,
        "ide2": "local:iso/debian-13.iso,media=cdrom", "ostype": "l26",
        "sockets": 2, "cores": 4, "vcpus": 6, "cpu": "host", "memory": 4096, "balloon": 4096,
        "virtio0": "local:40,format=qcow2",
        "net0": "virtio,bridge=vmbr0,tag=20,firewall=1,link_down=1",
    }


def test_vcpus_above_sockets_times_cores_is_capped(cli, api):
    setup(api)
    r = cli("vm", "add", "pve-lab1", *LINUX, "--sockets", "1", "--cores", "2", "--vcpus", "8", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).data["vcpus"] == 2


def test_windows_11_needs_tpm_and_efi(cli, api):
    setup(api)
    r = cli("vm", "add", "pve-lab1", "--name", "win", "--iso-storage", "local", "--iso", "w11.iso",
            "--os-type", "windows", "--efi-storage", "local-lvm", "-p", "p1")
    assert r.exit_code == 1
    assert "--tpm-storage" in r.output
    assert api.writes() == []


def test_windows_11_full(cli, api):
    setup(api)
    r = cli("vm", "add", "pve-lab1", "--name", "win", "--iso-storage", "local", "--iso", "w11.iso",
            "--os-type", "windows", "--os-version", "11/2022/2025",
            "--virtio-iso-storage", "local", "--virtio-iso", "virtio-win.iso",
            "--tpm-storage", "local-lvm", "--efi-storage", "local-lvm",
            "--disk-storage", "local", "--disk-size", "64", "-p", "p1")
    assert r.exit_code == 0, r.output
    data = created(api).data
    assert data["ostype"] == "win11"
    assert data["ide3"] == "local:iso/virtio-win.iso,media=cdrom"
    assert data["tpmstate0"] == "local-lvm:1,version=v2.0"
    assert data["efidisk0"] == "local-lvm:1,efitype=4m,pre-enrolled-keys=1"
    assert data["bios"] == "ovmf"
    # Windows has no virtio-blk driver at install time: SCSI on virtio-scsi
    assert data["scsi0"] == "local:64,format=qcow2"
    assert data["scsihw"] == "virtio-scsi-pci"
    assert "virtio0" not in data


@pytest.mark.parametrize(("version", "ostype"), [("10/2016/2019", "win10"), ("8.x/2012/2012r2", "win8"),
                                                 ("XP/2003", "wxp"), ("2000", "w2k")])
def test_windows_versions(cli, api, version, ostype):
    setup(api)
    r = cli("vm", "add", "pve-lab1", "--name", "win", "--iso-storage", "local", "--iso", "w.iso",
            "--os-type", "windows", "--os-version", version, "-p", "p1")
    assert r.exit_code == 0, r.output
    data = created(api).data
    assert data["ostype"] == ostype
    assert "tpmstate0" not in data


# The labels are the PVE GUI ones (pve-manager kvm_ostypes): 7/2008r2 is win7,
# Vista/2008 is w2k8.
@pytest.mark.parametrize(("version", "ostype"), [("7/2008r2", "win7"), ("Vista/2008", "w2k8")])
def test_windows_2008_versions(cli, api, version, ostype):
    setup(api)
    r = cli("vm", "add", "pve-lab1", "--name", "win", "--iso-storage", "local", "--iso", "w.iso",
            "--os-type", "windows", "--os-version", version, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).data["ostype"] == ostype


def test_default_disk_format_on_lvmthin(cli, api):
    setup(api)
    r = cli("vm", "add", "pve-lab1", *LINUX, "--disk-storage", "local-lvm", "--disk-size", "32", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "format=qcow2" not in created(api).data["virtio0"]


@pytest.mark.parametrize("vlan", ["0", "4095", "abc"])
def test_invalid_vlan_is_refused_before_any_call(cli, api, vlan):
    setup(api)
    r = cli("vm", "add", "pve-lab1", *LINUX, "--bridge", "vmbr0", "--vlan", vlan, "-p", "p1")
    assert r.exit_code == 1
    assert "VLAN" in r.output
    assert api.calls == []


def test_unknown_node_writes_nothing(cli, api):
    setup(api)
    r = cli("vm", "add", "pve-nope", *LINUX, "-p", "p1")
    assert r.exit_code == 1
    assert "pve-nope" in r.output
    assert api.writes() == []


def test_node_menu_when_several_nodes(cli, api, ui):
    setup(api)
    ui.menu("pve-lab3")
    r = cli("vm", "add", *LINUX, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).path == "/nodes/pve-lab3/qemu"


def test_node_menu_cancelled_writes_nothing(cli, api, ui):
    setup(api)
    ui.menu(None)
    r = cli("vm", "add", *LINUX, "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_single_node_is_used_without_asking(cli, api, ui):
    setup(api, nodes=[NODES[0]])
    r = cli("vm", "add", *LINUX, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).path == f"/nodes/{NODES[0]['node']}/qemu"
    assert ui.asked == []


def test_failed_create_task_is_an_error(cli, api):
    setup(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "unable to create VM 105 - VM 105 already exists on node 'pve-lab2'"})
    r = cli("vm", "add", "pve-lab1", *LINUX, "-p", "p1")
    assert r.exit_code == 1
    assert "already exists" in r.output


# ── wizard ──────────────────────────────────────────────────────────────


def linux_wizard(ui) -> None:
    ui.text(None, "web")            # VMID (default), name
    ui.menu("(none)")               # pool
    ui.multi(["web"])               # tags
    ui.confirm(False)               # onboot
    ui.menu("local", "debian-13")   # ISO storage, ISO
    ui.menu("Linux", "6.x")         # OS type, kernel
    ui.number(1, 2, None)           # sockets, cores, vCPUs (all)
    ui.menu("host")                 # CPU type
    ui.number(4096)                 # RAM
    ui.confirm(True)                # primary disk
    ui.menu("local-lvm")
    ui.number(20)
    ui.menu("raw")
    ui.menu("vmbr0")
    ui.text("")                     # VLAN
    ui.confirm(True, False)         # firewall, start disconnected


def test_wizard_linux(cli, api, ui):
    setup(api, pools=["dev"])
    linux_wizard(ui)
    ui.confirm(True, True)  # create, print the command
    r = cli("vm", "add", "pve-lab1", "-p", "p1")
    assert r.exit_code == 0, r.output
    post = created(api)
    assert post.path == "/nodes/pve-lab1/qemu"
    assert post.data == {
        "vmid": 105, "name": "web", "tags": "web", "onboot": 0,
        "ide2": "local:iso/debian-13.iso,media=cdrom", "ostype": "l26", "agent": 1,
        "sockets": 1, "cores": 2, "cpu": "host", "memory": 4096, "balloon": 4096,
        "virtio0": "local-lvm:20,format=raw", "net0": "virtio,bridge=vmbr0,firewall=1",
    }
    out = r.stdout.replace("\\\n", " ")
    for part in ("pvecli vm add pve-lab1", "--vmid 105", "--name web", "--iso-storage local",
                 "--iso debian-13.iso", "--os-type linux", "--cpu-type host", "--memory 4096",
                 "--disk-storage local-lvm", "--disk-size 20", "--disk-format raw",
                 "--bridge vmbr0", "--firewall", "-p p1"):
        assert part in out
    assert not ui.left()


def test_wizard_declined_writes_nothing(cli, api, ui):
    setup(api, pools=["dev"])
    linux_wizard(ui)
    ui.confirm(False)
    r = cli("vm", "add", "pve-lab1", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_wizard_options_skip_their_questions(cli, api, ui):
    setup(api)
    ui.text("130")                  # VMID: name, ISO, CPU, RAM, disk, network are given
    ui.multi([])                    # no tag
    ui.menu("Linux", "6.x")
    ui.number(None)                 # vCPUs
    ui.menu("x86-64")               # CPU type: not covered by --sockets/--cores
    ui.confirm(True, False)         # create, no command
    r = cli("vm", "add", "pve-lab1", "--name", "db", "--iso-storage", "local", "--iso", "debian-13.iso",
            "--no-onboot", "--sockets", "1", "--cores", "4", "--memory", "1024",
            "--disk-storage", "local", "--disk-size", "10", "--bridge", "vmbr0", "--vlan", "30", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).data == {
        "vmid": 130, "name": "db", "onboot": 0, "ide2": "local:iso/debian-13.iso,media=cdrom",
        "ostype": "l26", "agent": 1, "sockets": 1, "cores": 4, "cpu": "x86-64-v2-AES",
        "memory": 1024, "balloon": 1024, "virtio0": "local:10,format=qcow2",
        "net0": "virtio,bridge=vmbr0,tag=30",
    }
    assert not ui.left()


def test_wizard_empty_name_is_asked_again(cli, api, ui):
    setup(api)
    ui.text(None, "", "  ", "web")
    ui.multi([])
    ui.confirm(False)
    ui.menu(None)  # ISO storage: Escape stops the wizard
    r = cli("vm", "add", "pve-lab1", "-p", "p1")
    assert r.exit_code == 1
    assert r.output.count("cannot be empty") == 2
    assert api.writes() == []


def test_wizard_without_iso_storage_stops(cli, api, ui):
    setup(api)
    api.route("GET", "/nodes/{node}/storage", [s for s in STORAGES if "iso" not in s["content"]])
    ui.text(None, "web")
    ui.multi([])
    ui.confirm(False)
    r = cli("vm", "add", "pve-lab1", "-p", "p1")
    assert r.exit_code == 1
    assert "No storage with ISO content" in r.output
    assert api.writes() == []


def test_wizard_windows_11(cli, api, ui):
    setup(api)
    ui.text(None, "win")
    ui.multi([])
    ui.confirm(True)                         # onboot
    ui.menu("local", "debian-13")            # install ISO
    ui.menu("Windows", "11/2022/2025")
    ui.confirm(True)                         # VirtIO drivers ISO
    ui.menu("local", "virtio-win")
    ui.menu("local-lvm", "local-lvm")        # TPM, EFI
    ui.number(1, 4, None)
    ui.menu("x86-64")
    ui.number(8192)
    ui.confirm(True)                         # primary disk
    ui.menu("local")
    ui.number(64)
    ui.menu("qcow2")
    ui.menu(None)                            # no bridge
    ui.confirm(True, False)                  # create, no command
    r = cli("vm", "add", "pve-lab1", "-p", "p1")
    assert r.exit_code == 0, r.output
    data = created(api).data
    assert data["ostype"] == "win11"
    assert data["onboot"] == 1
    assert data["ide3"] == "local:iso/virtio-win.iso,media=cdrom"
    assert data["tpmstate0"] == "local-lvm:1,version=v2.0"
    assert data["efidisk0"] == "local-lvm:1,efitype=4m,pre-enrolled-keys=1"
    assert data["bios"] == "ovmf"
    assert data["scsi0"] == "local:64,format=qcow2"
    assert data["scsihw"] == "virtio-scsi-pci"
    assert "net0" not in data
    assert not ui.left()


def wizard_up_to_os(ui) -> None:
    ui.text(None, "x")
    ui.multi([])
    ui.confirm(False)
    ui.menu("local", "debian-13")


def wizard_after_os(ui) -> None:
    ui.number(1, 2, None)
    ui.menu("x86-64")
    ui.number(2048)
    ui.confirm(False)       # no disk
    ui.menu(None)           # no bridge
    ui.confirm(True, False)


def test_wizard_windows_vista_is_w2k8(cli, api, ui):
    setup(api)
    wizard_up_to_os(ui)
    ui.menu("Windows", "Vista/2008")
    ui.confirm(False)       # no VirtIO ISO
    wizard_after_os(ui)
    r = cli("vm", "add", "pve-lab1", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).data["ostype"] == "w2k8"


def test_wizard_escape_on_kernel_never_picks_l24(cli, api, ui):
    setup(api)
    wizard_up_to_os(ui)
    ui.menu("Linux", None)
    wizard_after_os(ui)
    cli("vm", "add", "pve-lab1", "-p", "p1")
    assert all(c.data.get("ostype") != "l24" for c in api.writes())
