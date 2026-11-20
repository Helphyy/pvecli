"""Helpers of src/utils/fleet.py: reading a -p value and keeping one profile
per cluster.

Protects the production guard (a `fleet: false` profile is never reached
by a pattern or a group, only when named) and the rule that a command runs
once per cluster, never twice on the same one nor zero times on a distinct
one.
"""

from __future__ import annotations

import pytest
import yaml

from src.api.exceptions import ConfigError
from src.utils import fleet
from src.utils.fleet import dedupe_by_cluster, is_multi_profile, resolve_profiles
from tests.fake_api import fixture

# ── is_multi_profile ─────────────────────────────────────────────────────


@pytest.mark.parametrize("spec, multi", [
    (None, False),
    ("", False),
    ("p1", False),
    ("client-a", False),
    ("p1,p2", True),
    ("@labs", True),
    ("client-*", True),
    ("p?", True),
    ("p[12]", True),
])
def test_is_multi_profile(spec, multi):
    assert is_multi_profile(spec) is multi


# ── resolve_profiles (test config: p1, p2, p3, prod with fleet: false) ───


@pytest.mark.parametrize("spec, expected", [
    (None, ["p1"]),                       # default profile
    ("p2", ["p2"]),
    ("p2,p1", ["p2", "p1"]),              # order kept
    ("p1, p1 ,p2,", ["p1", "p2"]),        # duplicates and blanks dropped
    ("p*", ["p1", "p2", "p3"]),           # prod matches but is out of the fleet
    ("*", ["p1", "p2", "p3"]),
    ("p[13]", ["p1", "p3"]),
    ("@labs", ["p1", "p2"]),
    ("@withprod", ["p1"]),                # prod is not reached through a group
    ("prod", ["prod"]),                   # but it is when named
    ("p*,prod", ["p1", "p2", "p3", "prod"]),
    ("@labs,p2,p3", ["p1", "p2", "p3"]),
])
def test_resolve_profiles(home, spec, expected):
    assert resolve_profiles(spec) == expected


def test_resolve_profiles_patterns_are_case_sensitive(home):
    with pytest.raises(ConfigError, match="No profile matches 'P\\*'"):
        resolve_profiles("P*")


@pytest.mark.parametrize("spec, message", [
    ("nope", "Profile 'nope' not found"),
    ("p1,nope", "Profile 'nope' not found"),
    ("p", "Profile 'p' not found"),               # a prefix is not a profile
    ("@nogroup", "Unknown profile group 'nogroup'. Groups: labs, withprod"),
    ("zz*", "No profile matches 'zz\\*'"),
    ("prod*", "No profile matches 'prod\\*'"),    # only fleet: false profiles match
])
def test_resolve_profiles_errors(home, spec, message):
    with pytest.raises(ConfigError, match=message):
        resolve_profiles(spec)


def edit_config(home, **changes):
    path = home / ".config" / "pvecli" / "config.yaml"
    data = yaml.safe_load(path.read_text())
    for key, value in changes.items():
        data[key] = value
    path.write_text(yaml.safe_dump(data))


def test_group_listing_an_unknown_profile(home):
    edit_config(home, groups={"broken": ["p1", "ghost"]})
    with pytest.raises(ConfigError, match="Group 'broken' lists unknown profile\\(s\\): ghost"):
        resolve_profiles("@broken")


def test_no_default_profile(home):
    edit_config(home, default_profile=None)
    with pytest.raises(ConfigError, match="No default profile"):
        resolve_profiles(None)


def test_group_with_only_production_profiles_is_an_error(home):
    edit_config(home, groups={"prodonly": ["prod"]})
    with pytest.raises(ConfigError):
        resolve_profiles("@prodonly")


# ── dedupe_by_cluster ────────────────────────────────────────────────────


def fake_identities(monkeypatch, table):
    probed = []

    def identity(name):
        probed.append(name)
        return table[name]

    monkeypatch.setattr(fleet, "_cluster_identity", identity)
    return probed


def test_dedupe_keeps_the_first_profile_of_each_cluster(monkeypatch):
    nodes = ("pve-lab1", "pve-lab2")
    fake_identities(monkeypatch, {
        "p1": ("cluster", "sdnlab", nodes),
        "p2": ("node", ("pve-lab9",)),
        "p3": ("cluster", "sdnlab", nodes),
    })
    assert dedupe_by_cluster(["p1", "p2", "p3"]) == (["p1", "p2"], {"p3": "p1"}, {})


def test_dedupe_does_not_merge_distinct_clusters(monkeypatch):
    fake_identities(monkeypatch, {
        # Same cluster name, other members: two clusters
        "a": ("cluster", "prod", ("pve1", "pve2")),
        "b": ("cluster", "prod", ("pve1", "pve3")),
        # Same members, other cluster name
        "c": ("cluster", "prod2", ("pve1", "pve2")),
        # A standalone node named like a cluster
        "d": ("node", ("prod",)),
    })
    assert dedupe_by_cluster(["a", "b", "c", "d"]) == (["a", "b", "c", "d"], {}, {})


def test_dedupe_reports_an_unreachable_profile_once(monkeypatch):
    probed = fake_identities(monkeypatch, {
        "p1": ("cluster", "sdnlab", ("pve-lab1",)),
        "p2": "Connection refused",
        "p3": ("cluster", "sdnlab", ("pve-lab1",)),
    })
    kept, skipped, unreachable = dedupe_by_cluster(["p1", "p2", "p3"])
    assert kept == ["p1"]
    assert skipped == {"p3": "p1"}
    assert unreachable == {"p2": "Connection refused"}
    assert sorted(probed) == ["p1", "p2", "p3"]


def test_dedupe_two_unreachable_profiles_are_not_twins(monkeypatch):
    # The same error text does not make two profiles the same cluster
    fake_identities(monkeypatch, {"a": "timed out", "b": "timed out"})
    assert dedupe_by_cluster(["a", "b"]) == ([], {}, {"a": "timed out", "b": "timed out"})


@pytest.fixture
def settled_home(home):
    """The test config once loaded: its plaintext passwords are encrypted
    and saved back on the first read. Done here, before the probes run in
    parallel threads, as it is on a real config."""
    from src.config import ConfigManager

    ConfigManager().get()
    return home


def test_cluster_identity_through_the_real_probe(settled_home, api):
    api.route("GET", "/cluster/status", fixture("pve-9.1/cluster_status"), host="h1")
    api.route("GET", "/cluster/status", fixture("pve-9.2/cluster_status"), host="h2")
    kept, skipped, unreachable = dedupe_by_cluster(["p1", "p2", "p3"])
    assert (kept, skipped, unreachable) == (["p1", "p2"], {"p3": "p1"}, {})


def test_two_standalone_hosts_with_the_same_name_are_both_kept(settled_home, api):
    for host, ip in (("h1", "192.168.1.10"), ("h2", "10.20.0.10")):
        api.route("GET", "/cluster/status",
                  [{"id": "node/pve", "ip": ip, "local": 1, "name": "pve", "nodeid": 0,
                    "online": 1, "type": "node"}], host=host)
    kept, skipped, _ = dedupe_by_cluster(["p1", "p2"])
    assert kept == ["p1", "p2"] and skipped == {}


def test_fleet_exit_code_keeps_findings_apart_from_errors():
    from src.utils.fleet import _fleet_exit_code

    # audit FAIL on every failed profile: still 2, like on one profile
    assert _fleet_exit_code([2]) == 2
    assert _fleet_exit_code([2, 2]) == 2
    # one profile could not run: an error wins over a finding
    assert _fleet_exit_code([2, 1]) == 1
    assert _fleet_exit_code([1]) == 1
    assert _fleet_exit_code([3]) == 1
    # Ctrl+C wins over everything
    assert _fleet_exit_code([1, 130, 2]) == 130
