"""pvecli snapshot add/list/prune against the fake API (guests from a real PVE 9.1 lab)."""

from __future__ import annotations

import json

import pytest

from tests.fake_api import fixture, upid

NOW = 1_800_000_000  # FakeClock start
DAY = 86400

# pve-9.1 lab: 300 (qemu, pve-lab2), 301 (qemu, pve-lab1), 311 (lxc, running),
# 9000 (template, never a target)
GUESTS = fixture("pve-9.1/cluster_resources_vm")


def snap(name: str, age_days: float, parent: str | None = None, vmstate: int = 0) -> dict:
    """A snapshot entry as GET .../snapshot returns it."""
    s = {"name": name, "snaptime": int(NOW - age_days * DAY), "description": "", "vmstate": vmstate}
    if parent:
        s["parent"] = parent
    return s


def current(parent: str | None = None) -> dict:
    """The 'current' pseudo snapshot PVE always appends (see fixture qemu_snapshot)."""
    c = fixture("pve-9.1/qemu_snapshot")[0]
    if parent:
        c["parent"] = parent
    return c


def setup(api, snaps: dict[int, list[dict]] | None = None, guests: list[dict] | None = None) -> None:
    snaps = snaps or {}
    api.route("GET", "/cluster/resources", guests if guests is not None else GUESTS)
    for g in guests if guests is not None else GUESTS:
        base = f"/nodes/{g['node']}/{g['type']}/{g['vmid']}"
        lst = snaps.get(g["vmid"], [])
        api.route("GET", f"{base}/snapshot", [*lst, current(lst[-1]["name"] if lst else None)])
    api.route("POST", "/nodes/{node}/{type}/{vmid}/snapshot",
              lambda c: upid(c.match["node"], "qmsnapshot", c.match["vmid"]))
    api.route("DELETE", "/nodes/{node}/{type}/{vmid}/snapshot/{snapname}",
              lambda c: upid(c.match["node"], "qmdelsnapshot", c.match["vmid"]))
    api.route("GET", "/access/permissions", lambda c: {c.params["path"]: {"VM.Audit": 1}})


# ── guest selection ─────────────────────────────────────────────────────


@pytest.mark.parametrize("cmd", [["add", "pre-up"], ["prune", "--older-than", "7d"]])
def test_refuses_without_filter_nor_all(cli, api, cmd):
    setup(api)
    r = cli("snapshot", *cmd, "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "--all" in r.output
    assert api.writes() == []


def test_unmatched_filter_is_an_error(cli, api):
    setup(api)
    r = cli("snapshot", "add", "x", "--tag", "nope", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "No guest matches" in r.output
    assert api.writes() == []


# ── add ─────────────────────────────────────────────────────────────────


def test_add_all_skips_templates_and_guests_already_having_the_name(cli, api):
    setup(api, {301: [snap("pre-up", 2)]})
    r = cli("snapshot", "add", "pre-up", "--all", "-y", "-d", "before upgrade", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "skipped: 301" in r.output
    posts = {c.path: c.data for c in api.writes()}
    assert posts == {
        "/nodes/pve-lab2/qemu/300/snapshot": {"snapname": "pre-up", "description": "before upgrade"},
        "/nodes/pve-lab1/lxc/311/snapshot": {"snapname": "pre-up", "description": "before upgrade"},
    }
    assert "2/2" in r.output


def test_add_nothing_left_when_every_guest_has_it(cli, api):
    setup(api, {300: [snap("s", 1)]})
    r = cli("snapshot", "add", "s", "--vms", "300", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Nothing to do" in r.output
    assert api.writes() == []


def test_add_asks_and_writes_nothing_on_no(cli, api):
    setup(api)
    r = cli("snapshot", "add", "s", "--vms", "300,301", "-p", "p1", input="n\n")
    assert r.exit_code == 0, r.output
    assert api.writes() == []


def test_add_vmstate_only_for_running_vms(cli, api):
    guests = [dict(g) for g in GUESTS]
    for g in guests:
        if g["vmid"] == 300:
            g["status"] = "running"
    setup(api, guests=guests)
    r = cli("snapshot", "add", "s", "--all", "--vmstate", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    data = {c.path: c.data for c in api.writes()}
    assert data["/nodes/pve-lab2/qemu/300/snapshot"] == {"snapname": "s", "vmstate": 1}
    assert data["/nodes/pve-lab1/qemu/301/snapshot"] == {"snapname": "s"}  # stopped
    assert data["/nodes/pve-lab1/lxc/311/snapshot"] == {"snapname": "s"}  # CT has no RAM state


def test_add_failed_task_is_reported_and_exit_1(cli, api):
    setup(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status",
              lambda c: {"status": "stopped", "exitstatus": "snapshot feature is not available"}
              if ":300:" in c.match["upid"] else {"status": "stopped", "exitstatus": "OK"})
    r = cli("snapshot", "add", "s", "--vms", "300,301", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "300" in r.output and "1/2" in r.output
    assert len(api.writes()) == 2


# ── list ────────────────────────────────────────────────────────────────


def test_list_json_hides_current_and_filters_by_age_and_prefix(cli, api):
    setup(api, {300: [snap("pre-a", 40), snap("pre-b", 1, "pre-a")], 311: [snap("manual", 50)]})
    r = cli("snapshot", "list", "--older-than", "30d", "--name-prefix", "pre-", "--json", "-p", "p1")
    assert r.exit_code == 0, r.output
    rows = json.loads(r.stdout)
    assert [(x["vmid"], x["snapshot"]) for x in rows] == [(300, "pre-a")]
    assert "_guest" not in rows[0]

    r = cli("snapshot", "list", "--json", "-p", "p1")
    names = {(x["vmid"], x["snapshot"], x["type"]) for x in json.loads(r.stdout)}
    assert names == {(300, "pre-a", "VM"), (300, "pre-b", "VM"), (311, "manual", "CT")}
    assert api.writes() == []


def test_list_table(cli, api):
    setup(api, {301: [snap("before-fix", 3)]})
    r = cli("snapshot", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "before-fix" in r.stdout and "3d" in r.stdout
    assert "current" not in r.stdout


# ── prune ───────────────────────────────────────────────────────────────


def test_prune_shows_then_deletes_nothing_on_no(cli, api):
    setup(api, {300: [snap("old", 30)]})
    r = cli("snapshot", "prune", "--older-than", "7d", "--all", "-p", "p1", input="n\n")
    assert r.exit_code == 0, r.output
    assert "old" in r.stdout and "1 snapshot(s) to delete" in r.stdout
    assert api.writes() == []


def test_prune_yes_on_prompt_deletes(cli, api):
    setup(api, {300: [snap("old", 30)]})
    r = cli("snapshot", "prune", "--older-than", "7d", "--all", "-p", "p1", input="y\n")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == ["/nodes/pve-lab2/qemu/300/snapshot/old"]


def test_prune_keep_1_keeps_newest_per_guest(cli, api):
    setup(api, {
        300: [snap("a", 40), snap("b", 30, "a"), snap("c", 20, "b")],
        301: [snap("x", 60), snap("y", 50, "x")],
        311: [snap("only", 90)],
    })
    r = cli("snapshot", "prune", "--older-than", "7d", "--keep", "1", "--all", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    deletes = [c.path for c in api.writes()]
    assert all(c.method == "DELETE" for c in api.writes())
    assert sorted(deletes) == sorted([
        "/nodes/pve-lab2/qemu/300/snapshot/a",
        "/nodes/pve-lab2/qemu/300/snapshot/b",
        "/nodes/pve-lab1/qemu/301/snapshot/x",
    ])
    # one guest's snapshots go oldest first (each locks the guest)
    assert [d for d in deletes if "/300/" in d] == [
        "/nodes/pve-lab2/qemu/300/snapshot/a", "/nodes/pve-lab2/qemu/300/snapshot/b"]
    assert "3/3" in r.output


def test_prune_respects_age_and_prefix(cli, api):
    setup(api, {300: [snap("pre-old", 30), snap("manual-old", 30), snap("pre-new", 2)]})
    r = cli("snapshot", "prune", "--older-than", "7d", "--name-prefix", "pre-", "--vms", "300",
            "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == ["/nodes/pve-lab2/qemu/300/snapshot/pre-old"]


def test_prune_nothing_to_do(cli, api):
    setup(api, {300: [snap("recent", 1)]})
    r = cli("snapshot", "prune", "--older-than", "7d", "--all", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Nothing to prune" in r.output
    assert api.writes() == []


# ── what could not be read ──────────────────────────────────────────────


def test_list_names_the_guests_a_pool_limited_user_cannot_see(cli, api):
    # Seen on the lab with PVEAuditor on one pool: "No snapshot found",
    # while the guests outside the pool were never looked at
    setup(api)
    api.route("GET", "/access/permissions", lambda c: {c.params["path"]: {}})
    r = cli("snapshot", "list", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout) == []
    assert "VM.Audit on /vms" in r.stderr


def test_list_json_stays_valid_when_a_guest_is_unreadable(cli, api):
    setup(api, {301: [snap("a", 1)]})
    api.route("GET", "/nodes/pve-lab2/qemu/300/snapshot", lambda c: api.error(500, "node pve-lab2 offline"))
    r = cli("snapshot", "list", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    assert [x["vmid"] for x in json.loads(r.stdout)] == [301]
    assert "300" in r.stderr and "unreadable" in r.stderr
