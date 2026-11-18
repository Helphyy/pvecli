"""`storage list/info/remove/config`: reading and editing storage definitions.

Per node status (GET /nodes/{node}/storage) carries active, enabled, total,
used and avail; the definition (GET /storage/{id}) carries path, content...
"""

import json

import pytest

from tests.fake_api import fixture

NODES = [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"}]
TIB = 2**40


def node_storages(call):
    rows = [
        {"storage": "local", "type": "dir", "content": "iso,vztmpl,backup", "active": 1, "enabled": 1,
         "total": 100 * 2**30, "used": 25 * 2**30, "avail": 75 * 2**30},
        {"storage": "labpool", "type": "rbd", "content": "images,rootdir", "active": 1, "enabled": 1,
         "shared": 1, "total": 10 * TIB, "used": 4 * TIB, "avail": 6 * TIB},
        {"storage": "pbs-lab", "type": "pbs", "content": "backup", "active": 0, "enabled": 1,
         "total": 0, "used": 0, "avail": 0},
    ]
    if call.match["node"] == "pve2":
        rows.append({"storage": "old", "type": "dir", "content": "iso", "active": 0, "enabled": 0})
    return rows


@pytest.fixture
def lab(api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/storage", node_storages)
    api.route("GET", "/storage", fixture("pve-9.1/storage"))
    return api


# ── list ─────────────────────────────────────────────────────────────────


def test_list_whole_cluster(cli, lab):
    r = cli("storage", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert {c.match["node"] for c in lab.called("GET", "/nodes/{node}/storage")} == {"pve1", "pve2"}
    for word in ("labpool", "pbs-lab", "inactive", "disabled", "40.0%"):
        assert word in r.output, word


def test_list_json(cli, lab):
    r = cli("storage", "list", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["profile"] == "p1" and doc["node"] is None
    rows = {(s["node"], s["storage"]): s for s in doc["storages"]}
    assert len(rows) == 7
    assert rows[("pve1", "labpool")]["used_percent"] == pytest.approx(40.0)
    # A storage with no capacity reported is 0 %, not a division error
    assert rows[("pve1", "pbs-lab")]["used_percent"] == 0.0
    assert "_node" not in rows[("pve1", "local")]


def test_list_one_node(cli, lab):
    r = cli("storage", "list", "-p", "p1", "-n", "pve2", "--json")
    doc = json.loads(r.stdout)
    assert {s["node"] for s in doc["storages"]} == {"pve2"}
    assert lab.called("GET", "/nodes/pve2/storage") and not lab.called("GET", "/nodes/pve1/storage")
    r = cli("storage", "list", "-p", "p1", "-n", "pve2")
    assert r.exit_code == 0 and "old" in r.output


def test_list_empty(cli, lab):
    lab.route("GET", "/nodes/{node}/storage", [])
    r = cli("storage", "list", "-p", "p1")
    assert r.exit_code == 0
    assert "No storage found" in r.output


def test_list_order_by_unknown_column_fails(cli, lab):
    r = cli("storage", "list", "-p", "p1", "-o", "nonsense")
    assert r.exit_code == 1


def test_list_api_error(cli, lab):
    lab.route("GET", "/nodes", lab.forbidden("/nodes", "Sys.Audit"))
    r = cli("storage", "list", "-p", "p1")
    assert r.exit_code == 1
    assert "Permission denied" in r.output


# ── info ─────────────────────────────────────────────────────────────────


def test_info_one_storage(cli, lab):
    lab.route("GET", "/nodes/{node}/storage/{storage}/status",
              {"type": "dir", "content": "iso,vztmpl,backup", "active": 1, "enabled": 1,
               "total": 100 * 2**30, "used": 25 * 2**30, "avail": 75 * 2**30})
    lab.route("GET", "/storage/{storage}", fixture("pve-9.1/storage")[0])
    r = cli("storage", "info", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "/var/lib/vz" in r.output
    assert "25.0%" in r.output
    assert "active" in r.output


def test_info_menus_several_storages(cli, lab, ui):
    lab.route("GET", "/nodes/{node}/storage/{storage}/status", {"type": "rbd", "active": 0, "enabled": 1})
    lab.route("GET", "/storage/{storage}", {})
    ui.menu("pve2")
    ui.multi(["labpool", "old"])
    r = cli("storage", "info", "-p", "p1")
    assert r.exit_code == 0, r.output
    asked = [c.match["storage"] for c in lab.called("GET", "/nodes/pve2/storage/{storage}/status")]
    assert asked == ["labpool", "old"]
    assert "inactive" in r.output


def test_info_cancelled(cli, lab, ui):
    ui.menu("pve1")
    ui.multi(None)
    r = cli("storage", "info", "-p", "p1")
    assert r.exit_code == 0
    assert not lab.called("GET", "/nodes/{node}/storage/{storage}/status")


def test_info_unknown_storage(cli, lab):
    lab.route("GET", "/nodes/{node}/storage/{storage}/status", lab.error(500, "storage 'zz' does not exist"))
    r = cli("storage", "info", "pve1", "zz", "-p", "p1")
    assert r.exit_code == 1
    assert "does not exist" in r.output


# ── remove ───────────────────────────────────────────────────────────────


def test_remove_with_yes(cli, lab):
    lab.route("DELETE", "/storage/{storage}", None)
    r = cli("storage", "remove", "pbs-lab", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [(c.method, c.path) for c in lab.writes()] == [("DELETE", "/storage/pbs-lab")]
    assert "data on the backend is kept" in r.output


def test_remove_unknown_refused(cli, lab):
    r = cli("storage", "remove", "nope", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "'nope' not found" in r.output
    assert lab.writes() == []


def test_remove_declined_writes_nothing(cli, lab, ui):
    ui.confirm(False)
    r = cli("storage", "remove", "labpool", "-p", "p1")
    assert r.exit_code == 0
    assert lab.writes() == []
    assert "labpool" in ui.asked[0][1]


def test_remove_from_menu(cli, lab, ui):
    lab.route("DELETE", "/storage/{storage}", None)
    ui.menu("local-lvm")
    ui.confirm(True)
    r = cli("storage", "remove", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in lab.writes()] == ["/storage/local-lvm"]


def test_remove_menu_cancelled(cli, lab, ui):
    ui.menu(None)
    cli("storage", "remove", "-p", "p1")
    assert lab.writes() == []


# ── config (content types) ───────────────────────────────────────────────


def test_config_changes_content_types(cli, lab, ui):
    lab.route("GET", "/storage/{storage}", fixture("pve-9.1/storage")[0])
    lab.route("PUT", "/storage/{storage}", None)
    ui.multi(["iso", "snippets"])
    ui.confirm(True)
    r = cli("storage", "config", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = lab.writes()
    assert put.path == "/storage/local"
    # Only content is sent: the path and other settings are left alone
    assert put.data == {"content": "iso,snippets"}


def test_config_preselects_current_types(cli, lab, ui, monkeypatch):
    lab.route("GET", "/storage/{storage}", fixture("pve-9.1/storage")[0])
    seen = {}

    def multi(items, title="", preselected=None, **k):
        seen["pre"] = sorted(items[i] for i in preselected)
        return None

    import src.cli.storage as storage_mod
    monkeypatch.setattr(storage_mod, "multi_select_menu", multi)
    r = cli("storage", "config", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0
    assert seen["pre"] == ["backup", "import", "iso", "vztmpl"]
    assert lab.writes() == []


def test_config_declined_writes_nothing(cli, lab, ui):
    lab.route("GET", "/storage/{storage}", fixture("pve-9.1/storage")[0])
    ui.multi(["iso"])
    ui.confirm(False)
    r = cli("storage", "config", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0
    assert lab.writes() == []


def test_config_storage_menu_on_node(cli, lab, ui):
    lab.route("GET", "/storage/{storage}", fixture("pve-9.1/storage")[1])
    lab.route("PUT", "/storage/{storage}", None)
    ui.menu("pve1", "labpool")
    ui.multi(["images"])
    ui.confirm(True)
    r = cli("storage", "config", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert lab.writes()[0].path == "/storage/labpool"
    assert lab.writes()[0].data == {"content": "images"}


def test_config_no_storage_on_node(cli, lab, ui):
    lab.route("GET", "/nodes/{node}/storage", [])
    ui.menu("pve1")
    r = cli("storage", "config", "-p", "p1")
    assert r.exit_code == 0
    assert "No storages found" in r.output
    assert lab.writes() == []


def test_config_refused_by_pve(cli, lab, ui):
    lab.route("GET", "/storage/{storage}", fixture("pve-9.1/storage")[1])
    lab.route("PUT", "/storage/{storage}", lab.error(400, "content: storage does not support content type 'iso'"))
    ui.multi(["iso"])
    ui.confirm(True)
    r = cli("storage", "config", "pve1", "labpool", "-p", "p1")
    assert r.exit_code == 1
    assert "does not support" in r.output
