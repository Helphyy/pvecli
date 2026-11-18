"""vm remove, template, lock/unlock and power actions: what reaches Proxmox, and what never does."""

from __future__ import annotations

from tests.fake_api import fixture, upid

# pve-9.1 lab: 300 (qemu, pve-lab2), 301 (qemu, pve-lab1), 311 (lxc), 9000 (template)
GUESTS = fixture("pve-9.1/cluster_resources_vm")


def guests(**status: str) -> list[dict]:
    """The recorded guests, with the cached status of some VMs changed (vm300="running")."""
    out = []
    for g in GUESTS:
        g = dict(g)
        key = f"vm{g['vmid']}"
        if key in status:
            g["status"] = status[key]
        out.append(g)
    return out


def setup(api, cached: dict[str, str] | None = None, live: dict[int, dict] | None = None) -> None:
    """Cluster view plus the live status/current each node answers."""
    res = guests(**(cached or {}))
    api.route("GET", "/cluster/resources", res)
    for g in res:
        if g["type"] != "qemu":
            continue
        state = (live or {}).get(g["vmid"], {"status": g["status"], "qmpstatus": g["status"]})
        api.route("GET", f"/nodes/{g['node']}/qemu/{g['vmid']}/status/current", state)
    for action in ("start", "stop", "shutdown", "reboot", "suspend", "resume"):
        api.route("POST", f"/nodes/{{node}}/qemu/{{vmid}}/status/{action}",
                  lambda c, a=action: upid(c.match["node"], f"qm{a}", c.match["vmid"]))
    api.route("DELETE", "/nodes/{node}/qemu/{vmid}",
              lambda c: upid(c.match["node"], "qmdestroy", c.match["vmid"]))
    api.route("POST", "/nodes/{node}/qemu/{vmid}/template",
              lambda c: upid(c.match["node"], "qmtemplate", c.match["vmid"]))
    api.route("PUT", "/nodes/{node}/qemu/{vmid}/config", None)


RUNNING = {"status": "running", "qmpstatus": "running"}
STOPPED = {"status": "stopped", "qmpstatus": "stopped"}
PAUSED = {"status": "running", "qmpstatus": "paused"}  # what PVE answers after `qm suspend`


# ── vm remove ───────────────────────────────────────────────────────────


def test_remove_stopped_vm_sends_a_plain_destroy(cli, api):
    setup(api)
    r = cli("vm", "remove", "300", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [delete] = api.writes()
    assert (delete.method, delete.path) == ("DELETE", "/nodes/pve-lab2/qemu/300")
    # No --purge: the VM stays referenced in backup jobs/HA, and nothing else is destroyed
    assert not (delete.params or {})


def test_remove_purge_only_adds_purge(cli, api):
    setup(api)
    r = cli("vm", "remove", "300", "--purge", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [delete] = api.writes()
    assert delete.params == {"purge": 1}
    assert "destroy-unreferenced-disks" not in delete.params


def test_remove_running_vm_is_refused_without_force(cli, api):
    setup(api, live={300: RUNNING})
    r = cli("vm", "remove", "300", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "running" in r.output
    assert api.writes() == []


def test_remove_paused_vm_is_refused_without_force(cli, api):
    # A paused VM still holds its disks open: PVE reports it "running"
    setup(api, live={300: PAUSED})
    r = cli("vm", "remove", "300", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_remove_trusts_the_live_status_over_the_stale_cluster_view(cli, api):
    # /cluster/resources still says stopped, the node knows it was just started
    setup(api, cached={"vm300": "stopped"}, live={300: RUNNING})
    r = cli("vm", "remove", "300", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_remove_force_stops_then_destroys(cli, api):
    setup(api, live={300: RUNNING})
    r = cli("vm", "remove", "300", "--force", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [(c.method, c.path) for c in api.writes()] == [
        ("POST", "/nodes/pve-lab2/qemu/300/status/stop"),
        ("DELETE", "/nodes/pve-lab2/qemu/300"),
    ]


def test_remove_declined_confirmation_writes_nothing(cli, api, ui):
    setup(api)
    ui.confirm(False)
    r = cli("vm", "remove", "300", "-p", "p1")
    assert r.exit_code == 0
    assert "300" in ui.asked[0][1]
    assert api.writes() == []


def test_remove_cancelled_menu_writes_nothing(cli, api, ui):
    setup(api)
    ui.multi(None)
    r = cli("vm", "remove", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_remove_menu_then_confirm_destroys_the_picked_vm(cli, api, ui):
    setup(api)
    ui.multi(["dhcp-test"]).confirm(True)
    r = cli("vm", "remove", "-p", "p1")
    assert r.exit_code == 0, r.output
    [delete] = api.writes()
    assert delete.path == "/nodes/pve-lab1/qemu/301"


def test_remove_unknown_vm_writes_nothing(cli, api):
    setup(api)
    r = cli("vm", "remove", "999", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "999" in r.output
    assert api.writes() == []


def test_remove_never_deletes_a_container(cli, api):
    setup(api)
    r = cli("vm", "remove", "311", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_remove_one_unknown_id_aborts_before_any_delete(cli, api):
    setup(api)
    r = cli("vm", "remove", "300,999", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_remove_several_skips_the_running_one_and_fails(cli, api):
    setup(api, live={301: RUNNING})
    r = cli("vm", "remove", "300,301", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert [c.path for c in api.writes()] == ["/nodes/pve-lab2/qemu/300"]
    assert "1 deleted, 1 failed" in r.output


def test_remove_failed_destroy_task_is_an_error(cli, api):
    setup(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "can't remove VM 300 - protection mode enabled"})
    r = cli("vm", "remove", "300", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "protection" in r.output


# ── vm template ─────────────────────────────────────────────────────────


def test_template_converts_a_stopped_vm(cli, api, ui):
    setup(api)
    ui.confirm(True)  # an irreversible action may ask first
    r = cli("vm", "template", "300", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert (post.method, post.path) == ("POST", "/nodes/pve-lab2/qemu/300/template")


def test_template_skips_a_running_vm(cli, api, ui):
    setup(api, cached={"vm300": "running"}, live={300: RUNNING})
    ui.confirm(True)
    cli("vm", "template", "300", "-p", "p1")
    assert api.writes() == []


def test_template_running_vm_is_a_failure(cli, api, ui):
    setup(api, cached={"vm300": "running"}, live={300: RUNNING})
    ui.confirm(True)
    r = cli("vm", "template", "300", "-p", "p1")
    assert r.exit_code != 0


def test_template_failed_task_is_a_failure(cli, api, ui):
    setup(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "you can't convert a VM to template if VM is running"})
    ui.confirm(True)
    r = cli("vm", "template", "300", "-p", "p1")
    assert r.exit_code != 0


def test_template_asks_before_an_irreversible_conversion(cli, api, ui):
    setup(api)
    ui.confirm(False)
    cli("vm", "template", "300", "-p", "p1")
    assert api.writes() == []


def test_template_menu_offers_no_existing_template(cli, api, ui):
    setup(api)
    ui.multi(None)
    r = cli("vm", "template", "-p", "p1")
    assert r.exit_code == 0
    question = ui.asked[0][1]
    assert "300" in question and "301" in question
    assert "9000" not in question
    assert api.writes() == []


# ── vm lock / unlock ────────────────────────────────────────────────────


def test_lock_sets_the_lock_key(cli, api):
    setup(api)
    r = cli("vm", "lock", "300", "backup", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.path == "/nodes/pve-lab2/qemu/300/config"
    assert put.data == {"lock": "backup"}


def test_lock_rejects_a_value_proxmox_does_not_know(cli, api):
    setup(api)
    r = cli("vm", "lock", "300", "frozen", "-p", "p1")
    assert r.exit_code == 1
    assert "Invalid lock type" in r.output
    assert api.writes() == []


def test_lock_type_from_the_menu(cli, api, ui):
    setup(api)
    ui.menu("migrate")
    r = cli("vm", "lock", "301", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.data == {"lock": "migrate"}


def test_lock_cancelled_menu_writes_nothing(cli, api, ui):
    setup(api)
    ui.menu(None)
    r = cli("vm", "lock", "301", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_lock_without_root_explains_why(cli, api):
    setup(api)
    api.route("PUT", "/nodes/{node}/qemu/{vmid}/config", api.forbidden("/vms/300", "VM.Config.Options"))
    r = cli("vm", "lock", "300", "backup", "-p", "p1")
    assert r.exit_code == 1
    assert "root@pam" in r.output


def test_unlock_deletes_the_lock_with_skiplock(cli, api):
    setup(api)
    r = cli("vm", "unlock", "300", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.path == "/nodes/pve-lab2/qemu/300/config"
    assert put.data == {"delete": "lock", "skiplock": 1}


def test_unlock_menu_only_offers_locked_vms(cli, api, ui):
    res = guests()
    res[1]["lock"] = "backup"  # 301
    api.route("GET", "/cluster/resources", res)
    api.route("PUT", "/nodes/{node}/qemu/{vmid}/config", None)
    ui.menu("301")
    r = cli("vm", "unlock", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "300" not in ui.asked[0][1]
    [put] = api.writes()
    assert put.path == "/nodes/pve-lab1/qemu/301/config"


def test_unlock_nothing_locked_writes_nothing(cli, api, ui):
    setup(api)
    r = cli("vm", "unlock", "-p", "p1")
    assert r.exit_code == 0
    assert "No locked VMs" in r.output
    assert api.writes() == []


# ── power actions ───────────────────────────────────────────────────────


def test_stop_running_vm(cli, api):
    setup(api, live={301: RUNNING})
    r = cli("vm", "stop", "301", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert post.path == "/nodes/pve-lab1/qemu/301/status/stop"
    assert not post.data


def test_stop_passes_the_timeout(cli, api):
    setup(api, live={301: RUNNING})
    r = cli("vm", "stop", "301", "-t", "30", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert post.data == {"timeout": 30}


def test_stop_skips_a_stopped_vm(cli, api):
    setup(api)
    r = cli("vm", "stop", "301", "-y", "-p", "p1")
    assert r.exit_code == 0
    assert "not running" in r.output
    assert api.writes() == []


def test_stop_declined_writes_nothing(cli, api, ui):
    setup(api, live={301: RUNNING})
    ui.confirm(False)
    r = cli("vm", "stop", "301", "-p", "p1")
    assert r.exit_code == 0
    assert "Hard stop" in ui.asked[0][1]
    assert api.writes() == []


def test_shutdown_sends_the_default_timeout(cli, api):
    setup(api, live={301: RUNNING})
    r = cli("vm", "shutdown", "301", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert post.path == "/nodes/pve-lab1/qemu/301/status/shutdown"
    assert post.data == {"timeout": 60}


def test_shutdown_force_asks_proxmox_to_stop_after_the_timeout(cli, api):
    setup(api, live={301: RUNNING})
    r = cli("vm", "shutdown", "301", "--force", "-t", "10", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert post.data == {"timeout": 10, "forceStop": 1}


def test_shutdown_several_only_touches_the_running_ones(cli, api):
    setup(api, live={301: RUNNING})
    r = cli("vm", "shutdown", "300,301", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == ["/nodes/pve-lab1/qemu/301/status/shutdown"]
    assert "1 shutdown, 1 skipped" in r.output


def test_reboot_running_vm(cli, api):
    setup(api, live={300: RUNNING})
    r = cli("vm", "reboot", "300", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert post.path == "/nodes/pve-lab2/qemu/300/status/reboot"


def test_reboot_declined_writes_nothing(cli, api, ui):
    setup(api, live={300: RUNNING})
    ui.confirm(False)
    cli("vm", "reboot", "300", "-p", "p1")
    assert api.writes() == []


def test_suspend_running_vm(cli, api):
    setup(api, live={300: RUNNING})
    r = cli("vm", "suspend", "300", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert post.path == "/nodes/pve-lab2/qemu/300/status/suspend"


def test_suspend_stopped_vm_is_skipped(cli, api):
    setup(api)
    r = cli("vm", "suspend", "300", "-y", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_resume_stopped_vm_is_skipped(cli, api):
    setup(api)
    r = cli("vm", "resume", "300", "-y", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_resume_paused_vm(cli, api):
    setup(api, cached={"vm300": "running"}, live={300: PAUSED})
    r = cli("vm", "resume", "300", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == ["/nodes/pve-lab2/qemu/300/status/resume"]


def test_power_menu_cancel_writes_nothing(cli, api, ui):
    setup(api, live={300: RUNNING})
    ui.multi(None)
    r = cli("vm", "shutdown", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
