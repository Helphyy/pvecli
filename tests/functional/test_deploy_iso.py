"""pvecli deploy: VMs installed from an ISO, YAML anchors, the network of a clone.

An ISO install and cloud-init never mix. The empty disk boots first, so the
installer starts from the CD the first time and the installed system after.
"""

from __future__ import annotations

from tests.fake_api import FakeAPI, fixture
from tests.functional.test_deploy_yaml import Cluster, write

ISO = "local:iso/debian-13.6.0-amd64-netinst.iso"
WIN = "local:iso/windows-server-2025.iso"
VIRTIO = "local:iso/virtio-win-0.1.285.iso"


def lab(api: FakeAPI) -> Cluster:
    c = Cluster(api, "h1", "sdnlab", "pve-lab1").add(9000, "qemu", "deb13-cloud", 1)
    isos = [{"volid": v, "content": "iso"} for v in (ISO, WIN, VIRTIO)]

    def content(call):
        if (call.params or {}).get("content") == "iso":
            return isos
        return fixture("pve-9.1/deploy_vztmpl_content")

    api.route("GET", "/nodes/pve-lab1/storage/local/content", content, host="h1")

    def create(call):
        c.add(int(call.data["vmid"]), "qemu", call.data.get("name", ""))
        return c.upid("qmcreate", call.data["vmid"])

    api.route("POST", "/nodes/pve-lab1/qemu", create, host="h1")
    return c


def created(api: FakeAPI):
    return [c for c in api.writes() if c.path == "/nodes/pve-lab1/qemu"]


def started(api: FakeAPI):
    return [c.path for c in api.writes() if c.path.endswith("/status/start")]


ONE = """
    defaults:
      profile: p1
    guests:
      - type: vm
        name: inst01
        iso: local:iso/debian-13.6.0-amd64-netinst.iso
        storage: local-lvm
        disk_size: 32G
        memory: 4G
        cores: 4
        cpu: host
        vlan: 20
        power_on: false
"""


def test_an_iso_install_gets_an_empty_disk_the_cd_and_disk_first_boot(cli, api, tmp_path):
    lab(api)
    r = cli("deploy", write(tmp_path, ONE), "-y")
    assert r.exit_code == 0, r.output
    [vm] = created(api)
    d = vm.data
    assert d["name"] == "inst01" and d["ostype"] == "l26" and d["cpu"] == "host"
    assert d["memory"] == 4096 and d["cores"] == 4
    assert d["scsi0"] == "local-lvm:32" and d["ide2"] == f"{ISO},media=cdrom"
    assert d["boot"] == "order=scsi0;ide2;net0"
    assert d["net0"] == "virtio,bridge=vmbr0,tag=20"
    assert started(api) == []  # power_on: false
    assert "debian-13.6.0-amd64-netinst.iso" in r.output


def test_the_plan_shows_the_bridge_and_what_is_not_started(cli, api, tmp_path, monkeypatch):
    from src.utils import console

    lab(api)
    monkeypatch.setattr(console, "width", 200)  # one line per guest
    r = cli("deploy", write(tmp_path, ONE), "--dry-run")
    assert r.exit_code == 0, r.output
    assert "vmbr0 vlan 20" in r.output and "not started" in r.output


def test_an_iso_install_starts_by_default(cli, api, tmp_path):
    lab(api)
    r = cli("deploy", write(tmp_path, ONE.replace("        power_on: false\n", "")), "-y")
    assert r.exit_code == 0, r.output
    assert len(started(api)) == 1


def test_windows_gets_uefi_a_tpm_and_the_virtio_drivers(cli, api, tmp_path):
    lab(api)
    f = write(tmp_path, f"""
        guests:
          - type: vm
            name: win01
            profile: p1
            iso: {WIN}
            virtio_iso: {VIRTIO}
            os_type: windows
            storage: local-lvm
            disk_size: 64G
    """)
    r = cli("deploy", f, "-y")
    assert r.exit_code == 0, r.output
    d = created(api)[0].data
    assert d["ostype"] == "win11" and d["bios"] == "ovmf" and d["machine"] == "q35"
    assert d["efidisk0"].startswith("local-lvm:1,") and d["tpmstate0"] == "local-lvm:1,version=v2.0"
    assert d["ide0"] == f"{VIRTIO},media=cdrom"


def test_cloud_init_and_an_iso_are_refused_together(cli, api, tmp_path):
    lab(api)
    f = write(tmp_path, f"""
        defaults:
          profile: p1
          vm:
            user: debian
        guests:
          - type: vm
            name: inst01
            iso: {ISO}
            storage: local-lvm
            disk_size: 32G
            ip: 10.0.0.21/24
          - type: vm
            name: both
            iso: {ISO}
            template: 9000
    """)
    r = cli("deploy", f, "--dry-run")
    assert r.exit_code == 1
    assert "an ISO install has no cloud-init: user, ip cannot apply" in r.output
    assert "template and iso both given" in r.output
    assert api.writes() == []


def test_the_guest_choice_wins_over_the_defaults(cli, api, tmp_path):
    # Common defaults feed the clones too: an ISO VM leaves their cloud-init out
    lab(api)
    f = write(tmp_path, f"""
        defaults:
          profile: p1
          ssh_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJx4 me@box
          vm:
            template: 9000
        guests:
          - type: vm
            name: clone01
          - type: vm
            name: inst01
            iso: {ISO}
            storage: local-lvm
            disk_size: 20G
    """)
    r = cli("deploy", f, "-y")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes() if c.path.endswith("/clone")] == ["/nodes/pve-lab1/qemu/9000/clone"]
    assert created(api)[0].data["name"] == "inst01"


def test_what_an_iso_install_needs(cli, api, tmp_path):
    lab(api)
    f = write(tmp_path, """
        guests:
          - type: vm
            name: a
            profile: p1
            iso: debian.iso
          - type: vm
            name: b
            profile: p1
            iso: local:iso/missing.iso
            storage: local-lvm
            disk_size: 20G
            bridge: vmbr9
          - type: vm
            name: c
            profile: p1
            template: 9000
            os_type: windows
    """)
    r = cli("deploy", f, "--dry-run")
    assert r.exit_code == 1
    out = r.output
    assert "iso 'debian.iso' is not an ISO volume" in out
    assert "needs storage:" in out and "needs disk_size:" in out
    assert "ISO 'local:iso/missing.iso' not found on pve-lab1" in out
    assert "Bridge 'vmbr9' not found" in out
    assert "os_type and virtio_iso apply to a VM installed from an ISO" in out


def test_yaml_anchors_and_x_keys(cli, api, tmp_path):
    lab(api)
    f = write(tmp_path, f"""
        x-installer: &installer
          iso: {ISO}
          storage: local-lvm
          disk_size: 32G
          power_on: false
        defaults:
          profile: p1
        guests:
          - type: vm
            name: inst01
            <<: *installer
          - type: vm
            name: inst02
            <<: *installer
            disk_size: 64G
    """)
    r = cli("deploy", f, "-y")
    assert r.exit_code == 0, r.output
    disks = {c.data["name"]: c.data["scsi0"] for c in created(api)}
    assert disks == {"inst01": "local-lvm:32", "inst02": "local-lvm:64"}


def test_any_other_top_level_key_is_still_refused(cli, api, tmp_path):
    lab(api)
    r = cli("deploy", write(tmp_path, "common: {}\nguests:\n  - {type: vm, name: a, template: 9000, profile: p1}\n"),
            "--dry-run")
    assert r.exit_code == 1 and "Unknown top-level key 'common'" in r.output


def test_a_clone_moves_to_a_bridge_and_a_vlan_and_gets_a_cpu_type(cli, api, tmp_path):
    lab(api)
    f = write(tmp_path, """
        guests:
          - type: vm
            name: web01
            profile: p1
            template: 9000
            bridge: vvlan
            vlan: 30
            cpu: host
    """)
    # The clone has the net0 of its template, with a MAC of its own
    api.route("GET", "/nodes/pve-lab1/qemu/120/config",
              {"boot": "order=scsi0", "scsi0": "local-lvm:vm-120-disk-0,size=3G",
               "ide2": "local-lvm:vm-120-cloudinit,media=cdrom", "net0": "virtio=BC:24:11:96:77:50,bridge=vmbr0"},
              host="h1")
    r = cli("deploy", f, "-y")
    assert r.exit_code == 0, r.output
    put = next(c for c in api.writes() if c.method == "PUT" and c.path.endswith("/config") and "net0" in c.data)
    assert put.data["cpu"] == "host"
    assert put.data["net0"] == "virtio=BC:24:11:96:77:50,bridge=vvlan,tag=30"


def test_null_clears_a_cloud_init_default_for_an_iso_install(cli, api, tmp_path):
    lab(api)
    f = write(tmp_path, f"""
        defaults:
          profile: p1
          vm:
            template: 9000
            user: debian
        guests:
          - type: vm
            name: clone01
          - type: vm
            name: inst01
            iso: {ISO}
            user: null
            storage: local-lvm
            disk_size: 20G
    """)
    r = cli("deploy", f, "-y")
    assert r.exit_code == 0, r.output
    assert created(api)[0].data["name"] == "inst01"
