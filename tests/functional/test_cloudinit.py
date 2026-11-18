"""vm deploy and vm cloudinit set: template check, cloud-init drive, keys, disk, password."""

from __future__ import annotations

from urllib.parse import quote, unquote

import pytest

from tests.fake_api import FakeAPI, fixture, upid

NODE = "pve-lab1"  # where template 9000 lives in the recorded 9.1 resources
KEY1 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGx1 alice@laptop"
KEY2 = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAB bob@desk+ops"

TEMPLATE_CFG = {
    "template": 1,
    "name": "deb13-cloud",
    "boot": "order=scsi0",
    "scsi0": "local-lvm:base-9000-disk-0,size=3G",
    "net0": "virtio=BC:24:11:96:77:50,bridge=vmbr0",
}
CLONE_CFG = {
    "name": "web01",
    "boot": "order=scsi0",
    "scsi0": "local-lvm:base-9000-disk-0/vm-120-disk-0,size=3G",
    "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0",
}


def route_deploy(api: FakeAPI, template_cfg=None, clone_cfg=None, resources=None) -> None:
    api.route("GET", "/cluster/resources", resources or fixture("pve-9.1/cluster_resources_vm"))
    api.route("GET", f"/nodes/{NODE}/qemu/9000/config", template_cfg or TEMPLATE_CFG)
    api.route("GET", "/cluster/nextid", "120")
    api.route("POST", f"/nodes/{NODE}/qemu/9000/clone", upid(NODE, "qmclone", 9000))
    api.route("GET", f"/nodes/{NODE}/qemu/120/config", clone_cfg or CLONE_CFG)
    api.route("PUT", f"/nodes/{NODE}/qemu/120/config", None)
    api.route("PUT", f"/nodes/{NODE}/qemu/120/resize", None)
    api.route("PUT", f"/nodes/{NODE}/qemu/120/cloudinit", None)
    api.route("POST", f"/nodes/{NODE}/qemu/120/status/start", upid(NODE, "qmstart", 120))


def ci_write(api: FakeAPI, vmid: int = 120):
    """The config write carrying the cloud-init settings (the last one)."""
    return api.called("PUT", f"/nodes/{NODE}/qemu/{vmid}/config")[-1]


# ── deploy ───────────────────────────────────────────────────────────────


def test_deploy_clones_sizes_configures_and_starts(cli, api, tmp_path):
    keyfile = tmp_path / "id.pub"
    keyfile.write_text(f"{KEY1}\n# comment\n{KEY2}\n")
    route_deploy(api)

    r = cli("vm", "deploy", "9000", "web01", "--ip", "dhcp", "--ssh-key", str(keyfile),
            "--user", "debian", "--cores", "2", "--memory", "2G", "--disk-size", "20G", "-p", "p1")

    assert r.exit_code == 0, r.output
    steps = [(w.method, w.path) for w in api.writes()]
    assert steps == [
        ("POST", f"/nodes/{NODE}/qemu/9000/clone"),
        ("PUT", f"/nodes/{NODE}/qemu/120/config"),
        ("PUT", f"/nodes/{NODE}/qemu/120/resize"),
        ("PUT", f"/nodes/{NODE}/qemu/120/config"),
        ("PUT", f"/nodes/{NODE}/qemu/120/cloudinit"),
        ("POST", f"/nodes/{NODE}/qemu/120/status/start"),
    ]
    clone, hw, resize, ci = api.writes()[:4]
    assert clone.data == {"newid": 120, "name": "web01"}  # linked clone
    assert hw.data == {"cores": 2, "memory": 2048}
    assert resize.data == {"disk": "scsi0", "size": "20G"}
    assert ci.data["ide2"] == "local-lvm:cloudinit"
    assert ci.data["ciuser"] == "debian"
    assert ci.data["ipconfig0"] == "ip=dhcp"
    assert ci.data["sshkeys"] == quote(f"{KEY1}\n{KEY2}", safe="")
    assert " " not in ci.data["sshkeys"] and "\n" not in ci.data["sshkeys"]
    assert unquote(ci.data["sshkeys"]) == f"{KEY1}\n{KEY2}"


def test_deploy_trusts_the_config_over_lagging_resources(cli, api):
    resources = fixture("pve-9.1/cluster_resources_vm")
    for r in resources:
        if r["vmid"] == 9000:
            r["template"] = 0  # just converted: /cluster/resources has not caught up
    route_deploy(api, resources=resources)

    r = cli("vm", "deploy", "9000", "web01", "--no-start", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert api.called("POST", f"/nodes/{NODE}/qemu/9000/clone")


def test_deploy_refuses_a_vm_whose_config_is_not_a_template(cli, api):
    cfg = {k: v for k, v in TEMPLATE_CFG.items() if k != "template"}
    route_deploy(api, template_cfg=cfg)  # resources still flag 9000 as a template

    r = cli("vm", "deploy", "9000", "web01", "-p", "p1")

    assert r.exit_code == 1
    assert "VM 9000 is not a template" in r.output
    assert api.writes() == []


def test_deploy_keeps_an_existing_cloudinit_drive(cli, api):
    tpl = {**TEMPLATE_CFG, "ide2": "local-lvm:vm-9000-cloudinit,media=cdrom"}
    clone = {**CLONE_CFG, "ide2": "local-lvm:vm-120-cloudinit,media=cdrom"}
    route_deploy(api, template_cfg=tpl, clone_cfg=clone)

    r = cli("vm", "deploy", "9000", "web01", "--ip", "10.0.0.21/24", "--gw", "10.0.0.1", "--no-start", "-p", "p1")

    assert r.exit_code == 0, r.output
    data = ci_write(api).data
    assert data == {"ipconfig0": "ip=10.0.0.21/24,gw=10.0.0.1"}
    assert api.called("POST", f"/nodes/{NODE}/qemu/120/status/start") == []


def test_deploy_storage_means_a_full_clone(cli, api):
    route_deploy(api)

    r = cli("vm", "deploy", "9000", "web01", "--storage", "ceph", "--no-start", "-p", "p1")

    assert r.exit_code == 0, r.output
    [clone] = api.called("POST", f"/nodes/{NODE}/qemu/9000/clone")
    assert clone.data == {"newid": 120, "name": "web01", "full": 1, "storage": "ceph"}


def test_deploy_grows_the_boot_disk_not_a_data_disk(cli, api):
    tpl = {**TEMPLATE_CFG, "boot": "order=virtio0", "virtio0": "local-lvm:base-9000-disk-0,size=3G",
           "scsi1": "local-lvm:base-9000-disk-1,size=50G"}
    tpl.pop("scsi0")
    clone = {**CLONE_CFG, "boot": "order=virtio0",
             "virtio0": "local-lvm:base-9000-disk-0/vm-120-disk-0,size=3G",
             "scsi1": "local-lvm:base-9000-disk-1/vm-120-disk-1,size=50G"}
    clone.pop("scsi0")
    route_deploy(api, template_cfg=tpl, clone_cfg=clone)

    r = cli("vm", "deploy", "9000", "web01", "--disk-size", "20G", "--no-start", "-p", "p1")

    assert r.exit_code == 0, r.output
    [resize] = api.called("PUT", f"/nodes/{NODE}/qemu/120/resize")
    assert resize.data == {"disk": "virtio0", "size": "20G"}


# ── password only from the environment ───────────────────────────────────


def test_deploy_password_comes_from_the_environment(cli, api, monkeypatch):
    monkeypatch.setenv("VM_PASS", "S3cr3t-pw")
    route_deploy(api)

    r = cli("vm", "deploy", "9000", "web01", "--password-env", "VM_PASS", "--no-start", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert ci_write(api).data["cipassword"] == "S3cr3t-pw"
    assert "S3cr3t-pw" not in r.output


def test_deploy_unset_password_variable_creates_nothing(cli, api, monkeypatch):
    monkeypatch.delenv("VM_PASS", raising=False)
    route_deploy(api)

    r = cli("vm", "deploy", "9000", "web01", "--password-env", "VM_PASS", "-p", "p1")

    assert r.exit_code == 1
    assert "VM_PASS" in r.output
    assert api.calls == []


@pytest.mark.parametrize("cmd", [("deploy", "9000", "web01"), ("cloudinit", "set", "120")])
def test_no_password_option_on_the_command_line(cli, api, cmd):
    route_deploy(api)

    r = cli("vm", *cmd, "--password", "S3cr3t-pw", "-p", "p1")

    assert r.exit_code == 2
    assert api.calls == []


# ── cloudinit set ────────────────────────────────────────────────────────


def route_set(api: FakeAPI, cfg: dict) -> None:
    api.route("GET", "/cluster/resources", fixture("pve-9.1/cluster_resources_vm"))
    api.route("GET", f"/nodes/{NODE}/qemu/301/config", cfg)
    api.route("PUT", f"/nodes/{NODE}/qemu/301/config", None)
    api.route("PUT", f"/nodes/{NODE}/qemu/301/cloudinit", None)


def test_cloudinit_set_adds_the_missing_drive_on_the_boot_storage(cli, api):
    route_set(api, {"name": "vm301", "scsi0": "ceph:vm-301-disk-0,size=10G",
                    "ide2": "none,media=cdrom"})

    r = cli("vm", "cloudinit", "set", "301", "--ssh-key", KEY1, "-p", "p1")

    assert r.exit_code == 0, r.output
    [cfg_put, regen] = api.writes()
    assert regen.path == f"/nodes/{NODE}/qemu/301/cloudinit"
    # ide2 holds a CD-ROM: the drive goes to the next free slot, on the boot disk storage
    assert cfg_put.data == {"sshkeys": quote(KEY1, safe=""), "ide0": "ceph:cloudinit"}


def test_cloudinit_set_with_a_drive_only_writes_the_settings(cli, api):
    route_set(api, {"name": "vm301", "scsi0": "ceph:vm-301-disk-0,size=10G",
                    "ide2": "ceph:vm-301-cloudinit,media=cdrom"})

    r = cli("vm", "cloudinit", "set", "301", "--user", "debian", "--dns", "10.0.0.1", "-p", "p1")

    assert r.exit_code == 0, r.output
    [cfg_put, regen] = api.writes()
    assert cfg_put.data == {"ciuser": "debian", "nameserver": "10.0.0.1"}
    assert regen.method == "PUT" and regen.path.endswith("/301/cloudinit")


def test_cloudinit_set_rejects_something_that_is_not_a_key(cli, api):
    route_set(api, {"scsi0": "ceph:vm-301-disk-0,size=10G"})

    r = cli("vm", "cloudinit", "set", "301", "--ssh-key", "not-a-key", "-p", "p1")

    assert r.exit_code == 1
    assert "Not an SSH public key" in r.output
    assert api.writes() == []
