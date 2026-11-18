"""ct deploy: a ready-to-use container from a file template (POST /lxc) or
from a CT template (clone, then config), every value checked first.

Expected values come from the lab (PVE 9.1): POST /lxc without rootfs makes
<storage>:4 on 'local', creation is unprivileged unless told otherwise, two
clones of one CT template at once fail with "CT is locked (disk)", and the
config of a clone cannot take a password nor SSH keys.
"""

from __future__ import annotations

import pytest

from tests.fake_api import FakeAPI, fixture, upid

NODE = "pve-lab1"
TPL = "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst"
KEY1 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGx1 alice@laptop"
KEY2 = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAB bob@desk+ops"
CLONE_NET0 = "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:28:9D:61,ip=dhcp,type=veth"


def resources() -> list[dict]:
    res = fixture("pve-9.1/cluster_resources_vm")
    res.append({"vmid": 920, "type": "lxc", "name": "tpl920", "node": NODE, "template": 1, "status": "stopped"})
    return res


def nextid(taken: set[int], first: int = 120):
    """GET /cluster/nextid as PVE answers it: the lowest free ID, or the
    asked one when free, else a 400."""
    def answer(call):
        vmid = (call.params or {}).get("vmid")
        if vmid is None:
            return str(first)
        if int(vmid) in taken:
            return FakeAPI.error(400, f"VM {vmid} already exists")
        return str(vmid)
    return answer


def route_ct(api: FakeAPI, template_cfg=None, clone_cfg=None, storages=None, status="running") -> None:
    api.route("GET", "/cluster/resources", resources())
    api.route("GET", "/nodes", fixture("pve-9.1/nodes"))
    api.route("GET", "/pools", [{"poolid": "dev"}])
    api.route("GET", f"/nodes/{NODE}/storage", storages or fixture("pve-9.1/deploy_node_storage"))
    api.route("GET", f"/nodes/{NODE}/storage/local/content", fixture("pve-9.1/deploy_vztmpl_content"))
    api.route("GET", f"/nodes/{NODE}/network", fixture("pve-9.1/deploy_network_any_bridge"))
    api.route("GET", f"/nodes/{NODE}/lxc/920/config", template_cfg or fixture("pve-9.1/deploy_lxc_template_config"))
    api.route("GET", "/cluster/nextid", nextid({300, 301, 311, 920, 9000}))
    api.route("POST", f"/nodes/{NODE}/lxc", upid(NODE, "vzcreate", 120))
    api.route("POST", f"/nodes/{NODE}/lxc/920/clone", upid(NODE, "vzclone", 920))
    api.route("GET", f"/nodes/{NODE}/lxc/120/config", clone_cfg or {
        "hostname": "web02", "net0": CLONE_NET0, "rootfs": "local-lvm:vm-120-disk-0,size=2G",
        "unprivileged": 1, "memory": 512, "swap": 512})
    api.route("PUT", f"/nodes/{NODE}/lxc/120/config", None)
    api.route("PUT", f"/nodes/{NODE}/lxc/120/resize", upid(NODE, "resize", 120))
    api.route("POST", f"/nodes/{NODE}/lxc/120/status/start", upid(NODE, "vzstart", 120))
    api.route("GET", f"/nodes/{NODE}/lxc/120/status/current", {"status": status})
    api.route("GET", f"/nodes/{NODE}/lxc/120/interfaces", fixture("pve-9.1/lxc_interfaces"))


def steps(api: FakeAPI) -> list[tuple[str, str]]:
    return [(w.method, w.path) for w in api.writes()]


# ── from a file template ─────────────────────────────────────────────────


def test_create_sends_every_value_and_starts(cli, api, tmp_path, monkeypatch):
    keyfile = tmp_path / "id.pub"
    keyfile.write_text(f"{KEY1}\n# comment\n{KEY2}\n")
    monkeypatch.setenv("CT_PASS", "S3cr3t-pw")
    route_ct(api)

    r = cli("ct", "deploy", TPL, "--hostname", "web01", "--node", NODE, "--storage", "local-lvm",
            "--disk-size", "8G", "--cores", "2", "--memory", "1G", "--swap", "0",
            "--ip", "10.0.0.5/24", "--gw", "10.0.0.1", "--dns", "10.0.0.1", "--search-domain", "lab.example",
            "--ssh-key", str(keyfile), "--password-env", "CT_PASS", "--features", "nesting=1",
            "--tags", "web,lab", "--pool", "dev", "--wait-ip", "30", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert steps(api) == [("POST", f"/nodes/{NODE}/lxc"), ("POST", f"/nodes/{NODE}/lxc/120/status/start")]
    assert api.writes()[0].data == {
        "vmid": 120, "ostemplate": TPL, "hostname": "web01", "unprivileged": 1,
        "rootfs": "local-lvm:8", "cores": 2, "memory": 1024, "swap": 0,
        "net0": "name=eth0,bridge=vmbr0,ip=10.0.0.5/24,gw=10.0.0.1",
        "nameserver": "10.0.0.1", "searchdomain": "lab.example",
        "ssh-public-keys": f"{KEY1}\n{KEY2}", "password": "S3cr3t-pw",
        "features": "nesting=1", "tags": "web;lab", "pool": "dev",
    }
    assert "10.77.30.101" in r.output  # read from /interfaces, prefix removed
    assert "10.77.30.101/24" not in r.output
    assert "S3cr3t-pw" not in r.output
    flat = " ".join(r.output.replace("\\\n", " ").split())
    assert f"pvecli ct deploy {TPL} --hostname web01 --ctid 120" in flat
    assert "--password-env CT_PASS" in flat and "-p p1" in flat


def test_omitted_options_are_left_to_pve(cli, api):
    route_ct(api)

    r = cli("ct", "deploy", TPL, "--hostname", "web01", "--node", NODE, "--storage", "local-lvm", "--no-start", "-p", "p1")

    assert r.exit_code == 0, r.output
    [create] = api.writes()
    # unprivileged is PVE's own default at creation, sent so the intent is
    # explicit; no rootfs size (PVE makes 4G), no memory/swap/cores
    assert create.data == {"vmid": 120, "ostemplate": TPL, "hostname": "web01", "unprivileged": 1,
                           "storage": "local-lvm", "net0": "name=eth0,bridge=vmbr0"}


def test_privileged_is_sent_as_zero(cli, api):
    route_ct(api)

    r = cli("ct", "deploy", TPL, "--node", NODE, "--storage", "local-lvm", "--privileged", "--no-start", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert api.writes()[0].data["unprivileged"] == 0
    assert "hostname" not in api.writes()[0].data  # PVE names it CT<id>


def test_pve_default_storage_without_container_disks_is_refused(cli, api):
    route_ct(api)

    r = cli("ct", "deploy", TPL, "--hostname", "web01", "--node", NODE, "-p", "p1")

    # without --storage PVE puts the rootfs on 'local', which holds no rootdir here
    assert r.exit_code == 1
    assert "'local'" in r.output and "--storage" in r.output
    assert api.writes() == []


def test_several_nodes_need_node(cli, api):
    route_ct(api)

    r = cli("ct", "deploy", TPL, "--hostname", "web01", "--storage", "local-lvm", "-p", "p1")

    assert r.exit_code == 1
    assert "--node" in r.output
    assert api.writes() == []


def test_every_cluster_problem_is_listed_and_nothing_is_written(cli, api):
    route_ct(api)

    r = cli("ct", "deploy", "local:vztmpl/missing.tar.zst", "--hostname", "web01", "--node", NODE,
            "--storage", "pbs-lab", "--bridge", "vmbr9", "--pool", "nope", "--ctid", "311", "-p", "p1")

    assert r.exit_code == 1
    for part in ("missing.tar.zst", "pbs-lab", "vmbr9", "nope", "311"):
        assert part in r.output, part
    assert api.writes() == []


def test_unknown_node_is_refused(cli, api):
    route_ct(api)

    r = cli("ct", "deploy", TPL, "--node", "pve-lab9", "--storage", "local-lvm", "-p", "p1")

    assert r.exit_code == 1
    assert "pve-lab9" in r.output
    assert api.writes() == []


@pytest.mark.parametrize("opts", [
    ("--ip", "2001:db8::5/64"), ("--ip", "10.0.0.5"), ("--gw6", "10.0.0.1"), ("--ip6", "10.0.0.5/24"),
    ("--vlan", "0"), ("--memory", "lots"), ("--disk-size", "512M"), ("--features", "turbo=1"),
    ("--dns", "dns.example"),
])
def test_bad_values_are_refused_before_any_call(cli, api, opts):
    route_ct(api)

    r = cli("ct", "deploy", TPL, "--node", NODE, "--storage", "local-lvm", *opts, "-p", "p1")

    assert r.exit_code == 1, r.output
    assert api.calls == []


def test_unset_password_variable_creates_nothing(cli, api, monkeypatch):
    monkeypatch.delenv("CT_PASS", raising=False)
    route_ct(api)

    r = cli("ct", "deploy", TPL, "--node", NODE, "--storage", "local-lvm", "--password-env", "CT_PASS", "-p", "p1")

    assert r.exit_code == 1
    assert "CT_PASS" in r.output
    assert api.calls == []


def test_no_password_option_on_the_command_line(cli, api):
    route_ct(api)

    r = cli("ct", "deploy", TPL, "--password", "S3cr3t-pw", "-p", "p1")

    assert r.exit_code == 2
    assert api.calls == []


def test_a_container_that_does_not_come_up_is_an_error(cli, api):
    route_ct(api, status="stopped")

    r = cli("ct", "deploy", TPL, "--node", NODE, "--storage", "local-lvm", "-p", "p1")

    assert r.exit_code == 1
    assert "120" in r.output and "running" in r.output


# ── from a CT template ───────────────────────────────────────────────────


def test_clone_is_linked_on_lvmthin_then_configured_resized_started(cli, api):
    route_ct(api)

    r = cli("ct", "deploy", "920", "--hostname", "web02", "--ip", "10.0.0.6/24", "--gw", "10.0.0.1",
            "--cores", "2", "--memory", "1G", "--swap", "0", "--disk-size", "8G", "--tags", "a",
            "--dns", "10.0.0.1", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert steps(api) == [
        ("POST", f"/nodes/{NODE}/lxc/920/clone"),
        ("PUT", f"/nodes/{NODE}/lxc/120/config"),
        ("PUT", f"/nodes/{NODE}/lxc/120/resize"),
        ("POST", f"/nodes/{NODE}/lxc/120/status/start"),
    ]
    clone, cfg, resize, _ = api.writes()
    assert clone.data == {"newid": 120, "hostname": "web02", "full": 0}
    assert cfg.data == {
        "cores": 2, "memory": 1024, "swap": 0, "nameserver": "10.0.0.1", "tags": "a",
        "net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:28:9D:61,ip=10.0.0.6/24,type=veth,gw=10.0.0.1",
    }
    assert resize.data == {"disk": "rootfs", "size": "8G"}
    # resize runs as a task on PVE: it is waited for before the start
    paths = [(c.method, c.path) for c in api.calls]
    resize_upid = upid(NODE, "resize", 120)
    assert paths.index(("GET", f"/nodes/{NODE}/tasks/{resize_upid}/status")) \
        < paths.index(("POST", f"/nodes/{NODE}/lxc/120/status/start"))


def test_clone_to_another_storage_is_full(cli, api):
    route_ct(api)

    r = cli("ct", "deploy", "920", "--hostname", "web02", "--storage", "labpool", "--no-start", "-p", "p1")

    assert r.exit_code == 0, r.output
    [clone] = api.writes()
    assert clone.data == {"newid": 120, "hostname": "web02", "full": 1, "storage": "labpool"}


def test_clone_of_a_template_on_a_directory_storage_is_full(cli, api):
    tpl = {**fixture("pve-9.1/deploy_lxc_template_config"), "rootfs": "ctdir:920/base-920-disk-0.raw,size=2G"}
    storages = fixture("pve-9.1/deploy_node_storage") + [
        {"storage": "ctdir", "type": "dir", "content": "rootdir", "active": 1, "enabled": 1, "shared": 0}]
    route_ct(api, template_cfg=tpl, storages=storages)

    r = cli("ct", "deploy", "920", "--hostname", "web02", "--no-start", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert api.writes()[0].data == {"newid": 120, "hostname": "web02", "full": 1}


@pytest.mark.parametrize("opts, why", [
    (("--ssh-key", KEY1), "--ssh-key"),
    (("--password-env", "CT_PASS"), "--password-env"),
    (("--privileged",), "unprivileged"),
    (("--disk-size", "1G"), "2G"),
    (("--node", "pve-lab2"), "local-lvm"),
])
def test_clone_refuses_what_pve_cannot_do_to_a_clone(cli, api, monkeypatch, opts, why):
    monkeypatch.setenv("CT_PASS", "S3cr3t-pw")
    route_ct(api)

    r = cli("ct", "deploy", "920", "--hostname", "web02", *opts, "-p", "p1")

    assert r.exit_code == 1, r.output
    assert why in r.output
    assert api.writes() == []


def test_a_regular_container_is_not_a_template(cli, api):
    route_ct(api)
    api.route("GET", f"/nodes/{NODE}/lxc/311/config", {"hostname": "fixct", "rootfs": "local-lvm:vm-311-disk-0,size=8G"})

    r = cli("ct", "deploy", "311", "--hostname", "web02", "-p", "p1")

    assert r.exit_code == 1
    assert "311 is not a template" in r.output
    assert api.writes() == []


def test_a_vm_id_is_not_a_ct_template(cli, api):
    route_ct(api)

    r = cli("ct", "deploy", "9000", "--hostname", "web02", "-p", "p1")

    assert r.exit_code == 1
    assert "9000" in r.output
    assert api.writes() == []


def test_wait_ip_without_an_address_warns_but_succeeds(cli, api):
    route_ct(api)
    api.route("GET", f"/nodes/{NODE}/lxc/120/interfaces", [fixture("pve-9.1/lxc_interfaces")[0]])  # lo only

    r = cli("ct", "deploy", TPL, "--node", NODE, "--storage", "local-lvm", "--wait-ip", "20", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "No IPv4 address" in r.output
