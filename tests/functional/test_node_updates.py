"""`pvecli node updates`: pending packages per node, Kernel column, --refresh failures."""

from tests.fake_api import fixture, upid

NODES = fixture("pve-9.1/nodes")  # pve-lab1..5, all online
BASE = fixture("pve-9.1/node_apt_update")  # real pending list, no kernel in it


def pkg(name: str, version: str, old: str | None, origin: str = "Proxmox") -> dict:
    """One entry as GET /nodes/{node}/apt/update returns it."""
    entry = {"Arch": "amd64", "Origin": origin, "Package": name, "Priority": "optional",
             "Section": "admin", "Title": name, "Version": version, "Description": name}
    if old is not None:
        entry["OldVersion"] = old
    return entry


def row(stdout: str, node: str) -> list[str]:
    """Cells of the table row of a node: Node, Pending, Proxmox, Kernel."""
    line = next(x for x in stdout.splitlines() if "\u2502" in x and f" {node} " in x)
    return [c.strip() for c in line.strip().strip("\u2502").split("\u2502")]


def two_nodes(api) -> None:
    api.route("GET", "/nodes", [n for n in NODES if n["node"] in ("pve-lab1", "pve-lab2")])


def test_counts_come_from_the_last_apt_list_without_writing(cli, api):
    two_nodes(api)
    api.route("GET", "/nodes/{node}/apt/update", BASE)
    r = cli("node", "updates", "-p", "p1")
    assert r.exit_code == 0, r.output
    proxmox = sum(1 for p in BASE if p["Origin"] == "Proxmox")
    for node in ("pve-lab1", "pve-lab2"):
        cells = row(r.stdout, node)
        assert cells[1:3] == [str(len(BASE)), str(proxmox)]
        assert cells[3] == "-"  # no kernel pending in the real list
    assert api.writes() == []
    assert "--refresh" in r.output


def test_kernel_column_takes_only_real_kernels(cli, api):
    two_nodes(api)
    lists = {
        # New versioned kernel: a new package, no OldVersion
        "pve-lab1": BASE + [
            pkg("proxmox-kernel-6.17.13-21-pve-signed", "6.17.13-21", None),
            pkg("proxmox-kernel-helper", "9.0.4", "9.0.3"),
        ],
        # Meta package upgrade: the kernel version is the package version
        "pve-lab2": BASE + [
            pkg("proxmox-kernel-7.0", "7.0.2-1", "7.0.0-3"),
            pkg("proxmox-kernel-helper", "9.0.4", "9.0.3"),
        ],
    }
    api.route("GET", "/nodes/{node}/apt/update", lambda c: lists[c.match["node"]])
    r = cli("node", "updates", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert row(r.stdout, "pve-lab1")[3] == "6.17.13-21"
    assert row(r.stdout, "pve-lab2")[3] == "7.0.2-1"


def test_kernel_helper_alone_is_not_a_kernel(cli, api):
    api.route("GET", "/nodes", [n for n in NODES if n["node"] == "pve-lab1"])
    api.route("GET", "/nodes/{node}/apt/update", [pkg("proxmox-kernel-helper", "9.0.4", "9.0.3")])
    r = cli("node", "updates", "-p", "p1")
    assert r.exit_code == 0, r.output
    cells = row(r.stdout, "pve-lab1")
    assert cells[1:] == ["1", "1", "-"]
    assert "9.0.4" not in r.stdout


def test_refresh_failing_on_one_node_warns_and_still_lists(cli, api):
    two_nodes(api)
    api.route("POST", "/nodes/{node}/apt/update", lambda c: upid(c.match["node"], "aptupdate"))
    # The task of pve-lab1 ends in error, as with an enterprise repo without subscription
    api.route("GET", "/nodes/pve-lab1/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "command 'apt-get update' failed: exit code 100"})
    api.route("GET", "/nodes/{node}/apt/update", BASE)
    r = cli("node", "updates", "--refresh", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert sorted(c.path for c in api.writes()) == ["/nodes/pve-lab1/apt/update", "/nodes/pve-lab2/apt/update"]
    assert "pve-lab1: apt update failed" in r.output
    assert "pve-lab2: apt update failed" not in r.output
    assert row(r.stdout, "pve-lab1")[1] == str(len(BASE))
    assert row(r.stdout, "pve-lab2")[1] == str(len(BASE))


def test_offline_node_is_refused(cli, api):
    nodes = [dict(n) for n in NODES if n["node"] in ("pve-lab1", "pve-lab2")]
    nodes[0]["status"] = "offline"
    api.route("GET", "/nodes", nodes)
    r = cli("node", "updates", nodes[0]["node"], "-p", "p1")
    assert r.exit_code == 1
    assert "not online" in r.output
    assert api.called("GET", "/nodes/{node}/apt/update") == []


def test_json_lists_packages_per_node(cli, api):
    import json

    api.route("GET", "/nodes", [n for n in NODES if n["node"] == "pve-lab1"])
    api.route("GET", "/nodes/{node}/apt/update", BASE)
    r = cli("node", "updates", "--json", "-p", "p1")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert list(doc) == ["pve-lab1"]
    assert {p["package"] for p in doc["pve-lab1"]} == {p["Package"] for p in BASE}
