"""Helpers of src/cli/cloudinit.py: memory sizes, ipconfig values, SSH keys,
the parameters sent to PVE and where the cloud-init drive lives.

Protects the values sent to the PVE API: sshkeys must be URL-encoded on top
of the form encoding, ipconfig needs a prefix length, and only real public
keys may be pushed into a guest.
"""

from __future__ import annotations

import urllib.parse

import pytest
import typer

from src.api.exceptions import PVECliError
from src.cli.cloudinit import (
    _boot_storage,
    _ci_params,
    _cloudinit_drive,
    _ipconfig,
    _parse_memory,
    _read_keys,
)
from tests.fake_api import fixture

ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOx+/abc= alice@laptop"
RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7+x/y== bob@desk"


def ci(**kw):
    """_ci_params with every option unset unless given."""
    args = {"user": None, "password_env": None, "ssh_key": None, "ip": None, "gw": None,
            "ip6": None, "gw6": None, "dns": None, "search_domain": None, "upgrade": None,
            "iface": 0}
    return _ci_params(**{**args, **kw})


# ── _parse_memory (MiB) ──────────────────────────────────────────────────


@pytest.mark.parametrize("text, mib", [
    ("2048", 2048),
    ("512M", 512),
    ("512MB", 512),
    ("2G", 2048),
    ("2g", 2048),
    ("2GiB", 2048),
    ("1.5G", 1536),
])
def test_parse_memory(text, mib):
    assert _parse_memory(text) == mib


@pytest.mark.parametrize("text", ["", "G", "2T", "two", "-1G", "2X"])
def test_parse_memory_rejects(text):
    with pytest.raises(typer.BadParameter):
        _parse_memory(text)


# ── _ipconfig ────────────────────────────────────────────────────────────


def test_ipconfig_static_ipv4():
    assert _ipconfig("10.0.0.21/24", "10.0.0.1", None, None) == "ip=10.0.0.21/24,gw=10.0.0.1"


def test_ipconfig_dhcp_and_ipv6():
    assert _ipconfig("dhcp", None, None, None) == "ip=dhcp"
    assert _ipconfig(None, None, "auto", None) == "ip6=auto"
    assert _ipconfig(None, None, "dhcp", None) == "ip6=dhcp"
    assert _ipconfig("dhcp", None, "2001:db8::5/64", "2001:db8::1") == \
        "ip=dhcp,ip6=2001:db8::5/64,gw6=2001:db8::1"


def test_ipconfig_nothing_given():
    assert _ipconfig(None, None, None, None) is None


def test_ipconfig_needs_a_prefix_length():
    with pytest.raises(PVECliError, match="prefix length"):
        _ipconfig("10.0.0.21", "10.0.0.1", None, None)


@pytest.mark.parametrize("args", [
    ("10.0.0.300/24", None, None, None),
    ("10.0.0.21/33", None, None, None),
    ("10.0.0.21/24", "10.0.0", None, None),
    (None, None, "2001:db8::zz/64", None),
    (None, None, None, "not-an-ip"),
])
def test_ipconfig_rejects_bad_addresses(args):
    # The command turns ValueError into a clean error message
    with pytest.raises((ValueError, PVECliError)):
        _ipconfig(*args)


def test_ipconfig_ipv6_needs_a_prefix_length():
    with pytest.raises((ValueError, PVECliError)):
        _ipconfig(None, None, "2001:db8::5", None)


@pytest.mark.parametrize("args", [
    ("2001:db8::5/64", None, None, None),
    ("10.0.0.21/24", "2001:db8::1", None, None),
    (None, None, "10.0.0.21/24", None),
    (None, None, "2001:db8::5/64", "10.0.0.1"),
])
def test_ipconfig_rejects_wrong_family(args):
    with pytest.raises((ValueError, PVECliError)):
        _ipconfig(*args)


# ── _read_keys ───────────────────────────────────────────────────────────


def test_read_keys_from_file_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "keys.pub"
    f.write_text(f"# team keys\n\n{ED25519}\n  {RSA}  \n")
    assert _read_keys([str(f)]) == f"{ED25519}\n{RSA}"


def test_read_keys_inline_and_file_mixed(tmp_path):
    f = tmp_path / "id.pub"
    f.write_text(RSA + "\n")
    ecdsa = "ecdsa-sha2-nistp256 AAAAE2VjZHNh carol@x"
    sk = "sk-ssh-ed25519@openssh.com AAAAGnNr dave@x"
    assert _read_keys([ED25519, str(f), ecdsa, sk]) == "\n".join([ED25519, RSA, ecdsa, sk])


def test_read_keys_rejects_a_private_key(tmp_path):
    f = tmp_path / "id_ed25519"
    f.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----\n")
    with pytest.raises(PVECliError, match="Not an SSH public key"):
        _read_keys([str(f)])


@pytest.mark.parametrize("value", ["hello", "AAAAC3NzaC1lZDI1NTE5"])
def test_read_keys_rejects_non_keys(value):
    with pytest.raises(PVECliError, match="Not an SSH public key"):
        _read_keys([value])


@pytest.mark.parametrize("value", ["/no/such/file.pub", "~/.ssh/missing", "id_none.pub"])
def test_read_keys_names_a_missing_file(value):
    with pytest.raises(PVECliError, match="SSH key file not found"):
        _read_keys([value])


def test_read_keys_empty(tmp_path):
    f = tmp_path / "empty.pub"
    f.write_text("# nothing here\n\n")
    with pytest.raises(PVECliError, match="No SSH public key"):
        _read_keys([str(f)])


# ── _ci_params ───────────────────────────────────────────────────────────


def test_ci_params_sshkeys_is_url_encoded():
    params = ci(ssh_key=[ED25519, RSA])
    raw = f"{ED25519}\n{RSA}"
    assert params["sshkeys"] == urllib.parse.quote(raw, safe="")
    # Spelled out: space, newline, '+', '/', '=' and '@' are all escaped
    assert params["sshkeys"].startswith("ssh-ed25519%20AAAAC3NzaC1lZDI1NTE5AAAAIOx%2B%2Fabc%3D%20alice%40laptop%0Assh-rsa%20")
    assert urllib.parse.unquote(params["sshkeys"]) == raw


def test_ci_params_only_what_is_given():
    assert ci() == {}
    assert ci(user="debian", ip="dhcp", iface=1, upgrade=False) == {
        "ciuser": "debian", "ipconfig1": "ip=dhcp", "ciupgrade": 0,
    }
    assert ci(dns="10.0.0.53", search_domain="lab.local", upgrade=True) == {
        "nameserver": "10.0.0.53", "searchdomain": "lab.local", "ciupgrade": 1,
    }


def test_ci_params_password_from_environment(monkeypatch):
    monkeypatch.setenv("CI_PWD", "s3cret")
    assert ci(password_env="CI_PWD") == {"cipassword": "s3cret"}
    monkeypatch.setenv("CI_EMPTY", "")
    with pytest.raises(PVECliError, match="CI_EMPTY"):
        ci(password_env="CI_EMPTY")
    monkeypatch.delenv("CI_UNSET", raising=False)
    with pytest.raises(PVECliError, match="CI_UNSET"):
        ci(password_env="CI_UNSET")


# ── _cloudinit_drive / _boot_storage ─────────────────────────────────────

CFG = {
    "name": "web01",
    "description": "cloudinit template",
    "scsi0": "local-lvm:vm-100-disk-0,size=8G",
    "ide2": "local-lvm:vm-100-cloudinit,media=cdrom",
    "ide0": "local:iso/debian-13.iso,media=cdrom",
    "efidisk0": "nvme:vm-100-disk-1,size=4M",
}


def test_cloudinit_drive():
    assert _cloudinit_drive(CFG) == "ide2"
    assert _cloudinit_drive({"scsi1": "ceph:vm-100-cloudinit,media=cdrom"}) == "scsi1"
    assert _cloudinit_drive({"scsi0": "local-lvm:vm-100-disk-0,size=8G"}) is None
    # A text field mentioning cloudinit is not a drive
    assert _cloudinit_drive({"description": "local:vm-100-cloudinit"}) is None
    assert _cloudinit_drive(fixture("pve-9.2/qemu_config")) is None


def test_cloudinit_drive_ignores_storage_named_cloudinit():
    cfg = {
        "scsi0": "cloudinit-nfs:100/vm-100-disk-0.qcow2,size=8G",
        "ide2": "local-lvm:vm-100-cloudinit,media=cdrom",
    }
    assert _cloudinit_drive(cfg) == "ide2"


def test_cloudinit_drive_on_a_file_storage():
    # Real config of a clone on a dir storage (PVE 8.4 lab): the volume
    # carries the <vmid>/ directory and the format extension. Missing it made
    # `vm deploy` add a second drive and fail ("already attached at 'ide2'").
    cfg = {
        "ide2": "local:120/vm-120-cloudinit.qcow2,media=cdrom,size=4M",
        "scsi0": "local:9000/base-9000-disk-0.qcow2/120/vm-120-disk-0.qcow2,size=4G",
    }
    assert _cloudinit_drive(cfg) == "ide2"
    assert _cloudinit_drive({"ide0": "nfs:7/vm-7-cloudinit.raw,media=cdrom"}) == "ide0"
    # A real disk in a directory is not a cloud-init drive
    assert _cloudinit_drive({"scsi0": "local:120/vm-120-disk-0.qcow2,size=4G"}) is None
    assert _boot_storage(cfg) == "local"


def test_boot_storage_is_the_first_real_disk():
    assert _boot_storage(CFG) == "local-lvm"
    assert _boot_storage({"virtio0": "ceph-rbd:vm-1-disk-0,size=8G", "ide2": "none,media=cdrom"}) == "ceph-rbd"


def test_boot_storage_skips_cdroms_passthrough_and_cloudinit():
    cfg = {
        "ide0": "local-lvm:vm-100-cloudinit,media=cdrom",
        "ide2": "local:iso/debian.iso,media=cdrom",
        "sata0": "/dev/disk/by-id/ata-SAMSUNG_123",
        "efidisk0": "nvme:vm-100-disk-1,size=4M",
    }
    assert _boot_storage(cfg) is None
    assert _boot_storage(fixture("pve-9.2/qemu_config")) is None
