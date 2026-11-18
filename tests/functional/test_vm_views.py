"""vm list/info, snapshots, tags and the VNC console (no browser nor server is ever started)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.fake_api import fixture, upid

GUESTS = [dict(g) for g in fixture("pve-9.1/cluster_resources_vm")]
QEMU = [g for g in GUESTS if g["type"] == "qemu"]


def vm(vmid: int) -> dict:
    return next(g for g in GUESTS if g["vmid"] == vmid)


def setup(api, guests: list[dict] | None = None) -> None:
    api.route("GET", "/cluster/resources", guests if guests is not None else GUESTS)
    api.route("GET", "/cluster/options", {})


# ── list / info ─────────────────────────────────────────────────────────


def test_list_shows_every_vm_and_no_container(cli, api):
    setup(api)
    r = cli("vm", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    for g in QEMU:
        assert g["name"] in r.stdout
    for g in GUESTS:
        if g["type"] == "lxc":
            assert g["name"] not in r.stdout
    assert api.writes() == []


def test_list_filters_by_node_and_status(cli, api):
    guests = [dict(g) for g in GUESTS]
    running = next(g for g in guests if g["type"] == "qemu")
    running.update(status="running", cpu=0.5, mem=1, maxmem=2, uptime=3600)
    setup(api, guests)
    r = cli("vm", "list", "--status", "running", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert running["name"] in r.stdout
    for g in QEMU:
        if g["vmid"] != running["vmid"]:
            assert g["name"] not in r.stdout
    r = cli("vm", "list", "--node", "nowhere", "-p", "p1")
    assert "No VMs found" in r.output


def test_list_shows_the_lock(cli, api):
    guests = [dict(g) for g in GUESTS]
    next(g for g in guests if g["type"] == "qemu")["lock"] = "backup"
    setup(api, guests)
    r = cli("vm", "list", "-p", "p1")
    assert "locked (backup)" in r.stdout


def test_list_unknown_order_column_fails(cli, api):
    setup(api)
    r = cli("vm", "list", "--order", "nope", "-p", "p1")
    assert r.exit_code == 1


def test_info_of_a_running_vm_with_agent(cli, api):
    g = QEMU[0]
    base = f"/nodes/{g['node']}/qemu/{g['vmid']}"
    setup(api)
    api.route("GET", f"{base}/status/current",
              {**fixture("pve-9.1/qemu_status_current"), "status": "running", "uptime": 7200,
               "cpus": 2, "cpu": 0.25, "mem": 512 * 2**20, "maxmem": 1024 * 2**20})
    api.route("GET", f"{base}/config", {**fixture("pve-9.1/qemu_config"), "name": "web-a", "tags": "prod",
                                       "ostype": "l26"})
    api.route("GET", f"{base}/agent/network-get-interfaces", fixture("pve-9.2/qemu_agent_interfaces_running"))
    api.route("GET", f"{base}/agent/get-osinfo", {"result": {"pretty-name": "Debian GNU/Linux 13"}})
    r = cli("vm", "info", str(g["vmid"]), "-p", "p1")
    assert r.exit_code == 0, r.output
    for text in (f"VM {g['vmid']}: web-a", "running", g["node"], "2 cores", "Debian GNU/Linux 13", "prod", "net0"):
        assert text in r.stdout
    assert api.writes() == []


def test_info_of_a_stopped_vm_without_agent(cli, api):
    g = QEMU[0]
    base = f"/nodes/{g['node']}/qemu/{g['vmid']}"
    setup(api)
    api.route("GET", f"{base}/status/current", fixture("pve-9.1/qemu_status_current"))
    api.route("GET", f"{base}/config", fixture("pve-9.1/qemu_config"))
    api.route("GET", f"{base}/agent/network-get-interfaces", api.error(500, "QEMU guest agent is not running"))
    api.route("GET", f"{base}/agent/get-osinfo", api.error(500, "QEMU guest agent is not running"))
    r = cli("vm", "info", str(g["vmid"]), "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "stopped" in r.stdout
    assert "OS Name" not in r.stdout


def test_info_unknown_vm(cli, api):
    setup(api)
    r = cli("vm", "info", "999", "-p", "p1")
    assert r.exit_code == 1
    assert "VM 999 not found" in r.output


def test_info_menu_cancelled(cli, api, ui):
    setup(api)
    ui.menu(None)
    r = cli("vm", "info", "-p", "p1")
    assert r.exit_code == 0
    assert api.called("GET", "/nodes/{node}/qemu/{vmid}/config") == []


# ── snapshots ───────────────────────────────────────────────────────────

SNAP_VM = QEMU[0]
SNAP_BASE = f"/nodes/{SNAP_VM['node']}/qemu/{SNAP_VM['vmid']}"
VMID = str(SNAP_VM["vmid"])
SNAPS = [
    {"name": "pre-upgrade", "snaptime": 1_799_000_000, "description": "before apt", "vmstate": 1},
    {"name": "clean", "snaptime": 1_799_500_000, "description": "", "parent": "pre-upgrade"},
    {**fixture("pve-9.1/qemu_snapshot")[0], "parent": "clean"},
]


def snap_setup(api, live: str = "stopped") -> None:
    setup(api)
    api.route("GET", f"{SNAP_BASE}/snapshot", SNAPS)
    api.route("GET", f"{SNAP_BASE}/status/current", {"status": live})
    api.route("POST", f"{SNAP_BASE}/snapshot", upid(SNAP_VM["node"], "qmsnapshot", VMID))
    api.route("POST", f"{SNAP_BASE}/snapshot/{{name}}/rollback", upid(SNAP_VM["node"], "qmrollback", VMID))
    api.route("DELETE", f"{SNAP_BASE}/snapshot/{{name}}", upid(SNAP_VM["node"], "qmdelsnapshot", VMID))
    api.route("POST", f"{SNAP_BASE}/status/start", upid(SNAP_VM["node"], "qmstart", VMID))
    api.route("POST", f"{SNAP_BASE}/status/reboot", upid(SNAP_VM["node"], "qmreboot", VMID))


def test_snapshot_list_hides_current(cli, api):
    snap_setup(api)
    r = cli("vm", "snapshot", "list", VMID, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "pre-upgrade" in r.stdout and "clean" in r.stdout
    assert "current" not in r.stdout
    assert "You are here" not in r.stdout


def test_snapshot_add(cli, api):
    snap_setup(api)
    r = cli("vm", "snapshot", "add", VMID, "pre-deploy", "--description", "release 4", "--vmstate", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert post.path == f"{SNAP_BASE}/snapshot"
    assert post.data == {"snapname": "pre-deploy", "description": "release 4", "vmstate": 1}


def test_snapshot_add_without_ram_by_default(cli, api):
    snap_setup(api)
    r = cli("vm", "snapshot", "add", VMID, "s1", "--wait", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert post.data == {"snapname": "s1"}
    assert api.called("GET", "/nodes/{node}/tasks/{upid}/status")


def test_snapshot_add_prompted_name(cli, api, ui):
    snap_setup(api)
    ui.text("  s2 ")
    r = cli("vm", "snapshot", "add", VMID, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes()[0].data == {"snapname": "s2"}


def test_snapshot_add_empty_name_cancels(cli, api, ui):
    snap_setup(api)
    ui.text("")
    r = cli("vm", "snapshot", "add", VMID, "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_snapshot_remove(cli, api):
    snap_setup(api)
    r = cli("vm", "snapshot", "remove", VMID, "clean", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [delete] = api.writes()
    assert delete.path == f"{SNAP_BASE}/snapshot/clean"
    assert not delete.params  # never forced: a failed removal must stay visible


def test_snapshot_remove_declined(cli, api, ui):
    snap_setup(api)
    ui.confirm(False)
    r = cli("vm", "snapshot", "remove", VMID, "clean", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_snapshot_remove_menu_never_offers_current(cli, api, ui):
    snap_setup(api)
    ui.menu("pre-upgrade").confirm(True)
    r = cli("vm", "snapshot", "remove", VMID, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "current" not in ui.asked[0][1]
    assert [c.path for c in api.writes()] == [f"{SNAP_BASE}/snapshot/pre-upgrade"]


def test_snapshot_remove_menu_cancelled(cli, api, ui):
    snap_setup(api)
    ui.menu(None)
    cli("vm", "snapshot", "remove", VMID, "-p", "p1")
    assert api.writes() == []


def test_snapshot_remove_failed_task_is_an_error(cli, api):
    snap_setup(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status", {"status": "stopped", "exitstatus": "rbd error"})
    r = cli("vm", "snapshot", "remove", VMID, "clean", "-y", "-p", "p1")
    assert r.exit_code == 1


def test_snapshot_rollback_declined(cli, api, ui):
    snap_setup(api)
    ui.confirm(False)
    r = cli("vm", "snapshot", "rollback", VMID, "clean", "-p", "p1")
    assert r.exit_code == 0
    assert "Current state will be lost" in ui.asked[0][1]
    assert api.writes() == []


def test_snapshot_rollback(cli, api):
    snap_setup(api)
    r = cli("vm", "snapshot", "rollback", VMID, "clean", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == [f"{SNAP_BASE}/snapshot/clean/rollback"]


def test_snapshot_rollback_reboot_starts_a_stopped_vm(cli, api):
    snap_setup(api, live="stopped")
    r = cli("vm", "snapshot", "rollback", VMID, "clean", "-y", "--reboot", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == [f"{SNAP_BASE}/snapshot/clean/rollback", f"{SNAP_BASE}/status/start"]


def test_snapshot_rollback_reboot_reboots_a_running_vm(cli, api):
    snap_setup(api, live="running")
    r = cli("vm", "snapshot", "rollback", VMID, "pre-upgrade", "-y", "--reboot", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == [f"{SNAP_BASE}/snapshot/pre-upgrade/rollback",
                                              f"{SNAP_BASE}/status/reboot"]


def test_snapshot_rollback_from_menu(cli, api, ui):
    snap_setup(api)
    ui.menu("clean").confirm(True)
    r = cli("vm", "snapshot", "rollback", VMID, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == [f"{SNAP_BASE}/snapshot/clean/rollback"]


def test_snapshot_menu_without_snapshot(cli, api, ui):
    setup(api)
    api.route("GET", f"{SNAP_BASE}/snapshot", fixture("pve-9.1/qemu_snapshot"))
    r = cli("vm", "snapshot", "rollback", VMID, "-p", "p1")
    assert r.exit_code == 0
    assert "No snapshots" in r.output
    assert api.writes() == []


# ── tags ────────────────────────────────────────────────────────────────

TAG_BASE = SNAP_BASE


def tag_setup(api, tags: str) -> None:
    guests = [dict(g) for g in GUESTS]
    guests[0]["tags"] = "db;web"
    setup(api, guests)
    api.route("GET", f"{TAG_BASE}/config", {**fixture("pve-9.1/qemu_config"), "tags": tags} if tags
              else fixture("pve-9.1/qemu_config"))
    api.route("PUT", f"{TAG_BASE}/config", None)


def test_tag_add_appends(cli, api):
    tag_setup(api, "web")
    r = cli("vm", "tag", "add", VMID, "db, prod", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.data == {"tags": "web;db;prod"}


def test_tag_add_existing_writes_nothing(cli, api):
    tag_setup(api, "web")
    r = cli("vm", "tag", "add", VMID, "web", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_tag_add_replace(cli, api):
    tag_setup(api, "web;old")
    r = cli("vm", "tag", "add", VMID, "prod", "--replace", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes()[0].data == {"tags": "prod"}


def test_tag_add_from_menu(cli, api, ui):
    tag_setup(api, "")
    ui.multi(["db"])
    r = cli("vm", "tag", "add", VMID, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes()[0].data == {"tags": "db"}


def test_tag_add_menu_cancelled(cli, api, ui):
    tag_setup(api, "")
    ui.multi(None)
    cli("vm", "tag", "add", VMID, "-p", "p1")
    assert api.writes() == []


def test_tag_remove_from_menu(cli, api, ui):
    tag_setup(api, "db;web")
    ui.multi(["web"])
    r = cli("vm", "tag", "remove", VMID, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes()[0].data == {"tags": "db"}


def test_tag_remove_unknown_tag_writes_nothing(cli, api):
    tag_setup(api, "db")
    r = cli("vm", "tag", "remove", VMID, "web", "-p", "p1")
    assert r.exit_code == 0
    assert "not found" in r.output
    assert api.writes() == []


def test_tag_remove_menu_cancelled(cli, api, ui):
    tag_setup(api, "db;web")
    ui.multi(None)
    cli("vm", "tag", "remove", VMID, "-p", "p1")
    assert api.writes() == []


def test_tag_list(cli, api):
    tag_setup(api, "db;web")
    r = cli("vm", "tag", "list", VMID, "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "db" in r.stdout and "web" in r.stdout
    assert api.writes() == []


def test_tag_list_none(cli, api):
    tag_setup(api, "")
    r = cli("vm", "tag", "list", VMID, "-p", "p1")
    assert "No tags" in r.output


# ── vnc ─────────────────────────────────────────────────────────────────


@pytest.fixture
def desktop(monkeypatch):
    """Record browser windows and background servers instead of starting them."""
    import src.utils
    import src.vnc.server

    seen = SimpleNamespace(opened=[], spawned=[], ran=[])

    def spawn(config):
        seen.spawned.append(config)
        return SimpleNamespace(pid=4242)

    async def run(self, interactive=True):
        seen.ran.append(self.ws_path)

    monkeypatch.setattr(src.utils, "open_browser_window", lambda url, new_window=False: seen.opened.append(url))
    monkeypatch.setattr(src.vnc.server, "spawn_background_server", spawn)
    monkeypatch.setattr(src.vnc.server.VNCProxyServer, "run", run)
    return seen


def vnc_setup(api, running: dict[int, bool]) -> None:
    guests = [dict(g) for g in GUESTS]
    for g in guests:
        if g["vmid"] in running:
            g["status"] = "running" if running[g["vmid"]] else "stopped"
    setup(api, guests)
    api.route("POST", "/nodes/{node}/qemu/{vmid}/vncproxy",
              lambda c: {"ticket": "PVEVNC:T/1+2", "port": "5900", "password": "pw/+ x",
                         "upid": upid(c.match["node"], "vncproxy", c.match["vmid"])})


def test_vnc_running_vm_in_background(cli, api, desktop):
    g = QEMU[0]
    vnc_setup(api, {g["vmid"]: True})
    r = cli("vm", "vnc", str(g["vmid"]), "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert post.path == f"/nodes/{g['node']}/qemu/{g['vmid']}/vncproxy"
    assert post.data == {"websocket": 1, "generate-password": 1}
    [config] = desktop.spawned
    assert config["ws_path"] == f"/api2/json/nodes/{g['node']}/qemu/{g['vmid']}/vncwebsocket"
    assert config["vncticket"] == "PVEVNC:T/1+2"
    assert config["pve_port"] == 5900
    assert config["proxmox_host"] == "h1"
    assert config["vnc_password"] == "pw/+ x"
    [url] = desktop.opened
    assert url.startswith(f"http://localhost:{config['local_port']}/")
    assert "password=pw%2F%2B%20x" in url
    assert desktop.ran == []


def test_vnc_stopped_vm_opens_nothing(cli, api, desktop):
    g = QEMU[0]
    vnc_setup(api, {g["vmid"]: False})
    r = cli("vm", "vnc", str(g["vmid"]), "-p", "p1")
    assert r.exit_code == 1
    assert "not running" in r.output
    assert api.writes() == []
    assert desktop.spawned == [] and desktop.opened == []


def test_vnc_foreground(cli, api, desktop):
    g = QEMU[0]
    vnc_setup(api, {g["vmid"]: True})
    r = cli("vm", "vnc", str(g["vmid"]), "--no-background", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert desktop.spawned == []
    assert desktop.ran == [f"/api2/json/nodes/{g['node']}/qemu/{g['vmid']}/vncwebsocket"]
    assert len(desktop.opened) == 1


def test_vnc_several_skips_the_stopped_one(cli, api, desktop):
    a, b = QEMU[0], QEMU[1]
    vnc_setup(api, {a["vmid"]: True, b["vmid"]: False})
    r = cli("vm", "vnc", f"{a['vmid']},{b['vmid']}", "--no-background", "-p", "p1")
    assert r.exit_code == 0, r.output
    # --no-background is for one VM: several run in background
    assert [c["ws_path"] for c in desktop.spawned] == [f"/api2/json/nodes/{a['node']}/qemu/{a['vmid']}/vncwebsocket"]
    assert desktop.ran == []


def test_vnc_menu_cancelled(cli, api, ui, desktop):
    vnc_setup(api, {})
    ui.multi(None)
    r = cli("vm", "vnc", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    assert desktop.opened == []
