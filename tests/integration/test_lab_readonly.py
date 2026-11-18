"""Read-only commands against a real Proxmox lab.

Skipped unless PVECLI_LAB=1. They use the real ~/.config/pvecli profiles
named in PVECLI_LAB_PROFILES (default: lab1), and only ever read: no
command here changes anything on the cluster.

    PVECLI_LAB=1 PVECLI_LAB_PROFILES=lab1,lab9 pytest tests/integration -m lab
"""

import json
import os

import pytest
from typer.testing import CliRunner

from src.cli.main import app

pytestmark = [
    pytest.mark.lab,
    pytest.mark.skipif(os.environ.get("PVECLI_LAB") != "1", reason="set PVECLI_LAB=1 to run against the lab"),
]

PROFILES = [p for p in os.environ.get("PVECLI_LAB_PROFILES", "lab1").split(",") if p]


def run(*args: str):
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, result.stdout, result.stderr


@pytest.fixture(params=PROFILES)
def profile(request):
    return request.param


def test_node_list_json(profile):
    code, out, err = run("node", "list", "-p", profile, "--json")
    assert code == 0, err
    nodes = json.loads(out)["nodes"]
    assert nodes and all(n["status"] in ("online", "offline") for n in nodes)


def test_cluster_health_json(profile):
    code, out, err = run("cluster", "health", "-p", profile, "--json")
    assert code == 0, err
    data = json.loads(out)
    assert "standalone" in data or isinstance(data["quorate"], bool)


def test_find_the_profile_node_address(profile):
    """The host a profile points at is a node: find must say so."""
    import ipaddress

    from src.config import ConfigManager

    host = ConfigManager().get_profile(profile).host
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pytest.skip("profile host is a name, not an address")
    code, out, err = run("find", host, "-p", profile, "--json")
    assert code == 0, err
    assert any(r["type"] == "node" for r in json.loads(out))


def test_audit_runs_and_never_crashes(profile):
    code, out, err = run("audit", "-p", profile, "--json")
    assert code in (0, 2), err
    data = json.loads(out)
    assert {f["level"] for f in data["findings"]} <= {"OK", "INFO", "WARN", "FAIL"}


def test_fleet_json_is_one_document():
    if len(PROFILES) < 2:
        pytest.skip("needs two profiles in PVECLI_LAB_PROFILES")
    code, out, err = run("cluster", "health", "-p", ",".join(PROFILES), "--json")
    data = json.loads(out)
    assert set(data) <= set(PROFILES)


@pytest.mark.parametrize("args", [
    ("sdn", "health"), ("ceph", "status"), ("vm", "list"), ("ct", "list"),
    ("vm", "ha", "status"), ("snapshot", "list"), ("storage", "list"),
    ("find", "--stopped"), ("cluster", "preflight"),
])
def test_read_commands(profile, args):
    code, out, err = run(*args, "-p", profile)
    assert code in (0, 1), err
    assert "Traceback" not in out + err
