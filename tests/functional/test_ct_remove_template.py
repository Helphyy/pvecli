"""pvecli ct remove and ct template: what is destroyed or frozen, and what is refused."""

from __future__ import annotations

from tests.fake_api import upid

RES = [
    {"vmid": 100, "type": "qemu", "node": "pve1", "status": "stopped", "name": "vm"},
    {"vmid": 200, "type": "lxc", "node": "pve1", "status": "stopped", "name": "web"},
    {"vmid": 201, "type": "lxc", "node": "pve1", "status": "running", "name": "db"},
    {"vmid": 202, "type": "lxc", "node": "pve2", "status": "stopped", "name": "cache"},
    {"vmid": 9100, "type": "lxc", "node": "pve1", "status": "stopped", "name": "tpl", "template": 1},
]


def setup(api, live: dict[int, str] | None = None) -> None:
    """Cluster resources, live status per CT (defaults to the cached one) and task routes."""
    status = {r["vmid"]: r["status"] for r in RES}
    status.update(live or {})
    api.route("GET", "/cluster/resources", RES)
    api.route("GET", "/nodes/{node}/lxc/{vmid}/status/current",
              lambda c: {"status": status[int(c.match["vmid"])]})
    api.route("DELETE", "/nodes/{node}/lxc/{vmid}", lambda c: upid(c.match["node"], "vzdestroy", c.match["vmid"]))
    api.route("POST", "/nodes/{node}/lxc/{vmid}/status/stop", lambda c: upid(c.match["node"], "vzstop", c.match["vmid"]))
    api.route("POST", "/nodes/{node}/lxc/{vmid}/template", None)


# ── remove ──────────────────────────────────────────────────────────────


def test_remove_stopped_container(cli, api):
    setup(api)
    r = cli("ct", "remove", "200", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [delete] = api.writes()
    assert (delete.method, delete.path, delete.params) == ("DELETE", "/nodes/pve1/lxc/200", None)


def test_remove_purge_sends_purge(cli, api):
    setup(api)
    r = cli("ct", "remove", "202", "--purge", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [delete] = api.writes()
    assert delete.path == "/nodes/pve2/lxc/202"
    assert delete.params == {"purge": 1}


def test_remove_running_container_is_refused(cli, api):
    setup(api)
    r = cli("ct", "remove", "201", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "running" in r.output
    assert api.writes() == []


def test_remove_running_force_stops_then_deletes(cli, api):
    setup(api)
    r = cli("ct", "remove", "201", "--force", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [(c.method, c.path) for c in api.writes()] == [
        ("POST", "/nodes/pve1/lxc/201/status/stop"),
        ("DELETE", "/nodes/pve1/lxc/201"),
    ]


def test_remove_uses_live_status_not_lagging_cache(cli, api):
    # /cluster/resources still says stopped, the node says running: refuse
    setup(api, live={200: "running"})
    r = cli("ct", "remove", "200", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_remove_just_stopped_container_is_allowed(cli, api):
    # The cache lags behind a stop that just happened: the live state wins
    setup(api, live={201: "stopped"})
    r = cli("ct", "remove", "201", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == ["/nodes/pve1/lxc/201"]


def test_remove_batch_deletes_stopped_and_refuses_running(cli, api):
    setup(api)
    r = cli("ct", "remove", "200,201", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert [(c.method, c.path) for c in api.writes()] == [("DELETE", "/nodes/pve1/lxc/200")]
    assert "1 deleted, 1 failed" in r.output


def test_remove_declined_confirmation_writes_nothing(cli, api, ui):
    setup(api)
    ui.confirm(False)
    r = cli("ct", "remove", "200", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    assert "cannot be undone" in ui.asked[0][1]


def test_remove_confirmed_interactively(cli, api, ui):
    setup(api)
    ui.multi(["200 - web"])
    ui.confirm(True)
    r = cli("ct", "remove", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == ["/nodes/pve1/lxc/200"]
    assert not ui.left()


def test_remove_menu_lists_only_containers(cli, api, ui):
    setup(api)
    ui.multi(None)
    r = cli("ct", "remove", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    question = ui.asked[0][1]
    assert "200 - web" in question and "100 - vm" not in question


def test_remove_a_vm_id_is_not_a_container(cli, api):
    setup(api)
    r = cli("ct", "remove", "100", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Container 100 not found" in r.output
    assert api.writes() == []


def test_remove_failed_task_is_reported(cli, api):
    setup(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "can't remove CT 200 - protection mode enabled"})
    r = cli("ct", "remove", "200", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "protection mode" in r.output


def test_remove_invalid_id_list_writes_nothing(cli, api):
    setup(api)
    r = cli("ct", "remove", "abc", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


# ── template ────────────────────────────────────────────────────────────


def test_template_converts_stopped_container(cli, api):
    setup(api)
    r = cli("ct", "template", "200", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert (post.method, post.path) == ("POST", "/nodes/pve1/lxc/200/template")


def test_template_running_container_is_not_converted(cli, api):
    setup(api)
    r = cli("ct", "template", "201", "-y", "-p", "p1")
    assert "stop it first" in r.output
    assert api.writes() == []


def test_template_refused_conversion_exits_non_zero(cli, api):
    setup(api)
    r = cli("ct", "template", "201", "-y", "-p", "p1")
    assert r.exit_code == 1


def test_template_batch_converts_only_stopped(cli, api):
    setup(api)
    r = cli("ct", "template", "200-202", "-y", "-p", "p1")
    assert [c.path for c in api.writes()] == [
        "/nodes/pve1/lxc/200/template",
        "/nodes/pve2/lxc/202/template",
    ]
    assert "2/3 converted" in r.output


def test_template_menu_offers_only_non_templates(cli, api, ui):
    setup(api)
    ui.multi(["202 - cache"])
    ui.confirm(True)
    r = cli("ct", "template", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == ["/nodes/pve2/lxc/202/template"]
    question = ui.asked[0][1]
    assert "9100" not in question and "100 - vm" not in question


def test_template_menu_cancelled_writes_nothing(cli, api, ui):
    setup(api)
    ui.multi(None)
    r = cli("ct", "template", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_template_vm_id_is_not_a_container(cli, api):
    setup(api)
    r = cli("ct", "template", "100", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_template_asks_confirmation_before_irreversible_conversion(cli, api, ui):
    setup(api)
    ui.confirm(False)
    cli("ct", "template", "200", "-p", "p1")
    assert api.writes() == []
