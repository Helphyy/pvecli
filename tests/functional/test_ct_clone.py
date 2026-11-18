"""pvecli ct clone: the clone request and the config applied after it, from flags and from the wizard."""

from __future__ import annotations

from tests.fake_api import upid

RES = [
    {"vmid": 100, "type": "qemu", "node": "pve1", "status": "stopped", "name": "vm", "tags": "prod"},
    {"vmid": 200, "type": "lxc", "node": "pve1", "status": "stopped", "name": "web", "tags": "web"},
    {"vmid": 9100, "type": "lxc", "node": "pve1", "status": "stopped", "name": "tpl", "template": 1},
    {"vmid": 9200, "type": "lxc", "node": "pve1", "status": "stopped", "name": "tpl-dir", "template": 1},
]

CONFIGS = {
    200: {"hostname": "web", "rootfs": "local-lvm:vm-200-disk-0,size=8G", "tags": "web", "memory": 512},
    9100: {"hostname": "tpl", "rootfs": "local-lvm:base-9100-disk-0,size=4G", "template": 1},
    # A template on a directory storage: no linked clone possible there
    9200: {"hostname": "tpl-dir", "rootfs": "local:9200/base-9200-disk-0.raw,size=4G", "template": 1},
}

# GET /nodes/{node}/storage: the status view of each storage (PVE 9)
NODE_STORAGE = [
    {"storage": "local", "type": "dir", "content": "backup,import,vztmpl,iso", "active": 1, "enabled": 1,
     "shared": 0, "total": 14513811456, "used": 4558462976, "avail": 9200000000},
    {"storage": "local-lvm", "type": "lvmthin", "content": "rootdir,images", "active": 1, "enabled": 1,
     "shared": 0, "total": 8000000000, "used": 1000000000, "avail": 7000000000},
]


def setup(api, pools: list[str] | None = None, resources: list[dict] | None = None) -> None:
    api.route("GET", "/cluster/resources", resources if resources is not None else RES)
    api.route("GET", "/nodes/{node}/lxc/{vmid}/config", lambda c: CONFIGS[int(c.match["vmid"])])
    api.route("GET", "/nodes/{node}/storage", NODE_STORAGE)
    api.route("GET", "/cluster/nextid", "300")
    api.route("GET", "/pools", [{"poolid": p} for p in (pools or [])])
    api.route("GET", "/cluster/options", {})
    api.route("POST", "/nodes/{node}/lxc/{vmid}/clone", lambda c: upid(c.match["node"], "vzclone", c.match["vmid"]))
    api.route("PUT", "/nodes/{node}/lxc/{vmid}/config", None)


def clone_call(api):
    [call] = api.called("POST", "/nodes/{node}/lxc/{vmid}/clone")
    return call


# ── non-interactive ─────────────────────────────────────────────────────


def test_clone_with_flags_sends_newid_and_hostname_only(cli, api):
    setup(api)
    r = cli("ct", "clone", "200", "--newid", "300", "--hostname", "web2", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [(c.method, c.path) for c in api.writes()] == [("POST", "/nodes/pve1/lxc/200/clone")]
    # A normal CT is always fully copied by PVE: no "full" to send
    assert clone_call(api).data == {"newid": 300, "hostname": "web2"}


def test_clone_with_pool_target_and_onboot(cli, api):
    setup(api)
    r = cli("ct", "clone", "200", "--newid", "300", "--hostname", "web2", "--pool", "prod",
            "--target", "pve2", "--onboot", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert clone_call(api).data == {"newid": 300, "hostname": "web2", "pool": "prod", "target": "pve2"}
    # onboot is not a clone parameter: it is set on the new CT, where it lives
    [put] = api.called("PUT", "/nodes/{node}/lxc/{vmid}/config")
    assert put.path == "/nodes/pve2/lxc/300/config"
    assert put.data == {"onboot": 1}


def test_clone_target_equal_to_source_is_not_sent(cli, api):
    setup(api)
    r = cli("ct", "clone", "200", "--newid", "300", "--hostname", "web2", "--target", "pve1", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "target" not in clone_call(api).data


def test_clone_template_full(cli, api):
    setup(api)
    r = cli("ct", "clone", "9100", "--newid", "300", "--hostname", "web2", "--full", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert clone_call(api).data == {"newid": 300, "hostname": "web2", "full": 1}


def test_clone_template_linked_by_default(cli, api):
    setup(api)
    r = cli("ct", "clone", "9100", "--newid", "300", "--hostname", "web2", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "full" not in clone_call(api).data


def test_clone_template_on_directory_storage_is_full(cli, api):
    setup(api)
    r = cli("ct", "clone", "9200", "--newid", "300", "--hostname", "web2", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert clone_call(api).data.get("full") == 1


def test_clone_unknown_source_writes_nothing(cli, api):
    setup(api)
    r = cli("ct", "clone", "999", "--newid", "300", "--hostname", "x", "-p", "p1")
    assert r.exit_code == 1
    assert "999 not found" in r.output
    assert api.writes() == []


def test_clone_vm_id_writes_nothing(cli, api):
    setup(api)
    # What PVE answers when the lxc endpoint is asked for a VM id
    api.route("GET", "/nodes/pve1/lxc/100/config",
              api.error(500, "Configuration file 'nodes/pve1/lxc/100.conf' does not exist"))
    r = cli("ct", "clone", "100", "--newid", "300", "--hostname", "x", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_clone_failed_task_skips_post_config(cli, api):
    setup(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "clone failed: no space left"})
    r = cli("ct", "clone", "200", "--newid", "300", "--hostname", "web2", "--onboot", "-p", "p1")
    assert r.exit_code == 1
    assert "no space left" in r.output
    assert api.called("PUT", "/nodes/{node}/lxc/{vmid}/config") == []


# ── wizard ──────────────────────────────────────────────────────────────


def test_wizard_full_flow(cli, api, ui):
    setup(api, pools=["prod", "dev"])
    ui.text("301", "web2")         # new CTID, hostname
    ui.menu("prod")                # pool
    ui.multi(["web", "prod"])      # tags
    ui.confirm(True, True, False)  # onboot, go, print command
    r = cli("ct", "clone", "200", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert clone_call(api).data == {"newid": 301, "hostname": "web2", "pool": "prod"}
    [put] = api.called("PUT", "/nodes/{node}/lxc/{vmid}/config")
    assert put.path == "/nodes/pve1/lxc/301/config"
    assert put.data == {"tags": "prod;web", "onboot": 1}
    assert not ui.left()


def test_wizard_defaults_to_next_id_and_source_hostname(cli, api, ui):
    setup(api)
    ui.text(None, None)            # accept defaults
    ui.multi([])                   # no tag picked: keep the source ones
    ui.confirm(False, True, False)
    r = cli("ct", "clone", "200", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert clone_call(api).data == {"newid": 300, "hostname": "web"}
    [put] = api.called("PUT", "/nodes/{node}/lxc/{vmid}/config")
    assert put.data == {"tags": "web", "onboot": 0}


def test_wizard_invalid_ctid_is_asked_again(cli, api, ui):
    setup(api)
    ui.text("abc", "302", "web2")
    ui.multi([])
    ui.confirm(False, True, False)
    r = cli("ct", "clone", "200", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "valid number" in r.output
    assert clone_call(api).data["newid"] == 302


def test_wizard_declined_writes_nothing(cli, api, ui):
    setup(api, pools=["prod"])
    ui.text("301", "web2")
    ui.menu("prod")
    ui.multi([])
    ui.confirm(True, False)
    r = cli("ct", "clone", "200", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_wizard_none_tags_removes_source_tags(cli, api, ui):
    setup(api)
    ui.text("301", "web2")
    ui.multi(["(none)"])
    ui.confirm(False, True, False)
    r = cli("ct", "clone", "200", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.called("PUT", "/nodes/{node}/lxc/{vmid}/config")
    assert put.data.get("delete") == "tags"


def test_wizard_custom_tag(cli, api, ui):
    setup(api)
    ui.text("301", "web2")
    ui.multi(["+ Add custom tag"])
    ui.text("blue")
    ui.confirm(False, True, False)
    r = cli("ct", "clone", "200", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.called("PUT", "/nodes/{node}/lxc/{vmid}/config")
    assert put.data["tags"] == "blue"


def test_wizard_without_known_tags_asks_free_text(cli, api, ui):
    resources = [{**r, "tags": ""} for r in RES]
    setup(api, resources=resources)
    ui.text("301", "web2", "a;b")
    ui.confirm(False, True, False)
    r = cli("ct", "clone", "200", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.called("PUT", "/nodes/{node}/lxc/{vmid}/config")
    assert put.data["tags"] == "a;b"


def test_wizard_template_linked_clone_on_dir_storage_is_forced_full(cli, api, ui):
    setup(api)
    ui.text("301", "web2")
    ui.multi([])
    ui.confirm(False, True, False)  # onboot, go, print: no linked-clone question
    r = cli("ct", "clone", "9200", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert clone_call(api).data["full"] == 1
    assert not any("linked" in q for _, q in ui.asked)


def test_wizard_template_on_lvmthin_offers_linked_clone(cli, api, ui):
    setup(api)
    ui.text("301", "web2")
    ui.multi([])
    ui.confirm(False, True, True, False)  # onboot, linked clone, go, print
    r = cli("ct", "clone", "9100", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert any("linked" in q for _, q in ui.asked)
    assert "full" not in clone_call(api).data


def test_wizard_template_full_flag_skips_question(cli, api, ui):
    setup(api)
    ui.text("301", "web2")
    ui.multi([])
    ui.confirm(False, True, False)
    r = cli("ct", "clone", "9100", "--full", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert clone_call(api).data["full"] == 1


def test_wizard_with_target_and_pool_flags(cli, api, ui):
    setup(api, pools=["prod"])
    ui.text("301", "web2")
    ui.multi([])
    ui.confirm(True, True, False)
    r = cli("ct", "clone", "200", "--pool", "prod", "--target", "pve2", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert clone_call(api).data == {"newid": 301, "hostname": "web2", "pool": "prod", "target": "pve2"}
    [put] = api.called("PUT", "/nodes/{node}/lxc/{vmid}/config")
    assert put.path == "/nodes/pve2/lxc/301/config"


def test_wizard_prints_reproducible_command_with_profile(cli, api, ui):
    setup(api, pools=["prod"])
    ui.text("301", "web2")
    ui.menu("prod")
    ui.multi([])
    ui.confirm(True, True, True)
    r = cli("ct", "clone", "200", "--target", "pve2", "-p", "p1")
    assert r.exit_code == 0, r.output
    for part in ("pvecli ct clone 200", "--newid 301", "--hostname web2", "--pool prod",
                 "--onboot", "--target pve2", "-p p1"):
        assert part in r.output


def test_wizard_picks_source_from_menu(cli, api, ui):
    setup(api)
    ui.menu("200 - web")
    ui.text("301", "web2")
    ui.multi([])
    ui.confirm(False, True, False)
    r = cli("ct", "clone", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert clone_call(api).path == "/nodes/pve1/lxc/200/clone"


def test_wizard_source_menu_cancelled(cli, api, ui):
    setup(api)
    ui.menu(None)
    r = cli("ct", "clone", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
