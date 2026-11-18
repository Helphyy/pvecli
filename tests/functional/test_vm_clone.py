"""vm clone, with options and through the wizard: the clone request and the config set afterwards."""

from __future__ import annotations

from tests.fake_api import fixture, upid

# pve-9.1 lab: 300 (qemu, pve-lab2), 301 (qemu, pve-lab1), 311 (lxc), 9000 (template on pve-lab1)
GUESTS = [dict(g) for g in fixture("pve-9.1/cluster_resources_vm")]
GUESTS[0]["tags"] = "web"  # 300
GUESTS[1]["tags"] = "db;prod"  # 301

TEMPLATE_CFG = {"name": "deb13-cloud", "template": 1, "cores": 1, "memory": "1024",
                "scsi0": "labpool:base-9000-disk-0,size=3G", "tags": "cloud"}
VM_CFG = {**fixture("pve-9.1/qemu_config"), "name": "dhcp-test", "tags": "db;prod"}


def setup(api, pools: list[str] | None = None, tag_style: str = "") -> None:
    api.route("GET", "/cluster/resources", GUESTS)
    api.route("GET", "/nodes/pve-lab1/qemu/9000/config", TEMPLATE_CFG)
    api.route("GET", "/nodes/pve-lab1/qemu/301/config", VM_CFG)
    api.route("GET", "/cluster/nextid", "105")
    api.route("GET", "/pools", [{"poolid": p} for p in (pools or [])])
    api.route("GET", "/cluster/options", {"tag-style": tag_style} if tag_style else {})
    api.route("POST", "/nodes/{node}/qemu/{vmid}/clone",
              lambda c: upid(c.match["node"], "qmclone", c.match["vmid"]))
    api.route("PUT", "/nodes/{node}/qemu/{vmid}/config", None)


def clone_call(api):
    [call] = api.called("POST", "/nodes/{node}/qemu/{vmid}/clone")
    return call


# ── non interactive ─────────────────────────────────────────────────────


def test_clone_of_a_vm_sends_only_what_was_asked(cli, api):
    setup(api)
    r = cli("vm", "clone", "301", "--newid", "400", "--name", "web2", "-p", "p1")
    assert r.exit_code == 0, r.output
    call = clone_call(api)
    assert call.path == "/nodes/pve-lab1/qemu/301/clone"
    # A normal VM is always fully copied by PVE: no full/linked choice to send
    assert call.data == {"newid": 400, "name": "web2"}
    assert len(api.writes()) == 1


def test_clone_of_a_template_is_linked_by_default(cli, api):
    setup(api)
    r = cli("vm", "clone", "9000", "--newid", "400", "--name", "web2", "-p", "p1")
    assert r.exit_code == 0, r.output
    # PVE makes a linked clone of a template unless full=1 is sent
    assert not clone_call(api).data.get("full")


def test_clone_of_a_template_full(cli, api):
    setup(api)
    r = cli("vm", "clone", "9000", "--newid", "400", "--name", "web2", "--full", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert clone_call(api).data["full"] == 1


def test_clone_to_another_node_sets_onboot_on_that_node(cli, api):
    setup(api)
    r = cli("vm", "clone", "9000", "--newid", "400", "--name", "web2", "--full",
            "--target", "pve-lab3", "--pool", "prod", "--onboot", "-p", "p1")
    assert r.exit_code == 0, r.output
    call = clone_call(api)
    assert call.path == "/nodes/pve-lab1/qemu/9000/clone"  # sent to the source node
    assert call.data == {"newid": 400, "name": "web2", "full": 1, "target": "pve-lab3", "pool": "prod"}
    [put] = api.called("PUT", "/nodes/{node}/qemu/{vmid}/config")
    assert put.path == "/nodes/pve-lab3/qemu/400/config"
    assert put.data == {"onboot": 1}


def test_clone_target_equal_to_source_is_not_sent(cli, api):
    setup(api)
    r = cli("vm", "clone", "301", "--newid", "400", "--name", "x", "--target", "pve-lab1", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "target" not in clone_call(api).data


def test_clone_no_onboot_is_written_as_zero(cli, api):
    setup(api)
    r = cli("vm", "clone", "301", "--newid", "400", "--name", "x", "--no-onboot", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.called("PUT", "/nodes/{node}/qemu/{vmid}/config")
    assert put.path == "/nodes/pve-lab1/qemu/400/config"
    assert put.data == {"onboot": 0}


def test_clone_of_an_unknown_vm_writes_nothing(cli, api):
    setup(api)
    r = cli("vm", "clone", "999", "--newid", "400", "--name", "x", "-p", "p1")
    assert r.exit_code == 1
    assert "999" in r.output
    assert api.writes() == []


def test_clone_of_a_container_writes_nothing(cli, api):
    setup(api)
    # What PVE answers when asked the qemu config of a container ID
    api.route("GET", "/nodes/pve-lab1/qemu/311/config",
              api.error(500, "Configuration file 'nodes/pve-lab1/qemu-server/311.conf' does not exist"))
    r = cli("vm", "clone", "311", "--newid", "400", "--name", "x", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_clone_failed_task_does_not_touch_the_config(cli, api):
    setup(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "unable to create VM 400: config file already exists"})
    r = cli("vm", "clone", "301", "--newid", "400", "--name", "x", "--onboot", "-p", "p1")
    assert r.exit_code == 1
    assert "already exists" in r.output
    assert api.called("PUT", "/nodes/{node}/qemu/{vmid}/config") == []


# ── wizard ──────────────────────────────────────────────────────────────


def test_wizard_full_clone_of_a_template(cli, api, ui):
    setup(api, pools=["dev", "prod"])
    ui.text("401", "web-01")
    ui.menu("prod")
    ui.multi(["web"])
    # onboot, linked clone? (no: full), go, print the command
    ui.confirm(True, False, True, True)
    r = cli("vm", "clone", "9000", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert clone_call(api).data == {"newid": 401, "name": "web-01", "full": 1, "pool": "prod"}
    [put] = api.called("PUT", "/nodes/{node}/qemu/{vmid}/config")
    assert put.path == "/nodes/pve-lab1/qemu/401/config"
    assert put.data == {"tags": "web", "onboot": 1}
    # The printed command reproduces the clone on the same profile
    out = r.stdout.replace("\\\n", " ")
    for part in ("pvecli vm clone 9000", "--newid 401", "--name web-01", "--pool prod",
                 "--onboot", "--full", "-p p1"):
        assert part in out
    assert not ui.left()


def test_wizard_defaults_take_the_next_id_and_the_source_name(cli, api, ui):
    setup(api)
    ui.text(None, None)
    ui.multi([])  # nothing picked: keep the source tags
    ui.confirm(False, True, False)  # no onboot, go, no command
    r = cli("vm", "clone", "301", "-p", "p1")
    assert r.exit_code == 0, r.output
    # No pool configured: no pool menu; a normal VM: no linked-clone question
    assert not [q for q in ui.asked if "linked" in q[1]]
    assert clone_call(api).data == {"newid": 105, "name": "dhcp-test"}
    [put] = api.called("PUT", "/nodes/{node}/qemu/{vmid}/config")
    assert put.path == "/nodes/pve-lab1/qemu/105/config"
    assert put.data == {"tags": "db;prod", "onboot": 0}
    assert not ui.left()


def test_wizard_custom_tag(cli, api, ui):
    setup(api)
    ui.text("401", "x", "  new-tag ")
    ui.multi(["web", "+ Add custom tag"])
    ui.confirm(False, True, False)
    r = cli("vm", "clone", "301", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.called("PUT", "/nodes/{node}/qemu/{vmid}/config")
    assert put.data["tags"] == "new-tag;web"


def test_wizard_retries_a_non_numeric_id(cli, api, ui):
    setup(api)
    ui.text("abc", "402", "x")
    ui.multi([])
    ui.confirm(False, True, False)
    r = cli("vm", "clone", "301", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "valid number" in r.output
    assert clone_call(api).data["newid"] == 402


def test_wizard_declined_writes_nothing(cli, api, ui):
    setup(api, pools=["prod"])
    ui.text("401", "x")
    ui.menu("prod")
    ui.multi(["web"])
    ui.confirm(True, True, False)  # onboot, linked, then refuse the summary
    r = cli("vm", "clone", "9000", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_wizard_cancelled_vm_menu_writes_nothing(cli, api, ui):
    setup(api)
    ui.menu(None)
    r = cli("vm", "clone", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_wizard_linked_clone_of_a_template(cli, api, ui):
    setup(api)
    ui.text("401", "x")
    ui.multi([])
    ui.confirm(False, True, True, False)  # no onboot, linked: yes, go, no command
    r = cli("vm", "clone", "9000", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert not clone_call(api).data.get("full")


def test_wizard_none_tag_gives_a_clone_without_tags(cli, api, ui):
    setup(api)
    ui.text("401", "x")
    ui.multi(["(none)"])
    ui.confirm(False, True, False)
    r = cli("vm", "clone", "301", "-p", "p1")
    assert r.exit_code == 0, r.output
    puts = api.called("PUT", "/nodes/pve-lab1/qemu/401/config")
    assert any("tags" in str((p.data or {}).get("delete", "")).split(",") for p in puts)
