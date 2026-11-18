"""pvecli ct snapshot, ct tag and ct vnc: requests sent per container, and nothing opened for real."""

from __future__ import annotations

import pytest

from tests.fake_api import fixture, upid

RES = [
    {"vmid": 300, "type": "qemu", "node": "pve-lab2", "status": "running", "name": "vm"},
    {"vmid": 311, "type": "lxc", "node": "pve-lab1", "status": "running", "name": "fixct", "tags": "web"},
    {"vmid": 312, "type": "lxc", "node": "pve-lab2", "status": "stopped", "name": "idle", "tags": "db;web"},
]
BASE = "/nodes/pve-lab1/lxc/311"


def current(parent: str | None) -> dict:
    """The 'current' pseudo snapshot PVE always appends."""
    c = fixture("pve-9.1/qemu_snapshot")[0]
    if parent:
        c["parent"] = parent
    return c


SNAPS = [
    {"name": "pre-up", "snaptime": 1_799_000_000, "description": "before upgrade"},
    {"name": "daily", "snaptime": 1_799_900_000, "description": "", "parent": "pre-up"},
    current("daily"),
]


def setup(api, live_status: str = "running") -> None:
    api.route("GET", "/cluster/resources", RES)
    api.route("GET", "/cluster/options", {"tag-style": "color-map=web:00ff00:000000"})
    api.route("GET", f"{BASE}/snapshot", SNAPS)
    api.route("GET", f"{BASE}/status/current", {"status": live_status})
    api.route("POST", "/nodes/{node}/lxc/{vmid}/snapshot", lambda c: upid(c.match["node"], "vzsnapshot", c.match["vmid"]))
    api.route("POST", "/nodes/{node}/lxc/{vmid}/snapshot/{snap}/rollback",
              lambda c: upid(c.match["node"], "vzrollback", c.match["vmid"]))
    api.route("DELETE", "/nodes/{node}/lxc/{vmid}/snapshot/{snap}",
              lambda c: upid(c.match["node"], "vzdelsnapshot", c.match["vmid"]))
    api.route("POST", "/nodes/{node}/lxc/{vmid}/status/{action}", lambda c: upid(c.match["node"], "vzstart", c.match["vmid"]))
    api.route("GET", "/nodes/pve-lab1/lxc/311/config", {"hostname": "fixct", "tags": "web"})
    api.route("GET", "/nodes/pve-lab2/lxc/312/config", {"hostname": "idle", "tags": "db;web"})
    api.route("PUT", "/nodes/{node}/lxc/{vmid}/config", None)


def writes(api) -> list[tuple[str, str, object]]:
    return [(c.method, c.path, c.data) for c in api.writes()]


# ── snapshot ────────────────────────────────────────────────────────────


def test_snapshot_add_with_description_waits_for_task(cli, api):
    setup(api)
    r = cli("ct", "snapshot", "add", "311", "pre-up2", "--description", "before upgrade", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("POST", f"{BASE}/snapshot", {"snapname": "pre-up2", "description": "before upgrade"})]
    assert api.called("GET", "/nodes/{node}/tasks/{upid}/status")


def test_snapshot_add_failure_is_reported(cli, api):
    setup(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "snapshot feature is not available"})
    r = cli("ct", "snapshot", "add", "311", "x", "-p", "p1")
    assert r.exit_code == 1
    assert "snapshot feature is not available" in r.output


def test_snapshot_add_asks_name(cli, api, ui):
    setup(api)
    ui.text("  manual  ")
    r = cli("ct", "snapshot", "add", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("POST", f"{BASE}/snapshot", {"snapname": "manual"})]


def test_snapshot_add_empty_name_cancels(cli, api, ui):
    setup(api)
    ui.text("")
    r = cli("ct", "snapshot", "add", "311", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_snapshot_add_on_vm_id_is_refused(cli, api):
    setup(api)
    r = cli("ct", "snapshot", "add", "300", "x", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_snapshot_list_hides_current(cli, api):
    setup(api)
    r = cli("ct", "snapshot", "list", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "pre-up" in r.output and "daily" in r.output
    assert "current" not in r.output.replace("Snapshots for", "")


def test_snapshot_remove(cli, api):
    setup(api)
    r = cli("ct", "snapshot", "remove", "311", "daily", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("DELETE", f"{BASE}/snapshot/daily", None)]


def test_snapshot_remove_declined(cli, api, ui):
    setup(api)
    ui.confirm(False)
    r = cli("ct", "snapshot", "remove", "311", "daily", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_snapshot_remove_from_menu_never_offers_current(cli, api, ui):
    setup(api)
    ui.menu("pre-up")
    ui.confirm(True)
    r = cli("ct", "snapshot", "remove", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("DELETE", f"{BASE}/snapshot/pre-up", None)]
    assert "current" not in ui.asked[0][1]


def test_snapshot_remove_menu_cancelled(cli, api, ui):
    setup(api)
    ui.menu(None)
    r = cli("ct", "snapshot", "remove", "311", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_snapshot_remove_without_snapshots(cli, api, ui):
    setup(api)
    api.route("GET", f"{BASE}/snapshot", [current(None)])
    r = cli("ct", "snapshot", "remove", "311", "-p", "p1")
    assert r.exit_code == 0
    assert "No snapshots" in r.output
    assert api.writes() == []


def test_snapshot_rollback(cli, api):
    setup(api)
    r = cli("ct", "snapshot", "rollback", "311", "pre-up", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("POST", f"{BASE}/snapshot/pre-up/rollback", None)]


def test_snapshot_rollback_declined(cli, api, ui):
    setup(api)
    ui.confirm(False)
    r = cli("ct", "snapshot", "rollback", "311", "pre-up", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    assert "will be lost" in ui.asked[0][1]


@pytest.mark.parametrize(("live", "then"), [("stopped", "start"), ("running", "reboot")])
def test_snapshot_rollback_reboot_brings_container_back(cli, api, live, then):
    setup(api, live_status=live)
    r = cli("ct", "snapshot", "rollback", "311", "pre-up", "--reboot", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [p for _, p, _ in writes(api)] == [f"{BASE}/snapshot/pre-up/rollback", f"{BASE}/status/{then}"]


def test_snapshot_rollback_from_menu(cli, api, ui):
    setup(api)
    ui.menu("daily")
    ui.confirm(True)
    r = cli("ct", "snapshot", "rollback", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("POST", f"{BASE}/snapshot/daily/rollback", None)]


# ── tag ─────────────────────────────────────────────────────────────────


def test_tag_add_appends(cli, api):
    setup(api)
    r = cli("ct", "tag", "add", "311", "db,web", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("PUT", f"{BASE}/config", {"tags": "web;db"})]


def test_tag_add_replace(cli, api):
    setup(api)
    r = cli("ct", "tag", "add", "311", "prod", "--replace", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("PUT", f"{BASE}/config", {"tags": "prod"})]


def test_tag_add_already_present_writes_nothing(cli, api):
    setup(api)
    r = cli("ct", "tag", "add", "311", "web", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_tag_add_from_menu(cli, api, ui):
    setup(api)
    ui.multi(["db", "web"])
    r = cli("ct", "tag", "add", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("PUT", f"{BASE}/config", {"tags": "web;db"})]


def test_tag_add_picks_container_from_menu(cli, api, ui):
    setup(api)
    ui.menu("312 - idle")
    ui.multi(["+ Add custom tag"])
    ui.text("blue")
    r = cli("ct", "tag", "add", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("PUT", "/nodes/pve-lab2/lxc/312/config", {"tags": "db;web;blue"})]


def test_tag_remove_one_of_several(cli, api):
    setup(api)
    r = cli("ct", "tag", "remove", "312", "web", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("PUT", "/nodes/pve-lab2/lxc/312/config", {"tags": "db"})]


def test_tag_remove_from_menu(cli, api, ui):
    setup(api)
    ui.multi(["db", "web"])
    r = cli("ct", "tag", "remove", "312", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("PUT", "/nodes/pve-lab2/lxc/312/config", {"delete": "tags"})]


def test_tag_remove_unknown_tag_writes_nothing(cli, api):
    setup(api)
    r = cli("ct", "tag", "remove", "311", "nope", "-p", "p1")
    assert r.exit_code == 0
    assert "not found" in r.output
    assert api.writes() == []


def test_tag_list(cli, api):
    setup(api)
    r = cli("ct", "tag", "list", "312", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "db" in r.output and "web" in r.output
    assert api.writes() == []


def test_tag_on_vm_id_is_refused(cli, api):
    setup(api)
    r = cli("ct", "tag", "add", "300", "x", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


# ── vnc ─────────────────────────────────────────────────────────────────


@pytest.fixture
def opened(monkeypatch):
    """Nothing is spawned nor opened: record what would have been."""
    import src.utils
    import src.vnc.server

    seen: dict[str, list] = {"browser": [], "spawn": []}

    class Proc:
        pid = 4242

    def spawn(config):
        seen["spawn"].append(config)
        return Proc()

    monkeypatch.setattr(src.utils, "open_browser_window", lambda url, new_window=True: seen["browser"].append((url, new_window)))
    monkeypatch.setattr(src.vnc.server, "spawn_background_server", spawn)
    return seen


def vnc_routes(api) -> None:
    setup(api)
    api.route("POST", "/nodes/{node}/lxc/{vmid}/vncproxy",
              {"port": "5900", "ticket": "PVEVNC:ABC+/=", "user": "root@pam", "cert": "x", "upid": upid("pve-lab1", "vncproxy", 311)})


def test_vnc_running_container_spawns_proxy_and_opens_browser(cli, api, opened):
    vnc_routes(api)
    r = cli("ct", "vnc", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [("POST", f"{BASE}/vncproxy", {"websocket": 1})]
    [config] = opened["spawn"]
    assert config["ws_path"] == "/api2/json/nodes/pve-lab1/lxc/311/vncwebsocket"
    assert config["pve_port"] == 5900
    assert config["proxmox_host"] == "h1"
    assert config["vncticket"] == "PVEVNC:ABC+/="
    [(url, new_window)] = opened["browser"]
    assert url.startswith(f"http://localhost:{config['local_port']}/")
    assert "password=PVEVNC%3AABC%2B%2F%3D" in url
    assert new_window is False


def test_vnc_split_opens_new_windows(cli, api, opened):
    vnc_routes(api)
    r = cli("ct", "vnc", "311", "--split", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert opened["browser"][0][1] is True


def test_vnc_stopped_container_opens_nothing(cli, api, opened):
    vnc_routes(api)
    r = cli("ct", "vnc", "312", "-p", "p1")
    assert r.exit_code == 1
    assert "not running" in r.output
    assert api.writes() == []
    assert opened == {"browser": [], "spawn": []}


def test_vnc_vm_id_is_not_a_container(cli, api, opened):
    vnc_routes(api)
    r = cli("ct", "vnc", "300", "-p", "p1")
    assert r.exit_code == 1
    assert opened["spawn"] == []


def test_vnc_several_keeps_only_running(cli, api, opened):
    vnc_routes(api)
    r = cli("ct", "vnc", "311,312", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c["ws_path"] for c in opened["spawn"]] == ["/api2/json/nodes/pve-lab1/lxc/311/vncwebsocket"]


def test_vnc_menu_cancelled(cli, api, ui, opened):
    vnc_routes(api)
    ui.multi(None)
    r = cli("ct", "vnc", "-p", "p1")
    assert r.exit_code == 0
    assert opened["spawn"] == [] and api.writes() == []
