"""pvecli ct list/info and start/stop/shutdown/reboot: what is shown, what is sent, what is skipped."""

from __future__ import annotations

import pytest

from tests.fake_api import fixture, upid

GUESTS = fixture("pve-9.1/cluster_resources_vm")  # 311: lxc running on pve-lab1
CONFIG = fixture("pve-9.1/lxc_config")
IFACES = fixture("pve-9.1/lxc_interfaces")

RES = [
    *GUESTS,
    {"vmid": 312, "type": "lxc", "node": "pve-lab2", "status": "stopped", "name": "idle", "tags": "web",
     "maxcpu": 1, "maxmem": 536870912, "maxdisk": 4294967296, "pool": "prod"},
    {"vmid": 313, "type": "lxc", "node": "pve-lab2", "status": "running", "name": "busy", "tags": "web",
     "cpu": 0.5, "maxcpu": 2, "mem": 1, "maxmem": 2, "disk": 1, "maxdisk": 2, "uptime": 90000},
]

# GET /nodes/{node}/lxc/{vmid}/status/current of a running CT
STATUS_RUNNING = {"status": "running", "vmid": 311, "name": "fixct", "type": "lxc", "cpus": 2, "cpu": 0.05,
                  "mem": 2899968, "maxmem": 268435456, "swap": 0, "maxswap": 536870912,
                  "disk": 12464128, "maxdisk": 1020702720, "uptime": 3700}


def setup(api, resources: list[dict] | None = None) -> None:
    res = resources if resources is not None else RES
    status = {r["vmid"]: r["status"] for r in res}
    api.route("GET", "/cluster/resources", res)
    api.route("GET", "/cluster/options", {"tag-style": "color-map=web:00ff00"})
    api.route("GET", "/nodes/{node}/lxc/{vmid}/status/current",
              lambda c: {"status": status[int(c.match["vmid"])]})
    for action in ("start", "stop", "shutdown", "reboot"):
        api.route("POST", f"/nodes/{{node}}/lxc/{{vmid}}/status/{action}",
                  lambda c, a=action: upid(c.match["node"], f"vz{a}", c.match["vmid"]))


def posts(api) -> list[tuple[str, dict | None]]:
    return [(c.path, c.data) for c in api.writes()]


# ── list / info ─────────────────────────────────────────────────────────


def test_list_shows_only_containers_sorted(cli, api):
    setup(api)
    r = cli("ct", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "fixct" in r.output and "idle" in r.output and "busy" in r.output
    assert "guest-sdn" not in r.output and "deb13-cloud" not in r.output
    assert r.output.index("fixct") < r.output.index("idle") < r.output.index("busy")


def test_list_filters_by_node_and_status(cli, api):
    setup(api)
    r = cli("ct", "list", "--node", "pve-lab2", "--status", "running", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "busy" in r.output
    assert "idle" not in r.output and "fixct" not in r.output


def test_list_nothing_matches(cli, api):
    setup(api)
    r = cli("ct", "list", "--status", "paused", "-p", "p1")
    assert r.exit_code == 0
    assert "No containers found with status 'paused'" in r.output


def test_list_shows_lock(cli, api):
    setup(api, resources=[{**RES[-1], "lock": "backup"}])
    r = cli("ct", "list", "-p", "p1")
    assert "locked (backup)" in r.output


def test_list_unknown_order_column_fails(cli, api):
    setup(api)
    r = cli("ct", "list", "--order", "nope", "-p", "p1")
    assert r.exit_code == 1


def test_info_running_container(cli, api):
    setup(api)
    api.route("GET", "/nodes/pve-lab1/lxc/311/status/current", STATUS_RUNNING)
    api.route("GET", "/nodes/pve-lab1/lxc/311/config", {**CONFIG, "tags": "web", "lock": ""})
    api.route("GET", "/nodes/pve-lab1/lxc/311/interfaces", IFACES)
    r = cli("ct", "info", "311", "-p", "p1")
    assert r.exit_code == 0, r.output
    for text in ("Container 311: fixct", "pve-lab1", "running", "alpine", "10.77.30.101/24",
                 "bc:24:11:a6:9e:69", "bridge=vsimple", "web"):
        assert text in r.output
    assert api.writes() == []


def test_info_stopped_container_without_interfaces(cli, api):
    setup(api)
    api.route("GET", "/nodes/pve-lab2/lxc/312/status/current", {"status": "stopped", "cpus": 1, "maxmem": 536870912})
    api.route("GET", "/nodes/pve-lab2/lxc/312/config", {"hostname": "idle", "net0": "name=eth0,bridge=vmbr0,ip=dhcp"})
    api.route("GET", "/nodes/pve-lab2/lxc/312/interfaces", api.error(500, "CT 312 not running"))
    r = cli("ct", "info", "312", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "stopped" in r.output and "net0" in r.output and "Uptime" not in r.output


def test_info_vm_id_is_not_a_container(cli, api):
    setup(api)
    r = cli("ct", "info", "300", "-p", "p1")
    assert r.exit_code == 1
    assert "Container 300 not found" in r.output


def test_info_picked_from_menu(cli, api, ui):
    setup(api)
    api.route("GET", "/nodes/pve-lab1/lxc/311/config", CONFIG)
    api.route("GET", "/nodes/pve-lab1/lxc/311/interfaces", IFACES)
    ui.menu("311 - fixct")
    r = cli("ct", "info", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "fixct" in r.output


# ── start / stop / shutdown / reboot ────────────────────────────────────


def test_start_stopped_container(cli, api):
    setup(api)
    r = cli("ct", "start", "312", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert posts(api) == [("/nodes/pve-lab2/lxc/312/status/start", None)]


def test_start_running_container_is_skipped(cli, api):
    setup(api)
    r = cli("ct", "start", "311", "-p", "p1")
    assert r.exit_code == 0
    assert "already running" in r.output
    assert api.writes() == []


def test_start_vm_id_is_refused(cli, api):
    setup(api)
    r = cli("ct", "start", "301", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_start_by_tag_confirms_and_skips_running(cli, api, ui):
    setup(api)
    ui.confirm(True)
    r = cli("ct", "start", "--tag", "web", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert posts(api) == [("/nodes/pve-lab2/lxc/312/status/start", None)]
    assert "1 started, 1 skipped" in r.output


def test_start_by_tag_declined(cli, api, ui):
    setup(api)
    ui.confirm(False)
    r = cli("ct", "start", "--tag", "web", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_start_from_menu(cli, api, ui):
    setup(api)
    ui.multi(["312 - idle"])
    r = cli("ct", "start", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert posts(api) == [("/nodes/pve-lab2/lxc/312/status/start", None)]


def test_start_failed_task_exits_non_zero(cli, api):
    setup(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status", {"status": "stopped", "exitstatus": "startup for container '312' failed"})
    r = cli("ct", "start", "312", "-p", "p1")
    assert r.exit_code == 1
    assert "failed" in r.output


def test_stop_running_container(cli, api):
    setup(api)
    r = cli("ct", "stop", "311", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert posts(api) == [("/nodes/pve-lab1/lxc/311/status/stop", None)]


def test_stop_timeout_is_sent(cli, api):
    setup(api)
    r = cli("ct", "stop", "311", "-t", "30", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert posts(api) == [("/nodes/pve-lab1/lxc/311/status/stop", {"timeout": 30})]


def test_stop_declined_writes_nothing(cli, api, ui):
    setup(api)
    ui.confirm(False)
    r = cli("ct", "stop", "311", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    assert "Hard stop container 311?" in ui.asked[0][1]


def test_stop_range_skips_stopped(cli, api):
    setup(api)
    r = cli("ct", "stop", "311-313", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [p for p, _ in posts(api)] == ["/nodes/pve-lab1/lxc/311/status/stop", "/nodes/pve-lab2/lxc/313/status/stop"]
    assert "2 stopped, 1 skipped" in r.output


def test_shutdown_sends_default_timeout(cli, api):
    setup(api)
    r = cli("ct", "shutdown", "311", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert posts(api) == [("/nodes/pve-lab1/lxc/311/status/shutdown", {"timeout": 60})]


def test_shutdown_force(cli, api):
    setup(api)
    r = cli("ct", "shutdown", "311", "--force", "-t", "10", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert posts(api) == [("/nodes/pve-lab1/lxc/311/status/shutdown", {"timeout": 10, "forceStop": 1})]


def test_shutdown_stopped_is_skipped(cli, api):
    setup(api)
    r = cli("ct", "shutdown", "312", "-y", "-p", "p1")
    assert r.exit_code == 0
    assert "not running" in r.output
    assert api.writes() == []


def test_shutdown_declined(cli, api, ui):
    setup(api)
    ui.confirm(False)
    r = cli("ct", "shutdown", "311", "-p", "p1")
    assert api.writes() == []
    assert r.exit_code == 0


def test_reboot_running(cli, api):
    setup(api)
    r = cli("ct", "reboot", "311", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert posts(api) == [("/nodes/pve-lab1/lxc/311/status/reboot", None)]


def test_reboot_by_node_filter(cli, api, ui):
    setup(api)
    ui.confirm(True)
    r = cli("ct", "reboot", "--node", "pve-lab2", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [p for p, _ in posts(api)] == ["/nodes/pve-lab2/lxc/313/status/reboot"]


def test_reboot_stopped_is_skipped(cli, api):
    setup(api)
    r = cli("ct", "reboot", "312", "-y", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


@pytest.mark.parametrize("cmd", ["start", "stop", "shutdown", "reboot"])
def test_menu_cancelled_writes_nothing(cli, api, ui, cmd):
    setup(api)
    ui.multi(None)
    r = cli("ct", cmd, "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
