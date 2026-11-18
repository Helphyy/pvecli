"""`pvecli node maintenance enter/exit/status` against a stateful fake cluster."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.fake_api import fixture, upid

ALL_PRIVS = {"VM.Migrate": 1, "Sys.Modify": 1, "Sys.Console": 1, "Sys.Audit": 1}


class Lab:
    """The pve-9.1 lab (cluster sdnlab, pve-lab1..5) whose state follows
    the writes: migrations move guests, Ceph flags and HA failback stick."""

    def __init__(self, api: Any, ceph: bool = True, ha: tuple[str, ...] = (), noout: bool = False,
                 privs: dict[str, dict] | None = None) -> None:
        self.api = api
        self.guests = fixture("pve-9.1/cluster_resources_vm")  # 301, 311 (+ template 9000) on pve-lab1
        self.flags = {f["name"]: f["value"] for f in fixture("pve-9.1/ceph_flags")}
        self.flags["noout"] = int(noout)
        self.ha = {sid: {"sid": sid, "type": sid.split(":")[0], "state": "started", "failback": 1,
                         "digest": "0" * 40} for sid in ha}
        self.privs = privs or {}

        api.route("GET", "/cluster/status", fixture("pve-9.1/cluster_status"))
        api.route("GET", "/nodes", fixture("pve-9.1/nodes"))
        api.route("GET", "/cluster/resources", lambda c: self.guests)
        if ceph:
            api.route("GET", "/cluster/ceph/status", fixture("pve-9.1/ceph_status"))
        else:
            # What PVE answers on a cluster without Ceph
            api.route("GET", "/cluster/ceph/status", api.error(500, "binary not installed: /usr/bin/ceph-mon"))
        api.route("GET", "/cluster/ceph/flags", lambda c: [{"name": k, "value": v} for k, v in self.flags.items()])
        api.route("PUT", "/cluster/ceph/flags/{flag}", self._set_flag)
        api.route("GET", "/cluster/ha/resources", lambda c: list(self.ha.values()))
        api.route("GET", "/cluster/ha/resources/{sid}", lambda c: self.ha[c.match["sid"]])
        api.route("PUT", "/cluster/ha/resources/{sid}", self._update_ha)
        g = fixture("pve-9.1/ha_groups")  # PVE 9: groups migrated to rules
        api.route("GET", "/cluster/ha/groups", api.error(g["__status__"], g["__error__"]))
        api.route("GET", "/cluster/ha/status/current", self._ha_status)
        api.route("GET", "/access/permissions",
                  lambda c: {c.params["path"]: self.privs.get(c.params["path"], ALL_PRIVS)})
        api.route("POST", "/cluster/bulk-action/guest/migrate", self._migrate)
        api.route("GET", "/nodes/{node}/{kind}/{vmid}/status/current", self._status)

    def _set_flag(self, call: Any) -> None:
        self.flags[call.match["flag"]] = int(call.data["value"])

    def _update_ha(self, call: Any) -> None:
        self.ha[call.match["sid"]].update(call.data)

    def _migrate(self, call: Any) -> str:
        for g in self.guests:
            if g["vmid"] in call.data["vms"]:
                g["node"] = call.data["target"]
        return upid("pve-lab1", "bulkmigrate")

    def _status(self, call: Any) -> dict:
        g = next(g for g in self.guests if str(g["vmid"]) == call.match["vmid"])
        if g["node"] != call.match["node"]:
            from src.api.exceptions import APIError
            raise APIError(f"Configuration file 'nodes/{call.match['node']}/qemu-server/{g['vmid']}.conf' does not exist", status_code=500)
        return {"status": g["status"], "vmid": g["vmid"]}

    def _ha_status(self, call: Any) -> list:
        entries = fixture("pve-9.1/ha_status_current")
        for sid in self.ha:
            g = next(g for g in self.guests if g["vmid"] == int(sid.split(":")[1]))
            entries.append({"id": f"service:{sid}", "sid": sid, "node": g["node"], "state": "started",
                            "status": "started", "type": "service"})
        return entries

    def node_of(self, vmid: int) -> str:
        return next(g["node"] for g in self.guests if g["vmid"] == vmid)


def records(home) -> list[str]:
    d = home / ".config" / "pvecli" / "maintenance"
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def writes(api) -> list[tuple[str, str, Any]]:
    return [(c.method, c.path, c.data) for c in api.writes()]


# ── enter ────────────────────────────────────────────────────────────────


def test_enter_sets_noout_then_migrates_and_records(cli, api, home):
    lab = Lab(api)
    r = cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [
        ("PUT", "/cluster/ceph/flags/noout", {"value": 1}),
        ("POST", "/cluster/bulk-action/guest/migrate",
         {"vms": [301, 311], "target": "pve-lab2", "online": 1}),
    ]
    # The template stays, the guests reached the target
    assert lab.node_of(301) == lab.node_of(311) == "pve-lab2"
    assert lab.node_of(9000) == "pve-lab1"
    assert records(home) == ["sdnlab__pve-lab1.json"]
    state = json.loads((home / ".config/pvecli/maintenance/sdnlab__pve-lab1.json").read_text())
    assert state["noout_set"] is True and state["target"] == "pve-lab2"
    assert {g["vmid"] for g in state["guests"]} == {301, 311}


def test_enter_default_target_is_the_node_with_most_free_memory(cli, api):
    Lab(api)
    r = cli("node", "maintenance", "enter", "pve-lab1", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    # pve-lab4 has the most free memory in the recorded /nodes
    assert api.called("POST", "/cluster/bulk-action/guest/migrate")[0].data["target"] == "pve-lab4"


def test_enter_without_ceph_sets_no_flag(cli, api):
    Lab(api, ceph=False)
    r = cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [w[1] for w in writes(api)] == ["/cluster/bulk-action/guest/migrate"]


def test_enter_leaves_an_existing_noout_alone(cli, api, home):
    Lab(api, noout=True)
    r = cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("PUT", "/cluster/ceph/flags/{flag}") == []
    state = json.loads((home / ".config/pvecli/maintenance/sdnlab__pve-lab1.json").read_text())
    assert state["noout_set"] is False


@pytest.mark.parametrize("privs, expected", [
    ({"/vms/311": {"VM.Audit": 1}}, "VM.Migrate on 311"),
    ({"/": {"Sys.Audit": 1, "Sys.Console": 1}}, "Sys.Modify on /"),
    ({"/": {"Sys.Audit": 1}, "/vms/301": {}, "/vms/311": {}},
     "Sys.Modify on / (Ceph noout flag, or pass --no-ceph-flag); VM.Migrate on 301, 311"),
])
def test_enter_missing_privileges_refused_before_any_write(cli, api, home, privs, expected):
    Lab(api, privs=privs)
    r = cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "nothing was changed" in r.output
    assert expected in r.output.replace("\n", " ")
    assert api.writes() == []
    assert records(home) == []


def test_enter_missing_sys_console_with_ha_guests_is_refused(cli, api, home):
    Lab(api, ha=("vm:301",), privs={"/": {"Sys.Modify": 1}})
    r = cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Sys.Console on /" in r.output
    assert api.writes() == []
    assert records(home) == []


def test_no_ceph_flag_bypasses_sys_modify(cli, api, home):
    Lab(api, privs={"/": {"Sys.Audit": 1}})
    r = cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "--no-ceph-flag",
            "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [w[1] for w in writes(api)] == ["/cluster/bulk-action/guest/migrate"]
    assert records(home) == ["sdnlab__pve-lab1.json"]


def test_enter_answered_no_changes_nothing(cli, api, home):
    Lab(api)
    r = cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-p", "p1", input="n\n")
    assert r.exit_code == 0, r.output
    assert api.writes() == []
    assert records(home) == []


def test_enter_twice_on_the_same_cluster_through_another_profile(cli, api):
    Lab(api)
    assert cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1").exit_code == 0
    n = len(api.writes())
    r = cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p3")
    assert r.exit_code == 1
    assert "already in maintenance" in r.output
    assert len(api.writes()) == n


def test_ha_failback_paused_on_enter_and_restored_on_exit(cli, api, home):
    lab = Lab(api, ha=("vm:301",))
    r = cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    ha_writes = api.called("PUT", "/cluster/ha/resources/{sid}")
    assert [(c.path, c.data) for c in ha_writes] == [("/cluster/ha/resources/vm:301", {"failback": 0})]
    # Paused before the guests move
    order = [w[1] for w in writes(api)]
    assert order.index("/cluster/ha/resources/vm:301") < order.index("/cluster/bulk-action/guest/migrate")
    assert api.called("GET", "/cluster/ha/status/current")  # HA guest followed to the target
    assert lab.ha["vm:301"]["failback"] == 0

    api.calls.clear()
    r = cli("node", "maintenance", "exit", "pve-lab1", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [(c.path, c.data) for c in api.called("PUT", "/cluster/ha/resources/{sid}")] == [
        ("/cluster/ha/resources/vm:301", {"failback": 1})]
    assert lab.ha["vm:301"]["failback"] == 1
    assert records(home) == []


def test_ha_guest_already_without_failback_is_not_touched(cli, api):
    lab = Lab(api, ha=("vm:301",))
    lab.ha["vm:301"]["failback"] = 0
    r = cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("PUT", "/cluster/ha/resources/{sid}") == []
    api.calls.clear()
    assert cli("node", "maintenance", "exit", "pve-lab1", "-y", "-p", "p1").exit_code == 0
    # Exit must not switch on a failback the admin had switched off
    assert api.called("PUT", "/cluster/ha/resources/{sid}") == []


# ── exit ─────────────────────────────────────────────────────────────────


def test_exit_clears_noout_and_brings_guests_home(cli, api, home):
    lab = Lab(api)
    assert cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1").exit_code == 0
    api.calls.clear()
    r = cli("node", "maintenance", "exit", "pve-lab1", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert writes(api) == [
        ("PUT", "/cluster/ceph/flags/noout", {"value": 0}),
        ("POST", "/cluster/bulk-action/guest/migrate",
         {"vms": [301, 311], "target": "pve-lab1", "online": 1}),
    ]
    assert lab.flags["noout"] == 0
    assert lab.node_of(301) == lab.node_of(311) == "pve-lab1"
    assert records(home) == []


def test_exit_keeps_a_noout_pvecli_did_not_set(cli, api):
    lab = Lab(api, noout=True)
    assert cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1").exit_code == 0
    assert cli("node", "maintenance", "exit", "pve-lab1", "-y", "-p", "p1").exit_code == 0
    assert api.called("PUT", "/cluster/ceph/flags/{flag}") == []
    assert lab.flags["noout"] == 1


def test_exit_keeps_noout_while_another_node_of_the_cluster_is_in_maintenance(cli, api, home):
    lab = Lab(api)
    assert cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1").exit_code == 0
    assert cli("node", "maintenance", "enter", "pve-lab3", "--target", "pve-lab2", "-y", "-p", "p1").exit_code == 0
    api.calls.clear()
    r = cli("node", "maintenance", "exit", "pve-lab1", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "noout stays set" in r.output and "pve-lab3" in r.output
    assert api.called("PUT", "/cluster/ceph/flags/{flag}") == []
    assert lab.flags["noout"] == 1
    assert records(home) == ["sdnlab__pve-lab3.json"]


def test_last_node_out_clears_the_noout_pvecli_set(cli, api, home):
    lab = Lab(api)
    assert cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1").exit_code == 0
    assert cli("node", "maintenance", "enter", "pve-lab3", "--target", "pve-lab2", "-y", "-p", "p1").exit_code == 0
    assert cli("node", "maintenance", "exit", "pve-lab1", "-y", "-p", "p1").exit_code == 0
    assert cli("node", "maintenance", "exit", "pve-lab3", "-y", "-p", "p1").exit_code == 0
    assert records(home) == []
    assert lab.flags["noout"] == 0


def test_another_cluster_does_not_hold_noout(cli, api, home):
    """A node in maintenance on another cluster does not keep noout on sdnlab."""
    lab = Lab(api)
    # The recorded pve-9.2 lab is a standalone node: its records are keyed so
    status_h2 = fixture("pve-9.2/cluster_status")
    key = "standalone-" + next(x["name"] for x in status_h2 if x["type"] == "node")
    d = home / ".config/pvecli/maintenance"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}__pve-lab9.json").write_text(json.dumps({"cluster": key, "node": "pve-lab9", "guests": []}))
    assert cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1").exit_code == 0
    r = cli("node", "maintenance", "exit", "pve-lab1", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert lab.flags["noout"] == 0


def test_exit_missing_migrate_right_is_refused_before_any_write(cli, api, home):
    lab = Lab(api)
    assert cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1").exit_code == 0
    api.calls.clear()
    lab.privs = {"/vms/301": {}}
    r = cli("node", "maintenance", "exit", "pve-lab1", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "VM.Migrate on 301" in r.output
    assert api.writes() == []
    assert lab.flags["noout"] == 1
    assert records(home) == ["sdnlab__pve-lab1.json"]


def test_exit_without_record(cli, api):
    Lab(api)
    r = cli("node", "maintenance", "exit", "pve-lab1", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "No maintenance recorded for pve-lab1" in r.output
    assert api.writes() == []


# ── records are per cluster, not per profile ─────────────────────────────


def test_records_are_shared_by_profiles_of_the_same_cluster(cli, api, home):
    lab = Lab(api)
    assert cli("node", "maintenance", "enter", "pve-lab1", "--target", "pve-lab2", "-y", "-p", "p1").exit_code == 0
    r = cli("node", "maintenance", "status")
    assert r.exit_code == 0, r.output
    line = next(x for x in r.stdout.splitlines() if "pve-lab1" in x)
    assert "sdnlab" in line and "p1" in line and "2 -> pve-lab2" in line and "yes" in line
    # p3 points at the same cluster: it sees and closes the maintenance p1 opened
    r = cli("node", "maintenance", "exit", "pve-lab1", "-y", "-p", "p3")
    assert r.exit_code == 0, r.output
    assert lab.flags["noout"] == 0
    assert records(home) == []
    assert "No node in maintenance" in cli("node", "maintenance", "status").output


def test_status_without_records(cli):
    r = cli("node", "maintenance", "status")
    assert r.exit_code == 0
    assert "No node in maintenance" in r.output
