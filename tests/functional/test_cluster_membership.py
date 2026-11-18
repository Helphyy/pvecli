"""cluster create and cluster join: typed confirmation, refusals, what is sent."""

from __future__ import annotations

from typing import Any

import pytest

from tests.fake_api import FakeAPI

NOW = 1_800_000_000
TARGET = "10.0.0.9"
FP = "AA:BB:CC:DD"


# ── cluster create ───────────────────────────────────────────────────────

def create_setup(api: FakeAPI, totem: Any = None, guests: list[dict[str, Any]] | None = None) -> None:
    # A standalone node answers data:null on the totem
    api.route("GET", "/cluster/config/totem", totem)
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}])
    api.route("GET", "/cluster/resources", guests or [])
    api.route("POST", "/cluster/config", "UPID:pve1:0:0:0:clustercreate::root@pam:")
    api.route("GET", "/cluster/status", [{"type": "cluster", "name": "lab", "quorate": 1, "nodes": 1},
                                         {"type": "node", "name": "pve1", "online": 1}])


def test_create_with_typed_confirmation(cli, api, ui):
    create_setup(api)
    ui.text("lab")
    r = cli("cluster", "create", "lab", "--link0", "10.0.0.1", "--link1", "10.1.0.1", "-p", "p1")
    assert r.exit_code == 0, r.output
    (w,) = api.writes()
    assert (w.method, w.path) == ("POST", "/cluster/config")
    assert w.data == {"clustername": "lab", "link0": "10.0.0.1", "link1": "10.1.0.1"}
    assert "created and quorate" in r.output
    assert ui.left() == {}


def test_create_wrong_typed_name_writes_nothing(cli, api, ui):
    create_setup(api)
    ui.text("lba")
    r = cli("cluster", "create", "lab", "-p", "p1")
    assert r.exit_code == 1
    assert "does not match" in r.output
    assert api.writes() == []


def test_create_refused_when_already_in_a_cluster(cli, api):
    create_setup(api, totem={"cluster_name": "prod"})
    r = cli("cluster", "create", "lab", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "already belongs to cluster 'prod'" in r.output
    assert api.writes() == []


def test_create_asks_the_name_and_empty_cancels(cli, api, ui):
    create_setup(api)
    ui.text(None)
    r = cli("cluster", "create", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_create_warns_about_existing_guests(cli, api):
    create_setup(api, guests=[{"type": "qemu", "vmid": 100}])
    r = cli("cluster", "create", "lab", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "1 guest(s) already live" in r.output
    assert api.writes()[0].data == {"clustername": "lab"}


def test_create_never_quorate_times_out(cli, api, clock):
    create_setup(api)
    api.route("GET", "/cluster/status", [{"type": "cluster", "name": "lab", "quorate": 0}])
    r = cli("cluster", "create", "lab", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "did not report itself quorate" in r.output
    assert len(api.called("POST", "/cluster/config")) == 1


def test_create_api_error_exits_1(cli, api):
    create_setup(api)
    api.route("POST", "/cluster/config", api.error(500, "cluster config '/etc/pve/corosync.conf' already exists"))
    r = cli("cluster", "create", "lab", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "already exists" in r.output
    assert not api.called("GET", "/cluster/status")


# ── cluster join ─────────────────────────────────────────────────────────

def join_setup(
    api: FakeAPI,
    target_totem: Any = None,
    target_version: tuple[str, str] = ("9.1.1", "9.1"),
    peer_version: tuple[str, str] = ("9.1.1", "9.1"),
    target_skew: int = 0,
    nodelist: list[dict[str, Any]] | None = None,
    target_guests: list[dict[str, Any]] | None = None,
) -> None:
    # Cluster side, reached through the profile host h1
    api.route("GET", "/cluster/config/join", {
        "nodelist": nodelist or [
            {"name": "pve1", "pve_addr": "h1", "pve_fp": FP, "ring0_addr": "10.0.0.1"},
            {"name": "pve2", "pve_addr": "10.0.0.2", "pve_fp": "EE:FF", "ring0_addr": "10.0.0.2"},
        ],
        "totem": {"cluster_name": "lab"},
    }, host="h1")
    api.route("GET", "/nodes/{node}/time", {"time": NOW}, host="h1")
    api.route("GET", "/nodes/{node}/version",
              dict(zip(("version", "release"), peer_version, strict=True)), host="h1")
    api.route("GET", "/cluster/status", [
        {"type": "cluster", "name": "lab", "quorate": 1},
        {"type": "node", "name": "pve1"}, {"type": "node", "name": "pve2"}, {"type": "node", "name": "new"},
    ], host="h1")
    # Joining side, reached directly on its address
    api.route("GET", "/cluster/config/totem", target_totem, host=TARGET)
    api.route("GET", "/nodes", [{"node": "new", "status": "online"}], host=TARGET)
    api.route("GET", "/nodes/{node}/version",
              dict(zip(("version", "release"), target_version, strict=True)), host=TARGET)
    api.route("GET", "/nodes/{node}/time", {"time": NOW + target_skew}, host=TARGET)
    api.route("GET", "/cluster/resources", target_guests or [], host=TARGET)
    api.route("POST", "/cluster/config/join", "UPID:new:0:0:0:clusterjoin::root@pam:", host=TARGET)


@pytest.fixture
def target_password(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("NEW_PW", "newsecret")
    return ["--target-password-env", "NEW_PW"]


def test_join_sends_the_peer_and_its_own_fingerprint(cli, api, ui, target_password):
    join_setup(api)
    ui.text("lab")
    r = cli("cluster", "join", TARGET, *target_password, "--link0", "10.0.0.9", "-p", "p1")
    assert r.exit_code == 0, r.output
    (w,) = api.writes()
    assert (w.host, w.path) == (TARGET, "/cluster/config/join")
    # The profile is root@pam: its password is the peer's root password
    assert w.data == {"hostname": "h1", "password": "secret", "fingerprint": FP, "link0": "10.0.0.9"}
    assert "'new' joined cluster 'lab'" in r.output
    assert ui.left() == {}


def test_join_wrong_typed_name_writes_nothing(cli, api, ui, target_password):
    join_setup(api)
    ui.text("lab2")
    r = cli("cluster", "join", TARGET, *target_password, "-p", "p1")
    assert r.exit_code == 1
    assert "does not match" in r.output
    assert api.writes() == []


def test_join_refused_when_target_already_in_a_cluster(cli, api, target_password):
    join_setup(api, target_totem={"cluster_name": "other"})
    r = cli("cluster", "join", TARGET, *target_password, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "already belongs to cluster 'other'" in r.output
    assert api.writes() == []


def test_join_refused_when_profile_is_not_a_cluster(cli, api, target_password):
    join_setup(api)
    api.route("GET", "/cluster/config/join", api.error(500, "unable to read file: node is not in a cluster"), host="h1")
    api.route("GET", "/cluster/config/totem", None, host="h1")
    r = cli("cluster", "join", TARGET, *target_password, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "does not point at a cluster" in r.output
    assert api.writes() == []


def test_join_refused_across_releases(cli, api, target_password):
    join_setup(api, target_version=("9.2.28", "9.2"))
    r = cli("cluster", "join", TARGET, *target_password, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "PVE releases differ" in r.output
    assert api.writes() == []


def test_join_allowed_across_patch_levels_with_a_warning(cli, api, target_password):
    join_setup(api, target_version=("9.1.2", "9.1"))
    r = cli("cluster", "join", TARGET, *target_password, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "patch levels differ" in r.output
    assert len(api.writes()) == 1


def test_join_refused_on_large_clock_drift(cli, api, target_password):
    join_setup(api, target_skew=45)
    r = cli("cluster", "join", TARGET, *target_password, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Clocks differ by 44s" in r.output
    assert api.writes() == []


def test_join_warns_on_small_clock_drift(cli, api, target_password):
    join_setup(api, target_skew=10)
    r = cli("cluster", "join", TARGET, *target_password, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Clocks differ by 9s" in r.output


def test_join_two_link_cluster_needs_link1(cli, api, target_password):
    join_setup(api, nodelist=[
        {"name": "pve1", "pve_addr": "h1", "pve_fp": FP, "ring0_addr": "10.0.0.1", "ring1_addr": "10.1.0.1"},
    ])
    r = cli("cluster", "join", TARGET, *target_password, "--link0", "10.0.0.9", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "--link1 <address>" in r.output
    assert api.writes() == []


def test_join_two_link_cluster_with_both_links(cli, api, target_password):
    join_setup(api, nodelist=[
        {"name": "pve1", "pve_addr": "h1", "pve_fp": FP, "ring0_addr": "10.0.0.1", "ring1_addr": "10.1.0.1"},
    ])
    r = cli("cluster", "join", TARGET, *target_password, "--link0", "10.0.0.9", "--link1", "10.1.0.9",
            "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes()[0].data["link1"] == "10.1.0.9"


def test_join_profile_host_not_a_member_uses_the_fingerprint_of_the_chosen_peer(cli, api, target_password):
    # The profile host matches no nodelist entry: the peer handed to the
    # joining node and the fingerprint must describe the same node
    join_setup(api, nodelist=[
        {"name": "pve2", "pve_addr": "10.0.0.2", "pve_fp": "EE:FF", "ring0_addr": "10.0.0.2"},
        {"name": "pve3", "pve_addr": "10.0.0.3", "pve_fp": "11:22", "ring0_addr": "10.0.0.3"},
    ])
    r = cli("cluster", "join", TARGET, *target_password, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    data = api.writes()[0].data
    assert (data["hostname"], data["fingerprint"]) == ("10.0.0.2", "EE:FF")


def test_join_warns_about_guests_on_the_joining_node(cli, api, target_password):
    join_setup(api, target_guests=[{"type": "qemu", "vmid": 100}, {"type": "lxc", "vmid": 101}])
    r = cli("cluster", "join", TARGET, *target_password, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "2 guest(s) exist on new" in r.output


def test_join_empty_target_password_writes_nothing(cli, api, ui):
    join_setup(api)
    ui.text(None)
    r = cli("cluster", "join", TARGET, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "password is required" in r.output
    assert api.writes() == []


def test_join_dropped_connection_is_verified_from_the_cluster(cli, api, target_password):
    join_setup(api)
    api.route("POST", "/cluster/config/join", api.error(596, "connection reset"), host=TARGET)
    r = cli("cluster", "join", TARGET, *target_password, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Connection dropped" in r.output
    assert "joined cluster 'lab'" in r.output


def test_join_node_never_appears_fails(cli, api, target_password):
    join_setup(api)
    api.route("GET", "/cluster/status", [{"type": "cluster", "name": "lab", "quorate": 1},
                                         {"type": "node", "name": "pve1"}], host="h1")
    r = cli("cluster", "join", TARGET, *target_password, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "did not show up" in r.output
