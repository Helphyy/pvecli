"""Guest tags and cluster tag colors: removing the last tag, keeping tag-style options."""

from __future__ import annotations

RES = [
    {"vmid": 100, "type": "qemu", "node": "pve1", "status": "running", "name": "a", "tags": "web"},
    {"vmid": 101, "type": "qemu", "node": "pve1", "status": "running", "name": "b", "tags": "db;web"},
    {"vmid": 200, "type": "lxc", "node": "pve2", "status": "running", "name": "c", "tags": "web"},
]


def _style(value: str) -> dict[str, str]:
    """'color-map=a:1;b:2,shape=circle' -> {'color-map': ..., 'shape': ...}"""
    return dict(part.split("=", 1) for part in value.split(","))


def test_vm_removing_last_tag_deletes_the_key(cli, api):
    api.route("GET", "/cluster/resources", RES)
    api.route("GET", "/nodes/pve1/qemu/100/config", {"tags": "web"})
    api.route("PUT", "/nodes/pve1/qemu/100/config", None)
    r = cli("vm", "tag", "remove", "100", "web", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.path == "/nodes/pve1/qemu/100/config"
    assert put.data == {"delete": "tags"}


def test_vm_removing_one_of_several_tags_keeps_the_others(cli, api):
    api.route("GET", "/cluster/resources", RES)
    api.route("GET", "/nodes/pve1/qemu/101/config", {"tags": "db;web"})
    api.route("PUT", "/nodes/pve1/qemu/101/config", None)
    r = cli("vm", "tag", "remove", "101", "web", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.data == {"tags": "db"}


def test_ct_removing_last_tag_deletes_the_key(cli, api):
    api.route("GET", "/cluster/resources", RES)
    api.route("GET", "/nodes/pve2/lxc/200/config", {"tags": "web"})
    api.route("PUT", "/nodes/pve2/lxc/200/config", None)
    r = cli("ct", "tag", "remove", "200", "web", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.path == "/nodes/pve2/lxc/200/config"
    assert put.data == {"delete": "tags"}


def test_cluster_tag_remove_deletes_key_on_guests_left_without_tags(cli, api):
    api.route("GET", "/cluster/resources", RES)
    api.route("GET", "/cluster/options", {"keyboard": "fr"})
    api.route("PUT", "/nodes/{node}/{kind}/{vmid}/config", None)
    r = cli("tag", "remove", "web", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    by_path = {c.path: c.data for c in api.writes()}
    assert by_path["/nodes/pve1/qemu/101/config"] == {"tags": "db"}
    # Same rule as `vm tag remove`: an empty value leaves a blank tag behind
    assert by_path["/nodes/pve1/qemu/100/config"] == {"delete": "tags"}
    assert by_path["/nodes/pve2/lxc/200/config"] == {"delete": "tags"}


def test_tag_color_add_keeps_shape_and_ordering(cli, api):
    # PVE returns tag-style parsed, as an object
    api.route("GET", "/cluster/options", {
        "tag-style": {"color-map": "db:00FF00", "shape": "circle", "ordering": "config"},
    })
    api.route("PUT", "/cluster/options", None)
    r = cli("tag", "add", "web", "--color", "#ff4444", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.path == "/cluster/options"
    style = _style(put.data["tag-style"])
    assert style["shape"] == "circle"
    assert style["ordering"] == "config"
    assert set(style["color-map"].split(";")) == {"db:00FF00", "web:ff4444"}


def test_tag_color_remove_keeps_shape(cli, api):
    api.route("GET", "/cluster/resources", [])
    api.route("GET", "/cluster/options", {
        "tag-style": {"color-map": "db:00FF00;web:FF0000", "shape": "dense"},
    })
    api.route("PUT", "/cluster/options", None)
    r = cli("tag", "remove", "web", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    style = _style(put.data["tag-style"])
    assert style == {"color-map": "db:00FF00", "shape": "dense"}


def test_tag_color_remove_last_color_keeps_shape_alone(cli, api):
    api.route("GET", "/cluster/resources", [])
    api.route("GET", "/cluster/options", {"tag-style": {"color-map": "web:FF0000", "shape": "none"}})
    api.route("PUT", "/cluster/options", None)
    r = cli("tag", "remove", "web", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.data == {"tag-style": "shape=none"}


def test_tag_color_remove_last_option_deletes_tag_style(cli, api):
    api.route("GET", "/cluster/resources", [])
    api.route("GET", "/cluster/options", {"tag-style": {"color-map": "web:FF0000"}})
    api.route("PUT", "/cluster/options", None)
    r = cli("tag", "remove", "web", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.data == {"delete": "tag-style"}
