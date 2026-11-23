"""node shutdown / reboot / list / info / vnc / ssh against the fake API.

Responses come from the pve-9.1 recordings: five online nodes pve-lab1..5,
the API reached through pve-lab1 (10.199.32.51). The profile host `h1`
resolves to that address, as a DNS name of the connected node would.
"""

from __future__ import annotations

import json
import socket
from typing import Any

import pytest

from tests.fake_api import fixture

CONNECTED_IP = "10.199.32.51"  # pve-lab1 in pve-9.1/cluster_status


def offline(name: str) -> dict[str, Any]:
    """What /nodes returns for a node that is down: no usage figures at all."""
    return {"id": f"node/{name}", "level": "", "node": name, "status": "offline", "type": "node",
            "ssl_fingerprint": "AA:BB"}


# What /nodes/{node}/status answers on PVE 9 (trimmed to what is displayed)
NODE_STATUS = {
    "cpu": 0.25,
    "cpuinfo": {"cpus": 8, "model": "AMD EPYC 7302P 16-Core Processor", "sockets": 1, "cores": 8},
    "memory": {"total": 8 * 1024**3, "used": 2 * 1024**3, "free": 6 * 1024**3},
    "rootfs": {"total": 100 * 1024**3, "used": 25 * 1024**3, "free": 75 * 1024**3, "avail": 70 * 1024**3},
    "swap": {"total": 4 * 1024**3, "used": 1024**3, "free": 3 * 1024**3},
    "uptime": 90061,
    "kversion": "Linux 6.17.2-1-pve",
    "loadavg": ["0.10", "0.20", "0.30"],
}

# What POST /nodes/{node}/vncshell answers with websocket=1
VNC_SHELL = {
    "cert": "-----BEGIN CERTIFICATE-----...",
    "port": "5900",
    "ticket": "PVEVNC:6AFD0000::abc/def+ghi==",
    "upid": "UPID:pve-lab2:0000AAAA:0000BBBB:6AFD0000:vncshell::root@pam:",
    "user": "root@pam",
}


@pytest.fixture(autouse=True)
def dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The profile host h1 is the DNS name of pve-lab1; nothing else resolves."""
    def resolve(name: str) -> str:
        if name == "h1":
            return CONNECTED_IP
        raise socket.gaierror(name)

    monkeypatch.setattr(socket, "gethostbyname", resolve)


@pytest.fixture
def cluster(api: Any) -> Any:
    api.route("GET", "/nodes", fixture("pve-9.1/nodes"))
    api.route("GET", "/cluster/status", fixture("pve-9.1/cluster_status"))
    api.route("GET", "/version", fixture("pve-9.1/version"))
    api.route("POST", "/nodes/{node}/status", None)
    return api


def power_calls(api: Any) -> list[tuple[str, Any]]:
    return [(c.path, c.data) for c in api.called("POST", "/nodes/{node}/status")]


# ── shutdown / reboot ────────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["shutdown", "reboot"])
def test_power_command_sent_after_confirmation(cli, cluster, action):
    r = cli("node", action, "pve-lab3", "-p", "p1", input="y\n")
    assert r.exit_code == 0, r.output
    assert power_calls(cluster) == [("/nodes/pve-lab3/status", {"command": action})]
    # pve-lab3 is not the node behind the API: no warning about losing access
    assert "connected to this node" not in r.output


@pytest.mark.parametrize("action", ["shutdown", "reboot"])
def test_power_answered_no_sends_nothing(cli, cluster, action):
    r = cli("node", action, "pve-lab3", "-p", "p1", input="n\n")
    assert r.exit_code == 0
    assert cluster.writes() == []
    assert "cancel" in r.output.lower()


@pytest.mark.parametrize("action", ["shutdown", "reboot"])
def test_power_default_answer_is_no(cli, cluster, action):
    r = cli("node", action, "pve-lab3", "-p", "p1", input="\n")
    assert r.exit_code == 0
    assert cluster.writes() == []


@pytest.mark.parametrize("action", ["shutdown", "reboot"])
def test_power_on_the_connected_node_warns_before_asking(cli, cluster, action):
    r = cli("node", action, "pve-lab1", "-p", "p1", input="n\n")
    assert "connected to this node" in r.output
    assert cluster.writes() == []


def test_shutdown_of_the_connected_node_still_goes_through_with_yes(cli, cluster):
    r = cli("node", "shutdown", "pve-lab1", "-y", "-p", "p1")
    assert r.exit_code == 0
    assert "lose CLI access" in r.output
    assert power_calls(cluster) == [("/nodes/pve-lab1/status", {"command": "shutdown"})]


def test_connected_node_found_by_its_ip_in_the_profile(cli, cluster, home):
    import yaml

    cfg_path = home / ".config" / "pvecli" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["profiles"]["p1"]["host"] = "10.199.32.52"
    cfg_path.write_text(yaml.safe_dump(cfg))
    r = cli("node", "reboot", "pve-lab2", "-y", "-p", "p1")
    assert "temporarily lost" in r.output
    assert power_calls(cluster) == [("/nodes/pve-lab2/status", {"command": "reboot"})]


@pytest.mark.parametrize("action", ["shutdown", "reboot"])
def test_power_refused_on_an_offline_node(cli, cluster, action):
    nodes = fixture("pve-9.1/nodes")
    nodes = [n for n in nodes if n["node"] != "pve-lab4"] + [offline("pve-lab4")]
    cluster.route("GET", "/nodes", nodes)
    r = cli("node", action, "pve-lab4", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "not online" in r.output and "offline" in r.output
    assert cluster.writes() == []


@pytest.mark.parametrize("action", ["shutdown", "reboot"])
def test_power_refused_on_an_unknown_node(cli, cluster, action):
    r = cli("node", action, "pve-lab9", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "not found" in r.output
    assert cluster.writes() == []


def test_power_without_node_asks_which_one(cli, cluster, ui):
    ui.menu("pve-lab5").confirm(True)
    r = cli("node", "reboot", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert power_calls(cluster) == [("/nodes/pve-lab5/status", {"command": "reboot"})]
    assert ui.left() == {}


@pytest.mark.parametrize("action", ["shutdown", "reboot"])
def test_power_menu_escape_sends_nothing(cli, cluster, ui, action):
    ui.menu(None)
    r = cli("node", action, "-p", "p1")
    assert r.exit_code == 0
    assert cluster.writes() == []


def test_power_permission_denied_exits_1(cli, cluster):
    cluster.route("POST", "/nodes/{node}/status", cluster.forbidden("/nodes/pve-lab3", "Sys.PowerMgmt"))
    r = cli("node", "shutdown", "pve-lab3", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Sys.PowerMgmt" in r.output
    assert "Shutdown command sent" not in r.output


# ── list ─────────────────────────────────────────────────────────────────


def test_list_shows_every_node(cli, cluster):
    r = cli("node", "list", "-p", "p1")
    assert r.exit_code == 0
    for n in range(1, 6):
        assert f"pve-lab{n}" in r.output
    assert "2 cores" in r.output
    assert "4h" in r.output  # uptime 14894 s = 0d 4h


def test_list_json_carries_percentages(cli, cluster):
    r = cli("node", "list", "--json", "-p", "p1")
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    assert data["profile"] == "p1"
    lab1 = next(n for n in data["nodes"] if n["node"] == "pve-lab1")
    assert lab1["mem_percent"] == pytest.approx(2319888384 / 4106772480 * 100)
    assert lab1["cpu_percent"] == pytest.approx(9.4715852442672)
    assert lab1["disk_percent"] == pytest.approx(5352972288 / 14513811456 * 100)


def test_list_json_with_an_offline_node_does_not_divide_by_zero(cli, cluster):
    cluster.route("GET", "/nodes", [offline("pve-lab4")])
    r = cli("node", "list", "--json", "-p", "p1")
    assert r.exit_code == 0
    node = json.loads(r.stdout)["nodes"][0]
    assert (node["cpu_percent"], node["mem_percent"], node["disk_percent"]) == (0, 0, 0)


def test_list_order_by_unknown_column_fails(cli, cluster):
    r = cli("node", "list", "--order", "nosuchcolumn", "-p", "p1")
    assert r.exit_code == 1


def test_list_empty(cli, cluster):
    cluster.route("GET", "/nodes", [])
    r = cli("node", "list", "-p", "p1")
    assert r.exit_code == 0
    assert "No nodes found" in r.output


def test_list_offline_node_shows_no_invented_capacity(cli, cluster):
    cluster.route("GET", "/nodes", [offline("pve-lab4")])
    r = cli("node", "list", "-p", "p1")
    assert r.exit_code == 0
    assert "offline" in r.output
    assert "1 cores" not in r.output
    assert "/ 1 B" not in r.output


# ── info ─────────────────────────────────────────────────────────────────


def test_info_renders_status_of_one_node(cli, cluster):
    cluster.route("GET", "/nodes/{node}/status", NODE_STATUS)
    r = cli("node", "info", "pve-lab2", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in cluster.called("GET", "/nodes/{node}/status")] == ["/nodes/pve-lab2/status"]
    out = r.output
    assert "Node: pve-lab2" in out
    assert "online" in out
    assert "9.1.1 (release 9.1)" in out
    assert "AMD EPYC 7302P" in out
    assert "25.0%" in out  # cpu 0.25 and rootfs 25/100
    assert "Swap" in out
    assert cluster.writes() == []


def test_info_without_swap_has_no_swap_section(cli, cluster):
    status = {k: v for k, v in NODE_STATUS.items() if k != "swap"}
    cluster.route("GET", "/nodes/{node}/status", status)
    r = cli("node", "info", "pve-lab2", "-p", "p1")
    assert r.exit_code == 0
    assert "Swap" not in r.output


def test_info_all_shows_every_node_sorted(cli, cluster):
    cluster.route("GET", "/nodes/{node}/status", NODE_STATUS)
    r = cli("node", "info", "--all", "-p", "p1")
    assert r.exit_code == 0
    positions = [r.output.index(f"Node: pve-lab{n}") for n in range(1, 6)]
    assert positions == sorted(positions)


def test_info_picks_the_node_from_a_menu(cli, cluster, ui):
    cluster.route("GET", "/nodes/{node}/status", NODE_STATUS)
    ui.menu("pve-lab3")
    r = cli("node", "info", "-p", "p1")
    assert r.exit_code == 0
    assert "Node: pve-lab3" in r.output


def test_info_menu_escape_reads_nothing_more(cli, cluster, ui):
    ui.menu(None)
    r = cli("node", "info", "-p", "p1")
    assert r.exit_code == 0
    assert cluster.called("GET", "/nodes/{node}/status") == []


def test_info_no_nodes(cli, cluster):
    cluster.route("GET", "/nodes", [])
    r = cli("node", "info", "pve-lab1", "-p", "p1")
    assert r.exit_code == 0
    assert "No nodes found" in r.output


def test_info_all_with_an_offline_node_still_shows_the_others(cli, cluster):
    nodes = [n for n in fixture("pve-9.1/nodes") if n["node"] != "pve-lab2"] + [offline("pve-lab2")]
    cluster.route("GET", "/nodes", nodes)

    def status(call: Any) -> Any:
        if call.match["node"] == "pve-lab2":
            return cluster.error(595, "no route to host")
        return NODE_STATUS

    cluster.route("GET", "/nodes/{node}/status", status)
    r = cli("node", "info", "--all", "-p", "p1")
    assert r.exit_code == 0
    for n in (1, 3, 4, 5):
        assert f"Node: pve-lab{n}" in r.output
    assert "pve-lab2" in r.output and "offline" in r.output


# ── vnc ──────────────────────────────────────────────────────────────────


@pytest.fixture
def vnc(monkeypatch: pytest.MonkeyPatch, cluster: Any) -> dict[str, list[Any]]:
    """Nothing opens: browser, background server and foreground server are recorded."""
    import src.utils
    import src.vnc.server

    seen: dict[str, list[Any]] = {"browser": [], "spawn": [], "run": []}

    class Proc:
        pid = 4242

    def spawn(config: dict[str, Any]) -> Proc:
        seen["spawn"].append(config)
        return Proc()

    async def run(self: Any) -> None:
        seen["run"].append(self)

    monkeypatch.setattr(src.utils, "open_browser_window", lambda url, new_window=False: seen["browser"].append((url, new_window)))
    monkeypatch.setattr(src.vnc.server, "spawn_background_server", spawn)
    monkeypatch.setattr(src.vnc.server.VNCProxyServer, "run", run)
    cluster.route("POST", "/nodes/{node}/vncshell", VNC_SHELL)
    return seen


def test_vnc_opens_a_background_shell(cli, cluster, vnc):
    r = cli("node", "vnc", "pve-lab2", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [(c.path, c.data) for c in cluster.called("POST", "/nodes/{node}/vncshell")] == [
        ("/nodes/pve-lab2/vncshell", {"websocket": 1})
    ]
    [config] = vnc["spawn"]
    assert config["ws_path"] == "/api2/json/nodes/pve-lab2/vncwebsocket"
    assert config["vncticket"] == VNC_SHELL["ticket"]
    assert config["pve_port"] == 5900
    assert config["proxmox_host"] == "h1"
    # Without a separate password, noVNC authenticates with the ticket
    assert config["vnc_password"] == VNC_SHELL["ticket"]
    [(url, new_window)] = vnc["browser"]
    assert url.startswith(f"http://localhost:{config['local_port']}/vnc.html")
    assert "password=PVEVNC%3A" in url  # the ticket, URL-encoded
    assert new_window is False
    assert "PID: 4242" in r.output
    assert vnc["run"] == []


def test_vnc_several_nodes_split_in_windows(cli, cluster, vnc):
    r = cli("node", "vnc", "pve-lab2, pve-lab3", "--split", "-p", "p1")
    assert r.exit_code == 0
    assert [c["ws_path"].split("/")[4] for c in vnc["spawn"]] == ["pve-lab2", "pve-lab3"]
    assert [w for _, w in vnc["browser"]] == [True, True]
    assert len({c["local_port"] for c in vnc["spawn"]}) == 2


def test_vnc_skips_offline_and_unknown_nodes(cli, cluster, vnc):
    nodes = [n for n in fixture("pve-9.1/nodes") if n["node"] != "pve-lab4"] + [offline("pve-lab4")]
    cluster.route("GET", "/nodes", nodes)
    r = cli("node", "vnc", "pve-lab4,pve-lab9,pve-lab5", "-p", "p1")
    assert r.exit_code == 0
    assert "pve-lab4' is not online" in r.output
    assert "pve-lab9' not found" in r.output
    assert [c.path for c in cluster.called("POST", "/nodes/{node}/vncshell")] == ["/nodes/pve-lab5/vncshell"]
    assert len(vnc["spawn"]) == 1


def test_vnc_nothing_reachable_exits_1_and_opens_nothing(cli, cluster, vnc):
    cluster.route("GET", "/nodes", [offline("pve-lab4")])
    r = cli("node", "vnc", "pve-lab4", "-p", "p1")
    assert r.exit_code == 1
    assert cluster.writes() == []
    assert vnc == {"browser": [], "spawn": [], "run": []}


def test_vnc_foreground_runs_one_server(cli, cluster, vnc):
    r = cli("node", "vnc", "pve-lab2", "--no-background", "-p", "p1")
    assert r.exit_code == 0
    assert vnc["spawn"] == []
    assert len(vnc["run"]) == 1 and len(vnc["browser"]) == 1


def test_vnc_foreground_with_several_nodes_falls_back_to_background(cli, cluster, vnc):
    r = cli("node", "vnc", "pve-lab2,pve-lab3", "--no-background", "-p", "p1")
    assert r.exit_code == 0
    assert "only supports a single node" in r.output
    assert len(vnc["spawn"]) == 2 and vnc["run"] == []


def test_vnc_refused_with_a_token_profile(cli, cluster, vnc, home):
    import yaml

    cfg_path = home / ".config" / "pvecli" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["profiles"]["p1"]["auth"] = {"type": "token", "user": "root@pam", "token_name": "t", "token_value": "v"}
    cfg_path.write_text(yaml.safe_dump(cfg))
    r = cli("node", "vnc", "pve-lab2", "-p", "p1")
    assert r.exit_code == 1
    assert "token" in r.output
    assert cluster.calls == []
    assert vnc == {"browser": [], "spawn": [], "run": []}


def test_vnc_menu_escape_opens_nothing(cli, cluster, vnc, ui):
    ui.menu(None)
    r = cli("node", "vnc", "-p", "p1")
    assert r.exit_code == 0
    assert cluster.writes() == []
    assert vnc == {"browser": [], "spawn": [], "run": []}


def test_vnc_picked_from_menu(cli, cluster, vnc, ui):
    ui.menu("pve-lab3")
    r = cli("node", "vnc", "-p", "p1")
    assert r.exit_code == 0
    assert [c.path for c in cluster.called("POST", "/nodes/{node}/vncshell")] == ["/nodes/pve-lab3/vncshell"]


# ── ssh ──────────────────────────────────────────────────────────────────


@pytest.fixture
def ssh(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    import src.ssh

    runs: list[list[str]] = []
    monkeypatch.setattr(src.ssh, "exec_ssh", runs.append)
    return runs


def test_ssh_options_are_passed(cli, cluster, ssh):
    r = cli("node", "ssh", "pve-lab1", "-u", "admin", "-P", "2222", "-i", "/k", "-cm", "uptime", "-p", "p1")
    assert r.exit_code == 0
    [args] = ssh
    assert args[0] == "ssh"
    assert args[-2:] == ["admin@h1", "uptime"]
    assert ["-p", "2222"] == args[args.index("-p"):args.index("-p") + 2]
    assert ["-i", "/k"] == args[args.index("-i"):args.index("-i") + 2]


def test_ssh_menu_escape_connects_nowhere(cli, cluster, ssh, ui):
    ui.menu(None)
    r = cli("node", "ssh", "-p", "p1")
    assert r.exit_code == 0
    assert ssh == []


def test_ssh_goes_to_the_named_node(cli, cluster, ssh):
    r = cli("node", "ssh", "pve-lab2", "-p", "p1")
    assert r.exit_code == 0
    [args] = ssh
    assert args[-1] in ("root@10.199.32.52", "root@pve-lab2")


def test_node_list_is_sorted_by_name(cli, api):
    # PVE returns the nodes in no stable order
    api.route("GET", "/nodes", [{"node": n, "status": "online", "cpu": 0.1, "maxcpu": 4, "mem": 1,
                                 "maxmem": 2, "disk": 1, "maxdisk": 2, "uptime": 60}
                                for n in ("pve3", "pve1", "pve2")])
    r = cli("node", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert r.stdout.index("pve1") < r.stdout.index("pve2") < r.stdout.index("pve3")
