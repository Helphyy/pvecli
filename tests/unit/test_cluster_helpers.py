"""Helpers of src/cli/cluster.py: clock drift, /etc/hosts names, guest
selection and the live check after a bulk action.

Protects the preflight verdicts (a 1 s API resolution is not drift, a node
name never matches a longer one) and the bulk report (a guest the action
skipped is reported, an HA guest is followed through the HA state).
"""

from __future__ import annotations

import pytest

from src.api.client import ProxmoxClient
from src.api.exceptions import PVECliError
from src.cli.cluster import (
    _bulk_missed,
    _clock_offset,
    _drift_seconds,
    _hosts_names,
    _select_guests,
)
from src.models.config import ProfileConfig
from tests.fake_api import fixture


@pytest.fixture
def client(api) -> ProxmoxClient:
    return ProxmoxClient(ProfileConfig(
        host="h1", verify_ssl=False,
        auth={"type": "password", "user": "root@pam", "password": "x"},
    ))


# ── _drift_seconds ───────────────────────────────────────────────────────


@pytest.mark.parametrize("offsets, drift", [
    ([], 0.0),
    ([4.0], 0.0),
    ([5.0, 5.0], 0.0),
    ([0.0, 0.9], 0.0),
    ([0.0, 1.0], 0.0),   # exactly the API resolution
    ([0.0, 3.0], 2.0),
    ([-2.0, 0.5, 2.0], 3.0),
    ([10.0, 0.0, 4.0], 9.0),
])
def test_drift_seconds_deducts_the_one_second_resolution(offsets, drift):
    assert _drift_seconds(offsets) == pytest.approx(drift)


# ── _hosts_names ─────────────────────────────────────────────────────────


def test_hosts_names_from_a_real_hosts_file():
    names = _hosts_names(fixture("pve-9.2/node_hosts")["data"])
    assert {"pve-lab9", "pve-lab9.sdnlab.local", "localhost", "localhost.localdomain"} <= names
    # Addresses are not names
    assert "10.199.32.64" not in names and "::1" not in names
    assert "pve-lab1" not in names


def test_hosts_names_short_and_full_forms():
    names = _hosts_names("10.0.0.1 pve-lab1.example.com\n10.0.0.2 pve-lab2\n")
    assert {"pve-lab1", "pve-lab1.example.com", "pve-lab2"} <= names


def test_hosts_names_no_prefix_match():
    names = _hosts_names("10.0.0.10 pve-lab10.example.com pve-lab10\n")
    assert "pve-lab1" not in names
    names = _hosts_names("10.0.0.1 pve-lab1\n")
    assert "pve-lab10" not in names


def test_hosts_names_ignore_comments():
    names = _hosts_names("# 10.0.0.3 pve-lab3\n10.0.0.1 pve-lab1 # pve-lab2 is gone\n")
    assert names == {"pve-lab1"}


# ── _clock_offset ────────────────────────────────────────────────────────


def node_clock(clock, skew: float, latency: float = 0.4):
    """A /nodes/{node}/time responder whose clock runs `skew` seconds ahead
    of the local one, answering in whole seconds after `latency`."""

    def respond(call):
        clock.now += latency / 2
        t = int(clock.now + skew)
        clock.now += latency / 2
        return {"time": t, "localtime": t + 3600, "timezone": "Europe/Paris"}

    return respond


async def test_clock_offset_uses_utc_time_not_localtime(client, api, clock):
    # Real 9.2 answer: localtime is 'time' shifted by the timezone
    data = fixture("pve-9.2/node_time")
    clock.now = float(data["time"])
    api.route("GET", "/nodes/{node}/time", data)
    assert await _clock_offset(client, "pve-lab9") == pytest.approx(0.0)


async def test_clock_offset_ignores_latency(client, api, clock):
    api.route("GET", "/nodes/{node}/time", node_clock(clock, skew=5, latency=1.6))
    assert await _clock_offset(client, "pve1") == pytest.approx(5.0, abs=1.0)


async def test_clock_offset_independent_of_time_between_reads(client, api, clock):
    # Two nodes with the very same clock, read 20 minutes apart (a prompt)
    api.route("GET", "/nodes/{node}/time", node_clock(clock, skew=3))
    first = await _clock_offset(client, "pve1")
    clock.now += 1200.3
    second = await _clock_offset(client, "pve2")
    assert first == pytest.approx(3.0, abs=1.0)
    assert second == pytest.approx(3.0, abs=1.0)
    assert _drift_seconds([first, second]) == 0.0


async def test_clock_offset_sees_a_real_drift(client, api, clock):
    api.route("GET", "/nodes/pve1/time", node_clock(clock, skew=0))
    api.route("GET", "/nodes/pve2/time", node_clock(clock, skew=45))
    o1 = await _clock_offset(client, "pve1")
    clock.now += 600
    o2 = await _clock_offset(client, "pve2")
    assert 43.0 <= _drift_seconds([o1, o2]) <= 45.0


async def test_clock_offset_unreadable(client, api, clock):
    api.route("GET", "/nodes/pve1/time", api.error(500, "node offline"))
    api.route("GET", "/nodes/pve2/time", {"timezone": "UTC"})
    api.route("GET", "/nodes/pve3/time", {"time": "soon"})
    for node in ("pve1", "pve2", "pve3"):
        assert await _clock_offset(client, node) is None


# ── _select_guests ───────────────────────────────────────────────────────

RESOURCES = [
    {"vmid": 201, "type": "lxc", "node": "pve-lab1", "tags": "prod", "name": "ct"},
    {"vmid": 100, "type": "qemu", "node": "pve-lab1", "tags": "prod;web", "pool": "p1", "name": "a"},
    {"vmid": 101, "type": "qemu", "node": "pve-lab10", "tags": "prod", "name": "b"},
    {"vmid": 102, "type": "qemu", "node": "pve-lab1", "tags": "prod", "template": 1, "name": "tpl"},
    {"vmid": 103, "type": "qemu", "node": "pve-lab2", "tags": "production", "name": "c"},
]


@pytest.fixture
def resources(api):
    api.route("GET", "/cluster/resources", RESOURCES)
    return api


def vmids(guests):
    return [g["vmid"] for g in guests]


async def test_select_guests_requires_a_filter(client, resources):
    with pytest.raises(PVECliError, match="--vms, --tag, --pool or --node"):
        await _select_guests(client, None, None, None, None)
    assert resources.calls == []


@pytest.mark.parametrize("vms, tag, pool, node, expected", [
    # Both guest types; template and the 'production' tag left out
    (None, "prod", None, None, [100, 101, 201]),
    # pve-lab1 is not pve-lab10
    (None, None, None, "pve-lab1", [100, 201]),
    (None, None, "p1", None, [100]),
    ("100,101", None, None, None, [100, 101]),
    ("100-101", "prod", None, "pve-lab10", None),  # 100 filtered out: error
])
async def test_select_guests_filters(client, resources, vms, tag, pool, node, expected):
    if expected is None:
        with pytest.raises(PVECliError, match="filtered out: 100"):
            await _select_guests(client, vms, tag, pool, node)
    else:
        assert vmids(await _select_guests(client, vms, tag, pool, node)) == expected


async def test_select_guests_names_the_missing_ids(client, resources):
    with pytest.raises(PVECliError, match="Not found or filtered out: 102, 999"):
        await _select_guests(client, "100,102,999", None, None, None)


async def test_select_guests_nothing_matching(client, resources):
    with pytest.raises(PVECliError, match="No guest matches"):
        await _select_guests(client, None, "nosuchtag", None, None)


# ── _bulk_missed ─────────────────────────────────────────────────────────

VM100 = {"vmid": 100, "type": "qemu", "node": "pve1"}
VM101 = {"vmid": 101, "type": "qemu", "node": "pve1"}
CT200 = {"vmid": 200, "type": "lxc", "node": "pve1"}


def status_routes(api, states: dict[tuple[str, int], dict | Exception]):
    """Route /nodes/{node}/{kind}/{vmid}/status/current per (node, vmid)."""

    def respond(call):
        key = (call.match["node"], int(call.match["vmid"]))
        value = states.get(key, api.error(500, f"Configuration file for {key[1]} does not exist"))
        if isinstance(value, Exception):
            raise value
        return value

    for kind in ("qemu", "lxc"):
        api.route("GET", f"/nodes/{{node}}/{kind}/{{vmid}}/status/current", respond)


async def test_bulk_start_reports_a_skipped_guest(client, api):
    status_routes(api, {
        ("pve1", 100): {"status": "running", "qmpstatus": "running"},
        ("pve1", 200): {"status": "stopped"},
    })
    assert await _bulk_missed(client, "start", [VM100, CT200], None) == ["200 (stopped)"]


async def test_bulk_start_reports_lock_and_unreadable(client, api):
    status_routes(api, {("pve1", 100): {"status": "stopped", "lock": "backup"}})
    missed = await _bulk_missed(client, "start", [VM100, VM101], None)
    assert sorted(missed) == ["100 (stopped, locked backup)", "101 (unreadable)"]


async def test_bulk_shutdown(client, api):
    status_routes(api, {
        ("pve1", 100): {"status": "stopped"},
        ("pve1", 200): {"status": "running"},
    })
    assert await _bulk_missed(client, "shutdown", [VM100, CT200], None) == ["200 (running)"]


async def test_bulk_suspend(client, api):
    status_routes(api, {
        ("pve1", 100): {"status": "running", "qmpstatus": "paused"},
        # Hibernated to disk: stopped with a 'suspended' lock
        ("pve1", 101): {"status": "stopped", "lock": "suspended"},
    })
    assert await _bulk_missed(client, "suspend", [VM100, VM101], None) == []

    status_routes(api, {("pve1", 100): {"status": "running", "qmpstatus": "running"}})
    assert await _bulk_missed(client, "suspend", [VM100], None) == ["100 (running)"]


async def test_bulk_migrate_checks_the_target_node(client, api):
    api.route("GET", "/cluster/ha/resources", [])
    status_routes(api, {
        ("pve2", 100): {"status": "running"},
        # 101 still answers on pve1 only: it did not move
        ("pve1", 101): {"status": "running"},
    })
    missed = await _bulk_missed(client, "migrate", [VM100, VM101], "pve2")
    assert missed == ["101 (still on pve1)"]
    assert {c.path for c in api.called("GET", "/nodes/{node}/qemu/{vmid}/status/current")} == {
        "/nodes/pve2/qemu/100/status/current", "/nodes/pve2/qemu/101/status/current",
    }


async def test_bulk_migrate_without_ha_listing_falls_back_on_config(client, api):
    api.route("GET", "/cluster/ha/resources", api.forbidden("/", "Sys.Audit"))
    status_routes(api, {("pve2", 100): {"status": "running"}})
    assert await _bulk_missed(client, "migrate", [VM100], "pve2") == []


async def test_bulk_migrate_follows_ha_state(client, api, clock):
    api.route("GET", "/cluster/ha/resources", [{"sid": "vm:100", "state": "started"}])
    # The config already sits on pve2 while the HA manager is still moving it
    status_routes(api, {("pve2", 100): {"status": "running"}})
    states = iter([
        {"sid": "vm:100", "state": "migrate", "node": "pve1"},
        {"sid": "vm:100", "state": "migrate", "node": "pve2"},
        {"sid": "vm:100", "state": "started", "node": "pve2"},
    ])
    seen = []

    def ha_status(call):
        entry = next(states)
        seen.append(entry["state"])
        return [{"type": "quorum", "status": "OK"}, {"type": "service", **entry}]

    api.route("GET", "/cluster/ha/status/current", ha_status)
    assert await _bulk_missed(client, "migrate", [VM100], "pve2") == []
    # Settled only once the HA state said so, not on the config location
    assert seen == ["migrate", "migrate", "started"]
    assert api.called("GET", "/nodes/{node}/qemu/{vmid}/status/current") == []


async def test_bulk_migrate_reports_an_ha_request_never_acted_on(client, api, clock):
    api.route("GET", "/cluster/ha/resources", [{"sid": "ct:200"}])
    api.route("GET", "/cluster/ha/status/current",
              [{"type": "service", "sid": "ct:200", "state": "request_stop", "node": "pve1"}])
    missed = await _bulk_missed(client, "migrate", [CT200], "pve2")
    assert len(missed) == 1
    assert missed[0].startswith("200 (") and "did not act" in missed[0]


async def test_bulk_migrate_mixes_ha_and_plain_guests(client, api, clock):
    api.route("GET", "/cluster/ha/resources", [{"sid": "vm:100"}, {"sid": "ct:101"}])
    api.route("GET", "/cluster/ha/status/current",
              [{"type": "service", "sid": "vm:100", "state": "started", "node": "pve2"}])
    # vm:100 is HA, VM 101 is not: the HA container ct:101 is another guest
    status_routes(api, {})
    missed = await _bulk_missed(client, "migrate", [VM100, VM101], "pve2")
    assert missed == ["101 (still on pve1)"]
