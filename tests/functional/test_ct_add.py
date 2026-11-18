"""pvecli ct add: the creation request built from flags, and from the wizard."""

from __future__ import annotations

import getpass

import pytest

from tests.fake_api import upid

NODES = [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"}]
DEBIAN = "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
ALPINE = "local:vztmpl/alpine-3.20-default_20240908_amd64.tar.xz"
TEMPLATES = [
    {"volid": ALPINE, "content": "vztmpl", "format": "txz", "size": 3000000},
    {"volid": DEBIAN, "content": "vztmpl", "format": "tzst", "size": 120000000},
]
NODE_STORAGE = [
    {"storage": "local", "type": "dir", "content": "backup,import,vztmpl,iso", "active": 1, "enabled": 1},
    {"storage": "local-lvm", "type": "lvmthin", "content": "rootdir,images", "active": 1, "enabled": 1},
]
BRIDGES = [{"iface": "vmbr0", "type": "bridge"}, {"iface": "vmbr1", "type": "bridge"}]
RES = [{"vmid": 100, "type": "lxc", "node": "pve1", "status": "running", "name": "a", "tags": "web;db"}]

REQUIRED = ["--hostname", "web", "--template-storage", "local", "--template", "debian-12-standard_12.7-1_amd64.tar.zst"]


def setup(api, nodes: list[dict] | None = None, resources: list[dict] | None = None) -> None:
    api.route("GET", "/nodes", nodes if nodes is not None else NODES)
    api.route("GET", "/cluster/nextid", "105")
    api.route("GET", "/nodes/{node}/storage/{storage}/content", TEMPLATES)
    api.route("GET", "/nodes/{node}/storage", NODE_STORAGE)
    api.route("GET", "/nodes/{node}/network", BRIDGES)
    api.route("GET", "/cluster/resources", resources if resources is not None else RES)
    api.route("GET", "/cluster/options", {})
    api.route("GET", "/pools", [])
    api.route("POST", "/nodes/{node}/lxc", lambda c: upid(c.match["node"], "vzcreate", c.data["vmid"]))


def created(api):
    [post] = api.writes()
    assert post.method == "POST"
    return post


# ── non-interactive ─────────────────────────────────────────────────────


def test_flags_build_the_whole_request(cli, api):
    setup(api)
    r = cli("ct", "add", "pve1", *REQUIRED, "--password", "s3cret!", "--rootfs-storage", "local-lvm",
            "--rootfs-size", "4", "--bridge", "vmbr0", "--ip", "10.0.0.5/24", "--gateway", "10.0.0.1",
            "--vlan", "20", "--firewall", "--cores", "2", "--memory", "1024", "--swap", "256",
            "--pool", "prod", "--onboot", "-p", "p1")
    assert r.exit_code == 0, r.output
    post = created(api)
    assert post.path == "/nodes/pve1/lxc"
    assert post.data == {
        "vmid": 105, "hostname": "web", "pool": "prod", "onboot": 1, "ostemplate": DEBIAN,
        "unprivileged": 1, "password": "s3cret!", "cores": 2, "memory": 1024, "swap": 256,
        "rootfs": "local-lvm:4", "net0": "name=eth0,bridge=vmbr0,ip=10.0.0.5/24,gw=10.0.0.1,tag=20,firewall=1",
        "features": "nesting=1",
    }


def test_flags_defaults(cli, api):
    setup(api)
    r = cli("ct", "add", "pve2", *REQUIRED, "--ctid", "250", "--privileged", "--keyctl", "--fuse", "-p", "p1")
    assert r.exit_code == 0, r.output
    post = created(api)
    assert post.path == "/nodes/pve2/lxc"
    assert post.data == {
        "vmid": 250, "hostname": "web", "onboot": 0, "ostemplate": DEBIAN, "unprivileged": 0,
        "cores": 1, "memory": 512, "swap": 512, "features": "nesting=1,keyctl=1,fuse=1",
    }


@pytest.mark.parametrize(("args", "net0"), [
    (["--ip", "dhcp", "--ip6", "auto"], "name=eth0,bridge=vmbr1,ip=dhcp,ip6=auto"),
    (["--ip", "none", "--ip6", "dhcp"], "name=eth0,bridge=vmbr1,ip6=dhcp"),
    (["--ip6", "2001:db8::5/64", "--gateway6", "2001:db8::1"], "name=eth0,bridge=vmbr1,ip6=2001:db8::5/64,gw6=2001:db8::1"),
])
def test_flags_network_modes(cli, api, args, net0):
    setup(api)
    r = cli("ct", "add", "pve1", *REQUIRED, "--bridge", "vmbr1", *args, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).data["net0"] == net0


def test_rootfs_storage_alone_defaults_to_8g(cli, api):
    setup(api)
    r = cli("ct", "add", "pve1", *REQUIRED, "--rootfs-storage", "local-lvm", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).data["rootfs"] == "local-lvm:8"


def test_full_volid_template_is_used_as_is(cli, api):
    setup(api)
    r = cli("ct", "add", "pve1", "--hostname", "web", "--template-storage", "local", "--template", ALPINE, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).data["ostemplate"] == ALPINE


def test_swap_zero_is_kept(cli, api):
    setup(api)
    r = cli("ct", "add", "pve1", *REQUIRED, "--swap", "0", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).data["swap"] == 0


@pytest.mark.parametrize("vlan", ["0", "4095", "abc"])
def test_invalid_vlan_is_refused_before_any_call(cli, api, vlan):
    setup(api)
    r = cli("ct", "add", "pve1", *REQUIRED, "--vlan", vlan, "-p", "p1")
    assert r.exit_code == 1
    assert "VLAN" in r.output
    assert api.calls == []


def test_unknown_node_is_refused(cli, api):
    setup(api)
    r = cli("ct", "add", "pve9", *REQUIRED, "-p", "p1")
    assert r.exit_code == 1
    assert "pve9" in r.output and "pve1, pve2" in r.output
    assert api.writes() == []


def test_short_password_is_refused(cli, api):
    setup(api)
    r = cli("ct", "add", "pve1", *REQUIRED, "--password", "abc", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_failed_creation_task_exits_non_zero(cli, api):
    setup(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "unable to create CT 105 - volume not found"})
    r = cli("ct", "add", "pve1", *REQUIRED, "-p", "p1")
    assert r.exit_code == 1
    assert "volume not found" in r.output


# ── wizard ──────────────────────────────────────────────────────────────


@pytest.fixture
def wizard(monkeypatch, ui):
    """ct add reads passwords with getpass.getpass: route it to the script."""
    monkeypatch.setattr(getpass, "getpass", lambda prompt="", *a, **k: ui.text_answer(prompt))
    return ui


def script_full(ui, ctid=None, final: bool = True) -> None:
    ui.text(ctid, "web")                    # CTID (default), hostname
    script_after_hostname(ui, final)


def script_after_hostname(ui, final: bool = True) -> None:
    ui.multi(["db"])                        # tags
    ui.confirm(True)                        # start at boot
    ui.menu("local", "debian")              # image storage, image
    ui.text("s3cret!", "s3cret!")           # password, twice
    ui.number(2, 1024, 0)                   # cores, memory, swap
    ui.menu("local-lvm")                    # rootfs storage
    ui.number(16)                           # rootfs size
    ui.menu("vmbr1", "Static IP")           # bridge, IPv4 mode
    ui.text("10.0.0.5/24", "10.0.0.1")      # address, gateway
    ui.menu("Auto (SLAAC)")                 # IPv6 mode
    ui.text("30")                           # VLAN
    ui.confirm(True, False, True)           # firewall, keyctl, fuse
    ui.confirm(final)                       # create
    if final:
        ui.confirm(False)                   # print the command


def test_wizard_full_flow(cli, api, wizard):
    setup(api)
    script_full(wizard)
    r = cli("ct", "add", "pve1", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).data == {
        "vmid": 105, "hostname": "web", "tags": "db", "onboot": 1, "ostemplate": DEBIAN,
        "unprivileged": 1, "password": "s3cret!", "cores": 2, "memory": 1024, "swap": 0,
        "rootfs": "local-lvm:16", "net0": "name=eth0,bridge=vmbr1,ip=10.0.0.5/24,gw=10.0.0.1,ip6=auto,tag=30,firewall=1",
        "features": "nesting=1,fuse=1",
    }
    assert not wizard.left()
    # Only storages holding images are offered for the image, only rootdir ones for the root disk
    image_storage_q = next(q for k, q in wizard.asked if "storage for image" in q)
    rootfs_q = next(q for k, q in wizard.asked if "root filesystem" in q)
    assert "local-lvm" not in image_storage_q
    assert "'local'" not in rootfs_q


def test_wizard_declined_writes_nothing(cli, api, wizard):
    setup(api)
    script_full(wizard, final=False)
    r = cli("ct", "add", "pve1", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_wizard_picks_node_when_several(cli, api, wizard):
    setup(api)
    wizard.menu("pve2")
    script_full(wizard)
    r = cli("ct", "add", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).path == "/nodes/pve2/lxc"


def test_wizard_node_menu_cancelled(cli, api, wizard):
    setup(api)
    wizard.menu(None)
    r = cli("ct", "add", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_wizard_single_node_is_taken_without_asking(cli, api, wizard):
    setup(api, nodes=[{"node": "solo", "status": "online"}])
    script_full(wizard)
    r = cli("ct", "add", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).path == "/nodes/solo/lxc"


def test_wizard_image_storage_cancelled_writes_nothing(cli, api, wizard):
    setup(api)
    wizard.text(None, "web")
    wizard.multi([])
    wizard.confirm(False)
    wizard.menu(None)
    r = cli("ct", "add", "pve1", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_wizard_empty_hostname_is_asked_again(cli, api, wizard):
    setup(api)
    wizard.text(None, "", "  ", "web")
    script_after_hostname(wizard)
    r = cli("ct", "add", "pve1", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert r.output.count("Hostname cannot be empty") == 2
    assert created(api).data["hostname"] == "web"


def test_wizard_password_is_asked_until_valid(cli, api, wizard):
    setup(api)
    wizard.text(None, "web")
    wizard.multi([])
    wizard.confirm(False)
    wizard.menu("local", "alpine")
    wizard.text("", "abc", "s3cret!", "typo!!", "s3cret!", "s3cret!")
    wizard.number(1, 512, 512)
    wizard.menu(None)                       # no rootfs storage chosen
    wizard.menu(None)                       # no bridge
    wizard.confirm(False, False, True, False)
    r = cli("ct", "add", "pve1", "-p", "p1")
    assert r.exit_code == 0, r.output
    for msg in ("cannot be empty", "at least 5", "do not match"):
        assert msg in r.output
    data = created(api).data
    assert data["password"] == "s3cret!"
    assert data["ostemplate"] == ALPINE
    assert "rootfs" not in data and "net0" not in data


def test_wizard_with_flags_asks_only_the_rest(cli, api, wizard):
    setup(api)
    wizard.text("web")                      # hostname: the only missing required value
    wizard.multi([])                        # tags
    wizard.confirm(True, False)             # create, print
    r = cli("ct", "add", "pve1", "--ctid", "120", "--onboot", "--template-storage", "local",
            "--template", "alpine", "--password", "s3cret!", "--cores", "1", "--memory", "256", "--swap", "0",
            "--rootfs-storage", "local-lvm", "--rootfs-size", "2", "--bridge", "vmbr0", "--ip", "dhcp",
            "--no-keyctl", "--no-fuse", "-p", "p1")
    assert r.exit_code == 0, r.output
    data = created(api).data
    assert data["ostemplate"] == ALPINE
    assert data["vmid"] == 120 and data["swap"] == 0 and data["rootfs"] == "local-lvm:2"
    assert data["net0"] == "name=eth0,bridge=vmbr0,ip=dhcp"
    assert data["features"] == "nesting=1"


def test_wizard_prints_reproducible_command(cli, api, wizard):
    setup(api)
    wizard.text(None, "web")
    wizard.multi([])
    wizard.confirm(False)
    wizard.menu("local", "debian")
    wizard.text("s3cret!", "s3cret!")
    wizard.number(1, 512, 512)
    wizard.menu("local-lvm")
    wizard.number(8)
    wizard.menu("vmbr0", "DHCP", "None")
    wizard.text("")
    wizard.confirm(False, True, False, True, True)
    r = cli("ct", "add", "pve1", "-p", "p1")
    assert r.exit_code == 0, r.output
    for part in ("pvecli ct add pve1", "--ctid 105", "--hostname web", "--no-onboot",
                 "--template-storage local", "--template debian-12-standard_12.7-1_amd64.tar.zst",
                 "--unprivileged", "--rootfs-storage local-lvm", "--rootfs-size 8", "--bridge vmbr0",
                 "--ip dhcp", "--keyctl", "-p p1"):
        assert part in r.output
    assert "s3cret!" not in r.output


def test_wizard_without_known_tags_asks_free_text(cli, api, wizard):
    setup(api, resources=[])
    wizard.text(None, "web", "blue")
    wizard.confirm(False)
    wizard.menu("local", "alpine")
    wizard.text("s3cret!", "s3cret!")
    wizard.number(1, 512, 512)
    wizard.menu(None, None)
    wizard.confirm(False, False, True, False)
    r = cli("ct", "add", "pve1", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).data["tags"] == "blue"


def test_wizard_invalid_ctid_is_asked_again(cli, api, wizard):
    setup(api)
    wizard.text("abc")
    script_full(wizard, ctid="130")
    r = cli("ct", "add", "pve1", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(api).data["vmid"] == 130
