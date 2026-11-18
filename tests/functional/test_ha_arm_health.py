"""Cluster wide HA: arm/disarm (PVE 9.2+), HA during cluster shutdown, cluster health."""

from __future__ import annotations

import json
from typing import Any

from tests.fake_api import FakeAPI, fixture

DISARM = "/cluster/ha/status/disarm-ha"
ARM = "/cluster/ha/status/arm-ha"


def recorded(api: FakeAPI, name: str) -> Any:
    data = fixture(name)
    if isinstance(data, dict) and "__error__" in data:
        return api.error(data["__status__"], data["__error__"])
    return data


# ── cluster ha disarm / arm ──────────────────────────────────────────────

def test_disarm_on_old_cluster_says_it_needs_92(cli, api):
    api.route("POST", DISARM, api.not_implemented("POST", DISARM))
    r = cli("cluster", "ha", "disarm", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "needs PVE 9.2" in r.output


def test_disarm_on_92_posts_resource_mode(cli, api):
    api.route("POST", DISARM, None)
    r = cli("cluster", "ha", "disarm", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    (w,) = api.writes()
    assert (w.method, w.path) == ("POST", DISARM)
    assert w.data == {"resource-mode": "freeze"}
    assert "pvecli cluster ha arm" in r.output


def test_disarm_ignore_mode_is_passed(cli, api):
    api.route("POST", DISARM, None)
    r = cli("cluster", "ha", "disarm", "--mode", "ignore", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes()[0].data == {"resource-mode": "ignore"}


def test_disarm_invalid_mode_writes_nothing(cli, api):
    r = cli("cluster", "ha", "disarm", "--mode", "off", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_arm_on_old_cluster_says_it_needs_92(cli, api):
    api.route("POST", ARM, api.not_implemented("POST", ARM))
    r = cli("cluster", "ha", "arm", "-p", "p1")
    assert r.exit_code == 1
    assert "needs PVE 9.2" in r.output


def test_arm_on_92(cli, api):
    api.route("POST", ARM, None)
    r = cli("cluster", "ha", "arm", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [(w.method, w.path) for w in api.writes()] == [("POST", ARM)]


# ── cluster shutdown: HA phase ───────────────────────────────────────────

def shutdown_setup(api: FakeAPI) -> None:
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"}])
    api.route("GET", "/cluster/status", [
        {"type": "cluster", "name": "c", "quorate": 1},
        {"type": "node", "name": "pve1", "ip": "h1", "online": 1},
        {"type": "node", "name": "pve2", "ip": "10.0.0.2", "online": 1},
    ])
    api.route("GET", "/cluster/resources", [])
    api.route("GET", "/cluster/ha/resources", [
        {"sid": "vm:100", "state": "started"},
        {"sid": "ct:101", "state": "disabled"},
    ])
    api.route("PUT", "/cluster/ha/resources/{sid}", None)
    api.route("POST", "/nodes/{node}/status", None)


def test_shutdown_disarms_ha_on_92(cli, api):
    shutdown_setup(api)
    api.route("POST", DISARM, None)
    r = cli("cluster", "shutdown", "-y", "--skip-ceph", "-p", "p1")
    assert r.exit_code == 0, r.output
    (disarm,) = api.called("POST", DISARM)
    assert disarm.data == {"resource-mode": "freeze"}
    assert not api.called("PUT", "/cluster/ha/resources/{sid}")
    assert "pvecli cluster ha arm" in r.output
    # HA is handled before any node goes down, connected node last
    paths = [w.path for w in api.writes()]
    assert paths == [DISARM, "/nodes/pve2/status", "/nodes/pve1/status"]


def test_shutdown_disables_each_active_resource_before_92(cli, api):
    shutdown_setup(api)
    api.route("POST", DISARM, api.not_implemented("POST", DISARM))
    r = cli("cluster", "shutdown", "-y", "--skip-ceph", "-p", "p1")
    assert r.exit_code == 0, r.output
    (put,) = api.called("PUT", "/cluster/ha/resources/{sid}")
    assert put.path == "/cluster/ha/resources/vm:100"
    assert put.data == {"state": "disabled"}
    paths = [w.path for w in api.writes() if w.path != DISARM]
    assert paths == ["/cluster/ha/resources/vm:100", "/nodes/pve2/status", "/nodes/pve1/status"]
    assert "vm:100" in r.output


def test_shutdown_skip_ha_touches_no_ha(cli, api):
    shutdown_setup(api)
    api.route("POST", DISARM, None)
    r = cli("cluster", "shutdown", "-y", "--skip-ceph", "--skip-ha", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert not [w for w in api.writes() if "/cluster/ha/" in w.path]


# ── cluster health ───────────────────────────────────────────────────────

def health_setup(api: FakeAPI, ha_status: list[dict[str, Any]]) -> None:
    api.route("GET", "/cluster/status", fixture("pve-9.1/cluster_status"))
    api.route("GET", "/cluster/config/join", api.error(500, "no join info"))
    api.route("GET", "/cluster/config/qdevice", {})
    api.route("GET", "/cluster/ha/status/current", ha_status)
    api.route("GET", "/cluster/ceph/status", recorded(api, "pve-9.2/ceph_status"))


SERVICES = [
    {"id": "service:vm:100", "sid": "vm:100", "type": "service", "node": "pve-lab9", "state": "started"},
    {"id": "service:ct:101", "sid": "ct:101", "type": "service", "node": "pve-lab9", "state": "started"},
]


def test_health_counts_only_service_entries_and_shows_armed_state(cli, api):
    # Real 9.2 answer: quorum, master, fencing (armed-state standby), one LRM
    health_setup(api, fixture("pve-9.2/ha_status_current") + SERVICES)
    r = cli("cluster", "health", "--json", "-p", "p1")
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["ha_resources"] == 2
    assert data["ha_armed_state"] == "standby"


def test_health_real_92_status_has_no_ha_resource(cli, api):
    health_setup(api, fixture("pve-9.2/ha_status_current"))
    r = cli("cluster", "health", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "standby" in r.stdout
    assert "no HA resource" in r.stdout


def test_health_91_master_and_lrms_are_not_resources(cli, api):
    # Real 9.1 answer: 1 quorum, 1 master, 5 LRM, no armed-state field
    health_setup(api, fixture("pve-9.1/ha_status_current"))
    r = cli("cluster", "health", "--json", "-p", "p1")
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["ha_resources"] == 0
    assert data["ha_armed_state"] is None
    text = cli("cluster", "health", "-p", "p1")
    assert "no HA resource" in text.stdout


def test_health_disarmed_suggests_rearm(cli, api):
    status = [dict(e) for e in fixture("pve-9.2/ha_status_current")]
    for e in status:
        if "armed-state" in e:
            e["armed-state"] = "disarmed"
    health_setup(api, status + SERVICES)
    r = cli("cluster", "health", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "disarmed" in r.stdout
    assert "2 resource(s) under HA" in r.stdout
    assert "pvecli cluster ha arm" in r.stdout
