"""Fleet mode (-p with several profiles) through read-only commands."""

from __future__ import annotations

import json

import pytest

from src.api.exceptions import AuthenticationError, NetworkError
from tests.fake_api import fixture

# h1 is the 5 node 'sdnlab' cluster (PVE 9.1), h2 a standalone node (PVE 9.2),
# hp the production cluster.
H1_STATUS = fixture("pve-9.1/cluster_status")
H2_STATUS = fixture("pve-9.2/cluster_status")
PROD_STATUS = [
    {"id": "cluster", "name": "prodclu", "nodes": 1, "quorate": 1, "type": "cluster", "version": 3},
    {"id": "node/prod1", "ip": "10.0.0.1", "local": 1, "name": "prod1", "nodeid": 1,
     "online": 1, "type": "node"},
]
PROD_NODES = [{"node": "prod1", "status": "online", "cpu": 0.1, "maxcpu": 4, "mem": 1,
               "maxmem": 2, "disk": 1, "maxdisk": 2, "uptime": 60}]


@pytest.fixture
def clusters(api):
    api.route("GET", "/cluster/status", H1_STATUS, host="h1")
    api.route("GET", "/nodes", fixture("pve-9.1/nodes"), host="h1")
    api.route("GET", "/cluster/status", H2_STATUS, host="h2")
    api.route("GET", "/nodes", fixture("pve-9.2/nodes"), host="h2")
    api.route("GET", "/cluster/status", PROD_STATUS, host="hp")
    api.route("GET", "/nodes", PROD_NODES, host="hp")
    return api


def hosts_of(api, path):
    return [c.host for c in api.called("GET", path)]


# ── one section per profile, one JSON document ─────────────────────────


def test_two_profiles_give_one_section_each(cli, clusters):
    r = cli("node", "list", "-p", "p1,p2")
    assert r.exit_code == 0, r.output
    i1, i2 = r.stdout.index("p1"), r.stdout.index("p2")
    assert i1 < i2
    assert "pve-lab1" in r.stdout[i1:i2] and "pve-lab9" not in r.stdout[i1:i2]
    assert "pve-lab9" in r.stdout[i2:]
    assert sorted(hosts_of(clusters, "/nodes")) == ["h1", "h2"]


def test_json_is_one_document_keyed_by_profile(cli, clusters):
    r = cli("node", "list", "-p", "p1,p2", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert set(doc) == {"p1", "p2"}
    assert doc["p1"]["profile"] == "p1"
    assert {n["node"] for n in doc["p1"]["nodes"]} == {f"pve-lab{i}" for i in range(1, 6)}
    assert [n["node"] for n in doc["p2"]["nodes"]] == ["pve-lab9"]


def test_cluster_health_json_per_profile(cli, clusters):
    clusters.route("GET", "/cluster/config/join", {"nodelist": [
        {"name": f"pve-lab{i}", "quorum_votes": "1"} for i in range(1, 6)]}, host="h1")
    clusters.route("GET", "/cluster/config/qdevice", {})
    clusters.route("GET", "/cluster/ha/status/current", [])
    clusters.route("GET", "/cluster/ceph/status", clusters.error(500, "binary not installed"))
    r = cli("cluster", "health", "-p", "p1,p2", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["p1"]["cluster"] == "sdnlab"
    assert doc["p1"]["quorate"] is True
    assert doc["p1"]["tolerance"] == 2  # 5 votes online, quorum needs 3
    assert doc["p2"] == {"cluster": None, "standalone": True}


def test_failure_summary_goes_to_stderr_and_keeps_json_valid(cli, clusters):
    clusters.route("GET", "/nodes", clusters.error(500, "pveproxy exploded"), host="h2")
    r = cli("node", "list", "-p", "p1,p2", "--json")
    assert r.exit_code == 1
    doc = json.loads(r.stdout)  # the summary must not corrupt stdout
    assert doc["p1"]["nodes"]
    assert doc["p2"]["exit_code"] == 1
    assert "pveproxy exploded" in doc["p2"]["error"]
    assert "Failed on 1/2 profile(s): p2" in r.stderr
    assert "Failed on" not in r.stdout


def test_json_error_holds_no_color_codes(cli, clusters, monkeypatch):
    # On a terminal the error is printed in color: the JSON value is plain text
    from src.utils.output import err_console
    monkeypatch.setattr(err_console, "_force_terminal", True)
    monkeypatch.setattr(err_console, "_color_system", err_console._detect_color_system())
    clusters.route("GET", "/nodes", clusters.error(500, "pveproxy exploded"), host="h2")
    r = cli("node", "list", "-p", "p1,p2", "--json")
    error = json.loads(r.stdout)["p2"]["error"]
    assert "pveproxy exploded" in error
    assert "\x1b" not in error


# ── unreachable profile ────────────────────────────────────────────────


@pytest.mark.parametrize("exc", [
    NetworkError("Connection refused to h2:8006"),
    AuthenticationError("Authentication failed: invalid credentials"),
], ids=["network", "auth"])
def test_unreachable_profile_is_reported_once_and_others_continue(cli, clusters, exc):
    clusters.route("GET", "/cluster/status", exc, host="h2")
    clusters.route("GET", "/nodes", exc, host="h2")
    r = cli("node", "list", "-p", "p1,p2")
    assert r.exit_code == 1
    assert "pve-lab1" in r.stdout
    assert r.output.count(str(exc)) == 1
    assert "Failed on 1/2 profile(s): p2" in r.stderr
    # p2 is not tried a second time after the failed probe
    assert hosts_of(clusters, "/nodes") == ["h1"]


def test_unreachable_profile_in_json(cli, clusters):
    clusters.route("GET", "/cluster/status", NetworkError("Connection refused to h2:8006"), host="h2")
    r = cli("node", "list", "-p", "p1,p2", "--json")
    assert r.exit_code == 1
    doc = json.loads(r.stdout)
    assert "Connection refused" in doc["p2"]["error"]
    assert doc["p1"]["nodes"]
    assert r.output.count("Connection refused") == 1


# ── same cluster twice ─────────────────────────────────────────────────


def test_same_cluster_runs_once(cli, clusters):
    r = cli("node", "list", "-p", "p1,p3")
    assert r.exit_code == 0, r.output
    assert "Skipping profile p3: same cluster as p1" in r.stderr
    assert "Skipping" not in r.stdout
    assert len(clusters.called("GET", "/nodes")) == 1
    assert r.stdout.count("pve-lab1") == 1


def test_same_cluster_json_has_only_the_kept_profile(cli, clusters):
    r = cli("node", "list", "-p", "p1,p3", "--json")
    assert r.exit_code == 0, r.output
    assert set(json.loads(r.stdout)) == {"p1"}
    assert len(clusters.called("GET", "/nodes")) == 1


# ── patterns and groups ────────────────────────────────────────────────


def test_star_excludes_prod(cli, clusters):
    r = cli("node", "list", "-p", "*", "--json")
    assert r.exit_code == 0, r.output
    assert set(json.loads(r.stdout)) == {"p1", "p2"}  # p3 twin of p1, prod out
    assert "hp" not in {c.host for c in clusters.calls}


def test_prod_named_explicitly_is_included(cli, clusters):
    r = cli("node", "list", "-p", "p1,prod", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert set(doc) == {"p1", "prod"}
    assert [n["node"] for n in doc["prod"]["nodes"]] == ["prod1"]


def test_group_expands(cli, clusters):
    r = cli("node", "list", "-p", "@labs", "--json")
    assert r.exit_code == 0, r.output
    assert set(json.loads(r.stdout)) == {"p1", "p2"}


def test_group_skips_fleet_false_profile(cli, clusters):
    r = cli("node", "list", "-p", "@withprod", "--json")
    assert r.exit_code == 0, r.output
    assert set(json.loads(r.stdout)) == {"p1"}
    assert "hp" not in {c.host for c in clusters.calls}


def test_unknown_group_is_a_clear_error(cli, clusters):
    r = cli("node", "list", "-p", "@nope")
    assert r.exit_code == 1
    assert "Unknown profile group 'nope'" in r.stderr
    assert "labs" in r.stderr and "withprod" in r.stderr
    assert clusters.calls == []


def test_pattern_matching_nothing_is_an_error(cli, clusters):
    r = cli("node", "list", "-p", "zz*")
    assert r.exit_code == 1
    assert "No profile matches 'zz*'" in r.stderr
    assert clusters.calls == []
